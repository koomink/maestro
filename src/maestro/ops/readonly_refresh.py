from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError

from maestro.config.identity import ConfigIdentity
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
from maestro.core.provenance import current_deployment_identity
from maestro.execution.broker_router import BrokerAccountRouter
from maestro.execution.brokers.readonly_factory import (
    broker_readonly_accounts,
    build_broker_readonly_service,
)
from maestro.execution.brokers.toss.transport import TossRateLimitError
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


@dataclass(frozen=True)
class AccountRefreshResult:
    account_id: str
    status: str
    snapshot_id: int | None
    age_seconds: float | None
    retry_count: int
    reconciliation_passed: bool | None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {"cached", "refreshed"} and self.reconciliation_passed is not False


@dataclass(frozen=True)
class ReadonlyRefreshReport:
    run_id: str
    results: tuple[AccountRefreshResult, ...]

    @property
    def failed_account_ids(self) -> list[str]:
        return [result.account_id for result in self.results if not result.usable]


def required_account_ids_for_strategies(
    config: MaestroConfig,
    strategy_ids: list[str] | set[str] | None,
) -> list[str]:
    selected = set(strategy_ids or [])
    router = BrokerAccountRouter(config)
    output: set[str] = set()
    for strategy in config.strategies:
        if not strategy.enabled or not strategy.signal_enabled:
            continue
        if selected and strategy.id not in selected:
            continue
        group = config.multi_account_contribution_group_for_strategy(strategy.id)
        if group is not None:
            output.update(target.account_id for target in group.account_targets)
            continue
        if not strategy.account_id:
            continue
        account = router.account(strategy.account_id)
        if account is None or account.broker != "sandbox":
            output.add(strategy.account_id)
    return sorted(output)


def refresh_readonly_accounts(
    config: MaestroConfig,
    identity: ConfigIdentity | None,
    *,
    account_ids: list[str] | None = None,
    source: str,
    max_snapshot_age_seconds: int | None = None,
    attempts: int = 3,
    state_store: StateStore | None = None,
    audit_logger: AuditLogger | None = None,
) -> ReadonlyRefreshReport:
    store = state_store or StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )
    audit = audit_logger or AuditLogger(config.audit.jsonl_path)
    configured: dict[str, tuple[str | None, Any]] = {}
    for account_id, account in broker_readonly_accounts(config):
        logical_id = str(account_id or getattr(account, "account_id", None) or "default_kis")
        configured[logical_id] = (account_id, account)
    full_refresh = account_ids is None
    selected = sorted(account_ids or configured)
    unknown = sorted(set(selected) - set(configured))
    if unknown:
        raise ValueError("Unknown read-only account_id(s): " + ", ".join(unknown))
    run_id = new_run_id()
    deployment = current_deployment_identity()
    save_audited_system_event(
        store,
        audit,
        run_id,
        SystemEventType.RUN_PROVENANCE,
        {
            "run_kind": "readonly_refresh",
            "signal_run_id": None,
            "deployment_commit": deployment.commit,
            "deployment_source_fingerprint": deployment.source_fingerprint,
            "deployment_dirty": deployment.dirty,
            "config_path": identity.path if identity else None,
            "config_fingerprint": identity.fingerprint if identity else None,
            "config_runtime_fingerprint": identity.runtime_fingerprint if identity else None,
            "source": source,
            "account_ids": selected,
        },
    )
    results = [
        _refresh_one_account(
            config,
            store,
            audit,
            run_id=run_id,
            account_id=account_id,
            service_account_id=configured[account_id][0],
            source=source,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            attempts=attempts,
        )
        for account_id in selected
    ]
    if full_refresh:
        _record_account_lifecycle(store, audit, run_id, set(selected))
    return ReadonlyRefreshReport(run_id=run_id, results=tuple(results))


def _record_account_lifecycle(
    store: StateStore,
    audit: AuditLogger,
    run_id: str,
    configured_account_ids: set[str],
) -> None:
    active: set[str] = set()
    events = [
        *store.list_system_events_by_type(SystemEventType.ACCOUNT_TRACKING_STARTED, limit=1000),
        *store.list_system_events_by_type(SystemEventType.ACCOUNT_TRACKING_ENDED, limit=1000),
    ]
    for row in sorted(events, key=lambda item: (str(item.get("created_at") or ""), item["id"])):
        payload = row.get("payload") or {}
        account_id = str(payload.get("account_id") or "")
        if not account_id:
            continue
        if row.get("event_type") == SystemEventType.ACCOUNT_TRACKING_STARTED:
            active.add(account_id)
        else:
            active.discard(account_id)
    effective_at = utc_now().isoformat()
    for account_id in sorted(configured_account_ids - active):
        save_audited_system_event(
            store,
            audit,
            run_id,
            SystemEventType.ACCOUNT_TRACKING_STARTED,
            {"account_id": account_id, "effective_at": effective_at, "source": "full_refresh"},
        )
    for account_id in sorted(active - configured_account_ids):
        save_audited_system_event(
            store,
            audit,
            run_id,
            SystemEventType.ACCOUNT_TRACKING_ENDED,
            {"account_id": account_id, "effective_at": effective_at, "source": "full_refresh"},
        )


def latest_snapshot_for_account(store: StateStore, account_id: str) -> dict[str, Any] | None:
    for row in store.list_broker_account_snapshots(limit=1000):
        payload = row.get("payload") or {}
        logical_account_id = str(payload.get("account_id") or row.get("account_id") or "")
        if logical_account_id == account_id:
            return row
    return None


def snapshot_age_seconds(row: dict[str, Any] | None) -> float | None:
    if row is None or not row.get("created_at"):
        return None
    created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=utc_now().tzinfo)
    return max(0.0, (utc_now() - created_at).total_seconds())


def _refresh_one_account(
    config: MaestroConfig,
    store: StateStore,
    audit: AuditLogger,
    *,
    run_id: str,
    account_id: str,
    service_account_id: str | None,
    source: str,
    max_snapshot_age_seconds: int | None,
    attempts: int,
) -> AccountRefreshResult:
    started_at = time.monotonic()
    latest = latest_snapshot_for_account(store, account_id)
    age_seconds = snapshot_age_seconds(latest)
    if (
        max_snapshot_age_seconds is not None
        and age_seconds is not None
        and age_seconds <= max_snapshot_age_seconds
        and _snapshot_has_passed_reconciliation(store, account_id, latest)
    ):
        return _save_result(
            store,
            audit,
            run_id,
            source,
            started_at,
            AccountRefreshResult(
                account_id=account_id,
                status="cached",
                snapshot_id=int(latest["id"]),
                age_seconds=age_seconds,
                retry_count=0,
                reconciliation_passed=True,
            ),
        )
    retry_count = 0
    try:
        with store.account_refresh_lock(account_id):
            for attempt in range(max(1, attempts)):
                try:
                    service = build_broker_readonly_service(
                        config,
                        store,
                        audit,
                        account_id=service_account_id,
                    )
                    try:
                        service.fetch_and_store_snapshot(
                            config.portfolio.allowed_symbols,
                            run_id=run_id,
                        )
                    except TypeError as exc:
                        if "unexpected keyword argument 'run_id'" not in str(exc):
                            raise
                        service.fetch_and_store_snapshot(config.portfolio.allowed_symbols)
                    reconciliation = BrokerReconciliationService(
                        config.reconciliation,
                        store,
                        audit,
                        account_ids=[account_id],
                    ).reconcile_latest(run_id=run_id)
                    latest = latest_snapshot_for_account(store, account_id)
                    status = "refreshed" if reconciliation.passed else "quarantined"
                    return _save_result(
                        store,
                        audit,
                        run_id,
                        source,
                        started_at,
                        AccountRefreshResult(
                            account_id=account_id,
                            status=status,
                            snapshot_id=int(latest["id"]) if latest else None,
                            age_seconds=snapshot_age_seconds(latest),
                            retry_count=retry_count,
                            reconciliation_passed=reconciliation.passed,
                            error_type=(
                                None if reconciliation.passed else "ReconciliationFailed"
                            ),
                            error_message=(
                                None
                                if reconciliation.passed
                                else f"{len(reconciliation.issues)} reconciliation issue(s)"
                            ),
                        ),
                    )
                except Exception as exc:
                    if attempt + 1 >= max(1, attempts) or not _is_transient(exc):
                        raise
                    retry_count += 1
                    time.sleep((1.0, 3.0, 10.0)[min(attempt, 2)])
    except Exception as exc:
        latest = latest_snapshot_for_account(store, account_id)
        return _save_result(
            store,
            audit,
            run_id,
            source,
            started_at,
            AccountRefreshResult(
                account_id=account_id,
                status="quarantined",
                snapshot_id=int(latest["id"]) if latest else None,
                age_seconds=snapshot_age_seconds(latest),
                retry_count=retry_count,
                reconciliation_passed=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            ),
        )


def _save_result(
    store: StateStore,
    audit: AuditLogger,
    run_id: str,
    source: str,
    started_at: float,
    result: AccountRefreshResult,
) -> AccountRefreshResult:
    payload = {
        **result.__dict__,
        "source": source,
        "status": result.status,
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "checked_at": utc_now().isoformat(),
    }
    save_audited_system_event(
        store,
        audit,
        run_id,
        SystemEventType.BROKER_READONLY_REFRESH,
        payload,
    )
    return result


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, URLError, TossRateLimitError)):
        return True
    if isinstance(exc, HTTPError):
        return exc.code >= 500 or exc.code == 429
    cause = exc.__cause__
    if isinstance(cause, Exception) and cause is not exc:
        return _is_transient(cause)
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("timed out", "timeout", "name resolution", "before receiving a response")
    )


def _snapshot_has_passed_reconciliation(
    store: StateStore,
    account_id: str,
    snapshot: dict[str, Any] | None,
) -> bool:
    if snapshot is None:
        return False
    snapshot_id = int(snapshot["id"])
    for event in store.list_system_events_by_type(
        SystemEventType.BROKER_RECONCILIATION,
        limit=1000,
    ):
        payload = event.get("payload") or {}
        for result in payload.get("account_results") or []:
            if (
                str(result.get("account_id") or "") == account_id
                and result.get("passed") is True
                and int(result.get("broker_snapshot_id") or 0) == snapshot_id
            ):
                return True
        if (
            payload.get("passed") is True
            and int(payload.get("broker_snapshot_id") or 0) == snapshot_id
        ):
            return True
    return False


__all__ = [
    "AccountRefreshResult",
    "ReadonlyRefreshReport",
    "latest_snapshot_for_account",
    "refresh_readonly_accounts",
    "required_account_ids_for_strategies",
    "snapshot_age_seconds",
]
