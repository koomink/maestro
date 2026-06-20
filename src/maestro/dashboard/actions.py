from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from maestro.config.loader import load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.core.ids import new_run_id
from maestro.execution.brokers.readonly_factory import (
    broker_readonly_accounts,
    build_broker_readonly_services,
)
from maestro.fx.service import ConfiguredFXRefreshService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


@dataclass(frozen=True)
class DashboardRefreshResult:
    status: str
    accounts_synced: int
    signal_freshness: dict[str, Any]
    fx_refresh: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounts_synced": self.accounts_synced,
            "signal_freshness": self.signal_freshness,
            "fx_refresh": self.fx_refresh,
        }


def refresh_dashboard_state(config_path: str | Path) -> DashboardRefreshResult:
    config, identity = load_config_with_identity(config_path)
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )
    audit = AuditLogger(config.audit.jsonl_path)
    accounts_synced = 0
    if config.mode in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        refresh_run_id = new_run_id()
        for logical_account_id, service in build_broker_readonly_services(config, store, audit):
            account_label = logical_account_id or "default_kis"
            try:
                service.fetch_and_store_snapshot(
                    config.portfolio.allowed_symbols,
                    run_id=refresh_run_id,
                )
            except ValueError as exc:
                raise ValueError(f"Failed to refresh account {account_label}: {exc}") from exc
            accounts_synced += 1
    return DashboardRefreshResult(
        status="ok",
        accounts_synced=accounts_synced,
        signal_freshness=build_signal_freshness(
            store,
            max_age_seconds=config.approval.signal_max_age_seconds,
        ),
        fx_refresh=_refresh_fx_nonblocking(config, store),
    )


def generate_strategy_signal(config_path: str | Path, strategy_id: str) -> dict[str, Any]:
    config, identity = load_config_with_identity(config_path)
    matching = [strategy for strategy in config.strategies if strategy.id == strategy_id]
    if not matching:
        raise ValueError(f"Unknown strategy_id: {strategy_id}")
    strategy = matching[0]
    if not strategy.enabled or not strategy.signal_enabled:
        raise ValueError(f"Strategy is not signal-enabled: {strategy_id}")
    summary = MaestroOrchestrator(config, config_identity=identity).run_signal(
        strategy_ids=[strategy_id],
    )
    return {
        "status": "ok",
        "strategy_id": strategy_id,
        "signal_run_id": summary.signal_run_id,
        "loaded_strategies": summary.loaded_strategies,
        "action_required": summary.action_required,
        "orders_preview_count": summary.orders_preview_count,
    }


def build_signal_freshness(store: StateStore, *, max_age_seconds: int) -> dict[str, Any]:
    rows = store.list_system_events_by_type("signal_package", limit=20)
    if not rows:
        return {"overall": "missing", "strategies": []}
    now = utc_now()
    sorted_rows = sorted(
        rows,
        key=lambda row: _created_at_sort_key(row.get("created_at"), now),
        reverse=True,
    )
    strategies = []
    for row in sorted_rows:
        payload = row.get("payload") or {}
        created_at = row.get("created_at")
        age_seconds = _age_seconds(created_at, now)
        status = "fresh" if age_seconds is not None and age_seconds <= max_age_seconds else "stale"
        if payload.get("status") == "failed":
            status = "failed"
        for strategy_id in payload.get("loaded_strategies") or []:
            if any(item["strategy_id"] == strategy_id for item in strategies):
                continue
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "status": status,
                    "latest_signal_run_id": payload.get("signal_run_id") or row.get("run_id"),
                    "latest_signal_at": created_at,
                    "age_seconds": age_seconds,
                    "max_age_seconds": max_age_seconds,
                }
            )
    overall = _overall_signal_status([item["status"] for item in strategies])
    return {"overall": overall, "strategies": strategies}


def _kis_accounts(config) -> list[tuple[str | None, Any]]:
    return [
        (
            account_id,
            account.to_kis_config() if getattr(account, "broker", None) == "kis" else account,
        )
        for account_id, account in broker_readonly_accounts(config)
    ]


def _refresh_fx_nonblocking(config, store: StateStore) -> dict[str, Any]:
    try:
        result = ConfiguredFXRefreshService(config, store).refresh_from_config()
    except Exception as exc:
        return {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")


def _created_at_sort_key(created_at: Any, now: datetime) -> datetime:
    return _parse_created_at(created_at, now) or datetime.min.replace(tzinfo=now.tzinfo)


def _age_seconds(created_at: Any, now: datetime) -> int | None:
    created = _parse_created_at(created_at, now)
    if created is None:
        return None
    return max(0, int((now - created).total_seconds()))


def _parse_created_at(created_at: Any, now: datetime) -> datetime | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)
    return created


def _overall_signal_status(statuses: list[str]) -> str:
    if not statuses:
        return "missing"
    for status in ("failed", "stale", "missing", "fresh"):
        if status in statuses:
            return status
    return "missing"
