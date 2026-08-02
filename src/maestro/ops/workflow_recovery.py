from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus, RunMode
from maestro.core.ids import new_run_id
from maestro.execution.broker_router import BrokerAccountRouter
from maestro.execution.brokers.readonly_factory import (
    broker_readonly_account_ids,
    broker_readonly_accounts,
    build_broker_readonly_service,
)
from maestro.execution.brokers.toss.live_order_client import TossLiveOrderClient
from maestro.execution.live_order_factory import build_live_order_status_client
from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderRequest,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import LiveOrderStatusClient
from maestro.execution.live_orders import PartialFillReconciliationService
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class RecoveryBlocker(BaseModel):
    event_id: int
    event_type: str
    run_id: str
    created_at: str
    reason: str
    detail_reason: str | None = None
    order_id: str | None = None
    account_id: str | None = None
    broker_order_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)


class RecoveryPreview(BaseModel):
    blockers: list[RecoveryBlocker] = Field(default_factory=list)
    fingerprint: str


class WorkflowRecoveryResult(BaseModel):
    status: Literal["completed", "attestation_required"]
    run_id: str
    fingerprint: str
    resolved_orders: list[dict[str, Any]] = Field(default_factory=list)
    unmatched_orders: list[dict[str, Any]] = Field(default_factory=list)
    applied_fill_count: int = 0
    broker_snapshot_ids: list[int] = Field(default_factory=list)
    broker_reconciliation_event_id: int | None = None


class WorkflowRecoveryService:
    def __init__(
        self,
        config: MaestroConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        *,
        now_fn: Callable[[], datetime] = utc_now,
        status_client_for_account: Callable[[str | None], LiveOrderStatusClient]
        | None = None,
    ) -> None:
        self.config = config
        self.store = state_store
        self.audit = audit_logger
        self._now = now_fn
        self._status_client_for_account = status_client_for_account

    def preview(self) -> RecoveryPreview:
        blockers = self._unresolved_blockers()
        return RecoveryPreview(
            blockers=blockers,
            fingerprint=_recovery_fingerprint(blockers),
        )

    def recover_live_orders(
        self,
        *,
        reason: str,
        decided_by: str,
        expected_fingerprint: str | None = None,
        manual_attestation: bool = False,
        allow_without_blockers: bool = False,
    ) -> WorkflowRecoveryResult:
        if self.config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
            raise ValueError("live-order recovery requires live_readonly or live_approval")
        with self.store.live_order_lock("workflow_recovery"):
            preview = self.preview()
            if expected_fingerprint is not None and preview.fingerprint != expected_fingerprint:
                raise ValueError("Recovery state changed; open /recovery again")
            if not preview.blockers and not allow_without_blockers:
                raise ValueError("No live-order recovery is pending")

            run_id = new_run_id()
            resolved, unmatched = self._resolve_orders(run_id, preview.blockers)
            if unmatched and not manual_attestation:
                return WorkflowRecoveryResult(
                    status="attestation_required",
                    run_id=run_id,
                    fingerprint=preview.fingerprint,
                    resolved_orders=resolved,
                    unmatched_orders=unmatched,
                )
            if unmatched:
                self._persist_attestation(
                    run_id,
                    preview,
                    unmatched,
                    decided_by=decided_by,
                )

            fill_result = PartialFillReconciliationService(
                self.store,
                self.audit,
            ).reconcile_latest(run_id)
            account_ids = self._recovery_account_ids(preview.blockers)
            snapshot_ids = self._refresh_snapshots(run_id, account_ids)
            reconciliation = BrokerReconciliationService(
                self.config.reconciliation,
                self.store,
                self.audit,
                account_ids=account_ids or None,
            ).reconcile_latest(run_id=run_id)
            if not reconciliation.passed:
                raise ValueError(
                    f"Broker reconciliation failed with {len(reconciliation.issues)} issue(s)"
                )
            reconciliation_event = self.store.load_latest_system_event(
                SystemEventType.BROKER_RECONCILIATION
            )
            reconciliation_event_id = (
                int(reconciliation_event["id"]) if reconciliation_event is not None else None
            )
            payload = {
                "reason": reason,
                "decided_by": decided_by,
                "recovery_fingerprint": preview.fingerprint,
                "recovery_event_ids": [blocker.event_id for blocker in preview.blockers],
                "resolved_orders": resolved,
                "manual_attestation": bool(unmatched),
                "unmatched_orders": unmatched,
                "broker_snapshot_ids": snapshot_ids,
                "broker_reconciliation_event_id": reconciliation_event_id,
                "fill_reconciliation": fill_result.model_dump(mode="json"),
            }
            save_audited_system_event(
                self.store,
                self.audit,
                run_id,
                SystemEventType.LIVE_ORDER_RECOVERY_COMPLETED,
                payload,
            )
            return WorkflowRecoveryResult(
                status="completed",
                run_id=run_id,
                fingerprint=preview.fingerprint,
                resolved_orders=resolved,
                unmatched_orders=unmatched,
                applied_fill_count=len(fill_result.applied_fills),
                broker_snapshot_ids=snapshot_ids,
                broker_reconciliation_event_id=reconciliation_event_id,
            )

    def _unresolved_blockers(self) -> list[RecoveryBlocker]:
        latest_completion = self.store.load_latest_system_event(
            SystemEventType.LIVE_ORDER_RECOVERY_COMPLETED
        )
        completed_after = int(latest_completion["id"]) if latest_completion else 0
        blockers: list[RecoveryBlocker] = []
        seen_order_ids: set[str] = set()

        required_rows = self.store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
            limit=1000,
        )
        for row in reversed(required_rows):
            if int(row["id"]) <= completed_after:
                continue
            blocker = _blocker_from_event(row, "live_order_recovery_required")
            blockers.append(blocker)
            if blocker.order_id:
                seen_order_ids.add(blocker.order_id)

        intent_rows = self.store.list_system_events_by_type(
            "live_order_submit_intent", limit=1000
        )
        for row in reversed(intent_rows):
            if int(row["id"]) <= completed_after:
                continue
            payload = row["payload"]
            duplicate_key = str(payload.get("duplicate_key") or "")
            if not duplicate_key.startswith("intent:"):
                continue
            if self.store.duplicate_key_exists(duplicate_key.removeprefix("intent:")):
                continue
            blocker = _blocker_from_event(row, "live_order_intent_without_result")
            if blocker.order_id not in seen_order_ids:
                blockers.append(blocker)
                if blocker.order_id:
                    seen_order_ids.add(blocker.order_id)

        lifecycle_order_ids = self.store.list_order_ids_for_event_type(
            str(SystemEventType.LIVE_ORDER_LIFECYCLE)
        )
        result_rows = self.store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        )
        for row in reversed(result_rows):
            if int(row["id"]) <= completed_after:
                continue
            blocker = _blocker_from_event(row, "live_order_lifecycle_incomplete")
            if (
                blocker.order_id
                and blocker.order_id not in lifecycle_order_ids
                and blocker.order_id not in seen_order_ids
            ):
                blockers.append(blocker)
                seen_order_ids.add(blocker.order_id)
        return sorted(blockers, key=lambda blocker: blocker.event_id)

    def _resolve_orders(
        self,
        run_id: str,
        blockers: list[RecoveryBlocker],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        resolved: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        toss_clients: dict[str, TossLiveOrderClient] = {}
        history_cache: dict[tuple[str, str, date], list[LiveOrderStatusSnapshot]] = {}
        for blocker in blockers:
            if not blocker.request or not blocker.order_id:
                unmatched.append(_unmatched_payload(blocker, []))
                continue
            try:
                request = LiveOrderRequest.model_validate(blocker.request)
            except ValueError:
                unmatched.append(_unmatched_payload(blocker, []))
                continue
            account = BrokerAccountRouter(self.config).account(request.account_id)
            client = None
            if account is not None and account.broker == "toss":
                client = toss_clients.setdefault(
                    account.id,
                    TossLiveOrderClient(account, self.config.universe.instruments),
                )
            snapshot: LiveOrderStatusSnapshot | None = None
            candidates: list[LiveOrderStatusSnapshot] = []
            if blocker.broker_order_id:
                status_client = client or self._status_client(request.account_id)
                broker_order = BrokerOrderId(
                    broker=account.broker if account is not None else "kis",
                    broker_order_id=blocker.broker_order_id,
                    order_id=request.order_id,
                    submitted_at=blocker.created_at,
                    account_id=request.account_id,
                    broker_product=request.broker_product,
                )
                snapshot = status_client.get_order_status(broker_order)
            elif client is not None:
                submitted_date = _parse_datetime(blocker.created_at).date()
                key = (account.id, request.symbol, submitted_date)
                if key not in history_cache:
                    broker_symbol = client._broker_symbol(request.symbol)
                    history_cache[key] = _unique_order_snapshots(
                        [
                            *client.list_orders(status="OPEN", symbol=broker_symbol),
                            *client.list_orders(
                                status="CLOSED",
                                symbol=broker_symbol,
                                from_date=submitted_date,
                                to_date=self._now().date(),
                            ),
                        ]
                    )
                candidates = history_cache[key]
                matches = [
                    candidate
                    for candidate in candidates
                    if _matches_request(candidate, request, blocker.created_at)
                ]
                if len(matches) == 1:
                    snapshot = matches[0]
            if snapshot is None or snapshot.status in {OrderStatus.UNKNOWN, OrderStatus.HALTED}:
                unmatched.append(_unmatched_payload(blocker, candidates))
                continue
            snapshot = _bind_snapshot(snapshot, request, blocker.created_at)
            self._persist_resolution(run_id, blocker, snapshot)
            resolved.append(
                {
                    "event_id": blocker.event_id,
                    "order_id": request.order_id,
                    "broker_order_id": snapshot.broker_order.broker_order_id,
                    "status": snapshot.status.value,
                }
            )
            if snapshot.status in {
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.ACCEPTED_BY_BROKER,
            }:
                self._persist_tracking_incomplete(run_id, request, snapshot)
        return resolved, unmatched

    def _status_client(self, account_id: str | None) -> LiveOrderStatusClient:
        if self._status_client_for_account is not None:
            return self._status_client_for_account(account_id)
        return build_live_order_status_client(self.config, account_id=account_id)

    def _persist_resolution(
        self,
        run_id: str,
        blocker: RecoveryBlocker,
        snapshot: LiveOrderStatusSnapshot,
    ) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            run_id,
            SystemEventType.LIVE_ORDER_STATUS,
            snapshot.model_dump(mode="json"),
        )
        save_audited_system_event(
            self.store,
            self.audit,
            run_id,
            SystemEventType.LIVE_ORDER_RECOVERY_RESOLUTION,
            {
                "source_event_id": blocker.event_id,
                "source_event_type": blocker.event_type,
                "order_id": blocker.order_id,
                "broker_order_id": snapshot.broker_order.broker_order_id,
                "status": snapshot.status.value,
                "checked_at": snapshot.checked_at,
            },
        )

    def _persist_tracking_incomplete(
        self,
        run_id: str,
        request: LiveOrderRequest,
        snapshot: LiveOrderStatusSnapshot,
    ) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            run_id,
            SystemEventType.LIVE_ORDER_TRACKING_INCOMPLETE,
            {
                "reason": "recovered_order_still_open",
                "order_id": request.order_id,
                "broker_order": snapshot.broker_order.model_dump(mode="json"),
                "last_status": snapshot.status.value,
                "poll_count": 1,
            },
        )

    def _refresh_snapshots(self, run_id: str, account_ids: list[str]) -> list[int]:
        selected: list[str | None] = (
            list(account_ids)
            if account_ids
            else [account_id for account_id, _ in broker_readonly_accounts(self.config)]
        )
        snapshot_ids: list[int] = []
        for account_id in selected:
            service = build_broker_readonly_service(
                self.config,
                self.store,
                self.audit,
                account_id=account_id,
            )
            try:
                service.fetch_and_store_snapshot(
                    self.config.portfolio.allowed_symbols,
                    run_id=run_id,
                )
            except TypeError as exc:
                if "unexpected keyword argument 'run_id'" not in str(exc):
                    raise
                service.fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)
            latest = self.store.load_latest_broker_account_snapshot()
            if latest is None:
                raise ValueError(
                    f"Broker snapshot refresh did not persist: {account_id or 'default'}"
                )
            snapshot_ids.append(int(latest["id"]))
        if not snapshot_ids:
            raise ValueError("No read-only broker account is configured for recovery")
        return snapshot_ids

    def _recovery_account_ids(self, blockers: list[RecoveryBlocker]) -> list[str]:
        ids = {blocker.account_id for blocker in blockers if blocker.account_id}
        return sorted(ids) if ids else broker_readonly_account_ids(self.config)

    def _persist_attestation(
        self,
        run_id: str,
        preview: RecoveryPreview,
        unmatched: list[dict[str, Any]],
        *,
        decided_by: str,
    ) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            run_id,
            SystemEventType.LIVE_ORDER_RECOVERY_ATTESTATION,
            {
                "decided_by": decided_by,
                "confirmation": "broker_order_and_fill_absence_verified",
                "recovery_fingerprint": preview.fingerprint,
                "recovery_event_ids": [blocker.event_id for blocker in preview.blockers],
                "unmatched_orders": unmatched,
                "confirmed_at": self._now().isoformat(),
            },
        )


def _blocker_from_event(row: dict[str, Any], reason: str) -> RecoveryBlocker:
    payload = row.get("payload") or {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    broker_order = (
        result.get("broker_order") if isinstance(result.get("broker_order"), dict) else {}
    )
    order_id = str(request.get("order_id") or payload.get("order_id") or "") or None
    return RecoveryBlocker(
        event_id=int(row["id"]),
        event_type=str(row["event_type"]),
        run_id=str(row["run_id"]),
        created_at=str(row["created_at"]),
        reason=reason,
        detail_reason=str(payload.get("reason") or "") or None,
        order_id=order_id,
        account_id=str(request.get("account_id") or "") or None,
        broker_order_id=str(
            broker_order.get("broker_order_id") or payload.get("broker_order_id") or ""
        )
        or None,
        request=request,
    )


def _recovery_fingerprint(blockers: list[RecoveryBlocker]) -> str:
    encoded = json.dumps(
        [(blocker.event_id, blocker.event_type) for blocker in blockers],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _matches_request(
    snapshot: LiveOrderStatusSnapshot,
    request: LiveOrderRequest,
    submitted_at: str,
) -> bool:
    raw = snapshot.raw.get("result") if isinstance(snapshot.raw, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    ordered_at = raw.get("orderedAt")
    if not ordered_at:
        return False
    delta = _parse_datetime(str(ordered_at)) - _parse_datetime(submitted_at)
    if abs(delta.total_seconds()) > 300:
        return False
    if snapshot.symbol != request.symbol or snapshot.side != request.side:
        return False
    if abs(snapshot.partial_fill.ordered_quantity - request.quantity) > 1e-9:
        return False
    raw_price = raw.get("price")
    return raw_price is not None and abs(float(raw_price) - request.limit_price) <= 1e-9


def _bind_snapshot(
    snapshot: LiveOrderStatusSnapshot,
    request: LiveOrderRequest,
    submitted_at: str,
) -> LiveOrderStatusSnapshot:
    broker_order = snapshot.broker_order.model_copy(
        update={
            "order_id": request.order_id,
            "submitted_at": submitted_at,
            "account_id": request.account_id,
            "broker_product": request.broker_product,
        }
    )
    return snapshot.model_copy(update={"broker_order": broker_order})


def _unmatched_payload(
    blocker: RecoveryBlocker,
    candidates: list[LiveOrderStatusSnapshot],
) -> dict[str, Any]:
    return {
        "event_id": blocker.event_id,
        "order_id": blocker.order_id,
        "account_id": blocker.account_id,
        "reason": blocker.detail_reason or blocker.reason,
        "candidate_orders": [
            {
                "broker_order_id": candidate.broker_order.broker_order_id,
                "symbol": candidate.symbol,
                "side": candidate.side.value if candidate.side is not None else None,
                "quantity": candidate.partial_fill.ordered_quantity,
                "status": candidate.status.value,
                "ordered_at": (candidate.raw.get("result") or {}).get("orderedAt"),
            }
            for candidate in candidates
        ],
    }


def _unique_order_snapshots(
    snapshots: list[LiveOrderStatusSnapshot],
) -> list[LiveOrderStatusSnapshot]:
    output: dict[str, LiveOrderStatusSnapshot] = {}
    for snapshot in snapshots:
        output[snapshot.broker_order.broker_order_id] = snapshot
    return list(output.values())


__all__ = [
    "RecoveryBlocker",
    "RecoveryPreview",
    "WorkflowRecoveryResult",
    "WorkflowRecoveryService",
]
