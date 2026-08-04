"""Resume tracking for orders whose status poll window closed while still working.

The lifecycle poll loop is bounded, so an order that has not reached a terminal
state by the last poll is left live at the broker with nobody watching it. Any fill
after that point is invisible to Maestro: fill reconciliation replays recorded
status snapshots, so a fill that was never polled can never be applied, and the
position drifts until someone adopts the broker snapshot by hand.

This module closes that gap by re-polling those orders later. A fresh status
snapshot puts the fill back on the normal reconciliation path, so the position,
cash, and settlement costs land with the same provenance as a fill observed inline.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_fills import PartialFillReconciliationService
from maestro.execution.live_order_models import (
    BrokerOrderId,
    FillReconciliationResult,
    LiveOrderLifecycleNotification,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import (
    LiveOrderNotificationClient,
    LiveOrderStatusClient,
)
from maestro.execution.live_order_status import LiveOrderStatusService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore

TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELED,
    OrderStatus.HALTED,
    OrderStatus.FAILED,
}


def list_unreconciled_live_order_fills(
    state_store: StateStore,
    *,
    min_age_seconds: float = 900.0,
    limit: int = 2000,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return broker-observed live fills that are still absent from the ledger."""
    watermarks = state_store.load_fill_watermarks()
    candidates: dict[str, dict[str, Any]] = {}

    def remember(
        *,
        broker_order_id: str,
        account_id: str,
        symbol: str,
        side: str,
        filled_quantity: float,
        cumulative_notional: float,
        created_at: str,
    ) -> None:
        applied_quantity = float(watermarks.get(broker_order_id, (0.0, 0.0))[0])
        if filled_quantity <= applied_quantity + 1e-9:
            return
        observed_at = _event_timestamp(created_at)
        existing = candidates.get(broker_order_id)
        first_observed_at = min(
            observed_at,
            existing["first_observed_at"] if existing is not None else observed_at,
        )
        missing_quantity = filled_quantity - applied_quantity
        missing_notional = (
            cumulative_notional * missing_quantity / filled_quantity if filled_quantity > 0 else 0.0
        )
        candidates[broker_order_id] = {
            "broker_order_id": broker_order_id,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "filled_quantity": filled_quantity,
            "applied_quantity": applied_quantity,
            "missing_quantity": missing_quantity,
            "missing_notional": missing_notional,
            "first_observed_at": first_observed_at,
        }

    history_rows = state_store.list_system_events_by_type("broker_order_history_item", limit=limit)
    for row in reversed(history_rows):
        payload = row.get("payload") or {}
        if payload.get("history_mode") != "maestro_cost_only":
            continue
        broker_order_id = str(payload.get("broker_order_id") or "")
        if not broker_order_id:
            continue
        remember(
            broker_order_id=broker_order_id,
            account_id=str(payload.get("account_id") or ""),
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            filled_quantity=float(payload.get("filled_quantity") or 0.0),
            cumulative_notional=float(payload.get("cumulative_notional") or 0.0),
            created_at=str(row.get("created_at") or ""),
        )

    status_rows = state_store.list_system_events_by_type(
        SystemEventType.LIVE_ORDER_STATUS, limit=limit
    )
    for row in reversed(status_rows):
        payload = row.get("payload") or {}
        nested = payload.get("broker_order") or {}
        partial_fill = payload.get("partial_fill") or {}
        broker_order_id = str(nested.get("broker_order_id") or "")
        if not broker_order_id or broker_order_id in candidates:
            continue
        filled_quantity = float(partial_fill.get("filled_quantity") or 0.0)
        average_price = float(partial_fill.get("average_fill_price") or 0.0)
        remember(
            broker_order_id=broker_order_id,
            account_id=str(nested.get("account_id") or ""),
            symbol=str(payload.get("symbol") or nested.get("symbol") or ""),
            side=str(payload.get("side") or nested.get("side") or ""),
            filled_quantity=filled_quantity,
            cumulative_notional=filled_quantity * average_price,
            created_at=str(row.get("created_at") or ""),
        )

    checked_at = now or datetime.now(UTC)
    output = []
    for candidate in candidates.values():
        age_seconds = max(
            (checked_at - candidate["first_observed_at"]).total_seconds(),
            0.0,
        )
        if age_seconds < min_age_seconds:
            continue
        output.append(
            {
                **candidate,
                "first_observed_at": candidate["first_observed_at"].isoformat(),
                "age_seconds": age_seconds,
            }
        )
    return sorted(output, key=lambda item: (-item["age_seconds"], item["broker_order_id"]))


def _event_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class OutstandingOrder:
    """An order recorded as outstanding, with the context needed to re-poll it."""

    def __init__(
        self,
        *,
        event_id: int,
        run_id: str,
        order_id: str,
        broker_order: BrokerOrderId,
        last_status: str,
    ) -> None:
        self.event_id = event_id
        self.run_id = run_id
        self.order_id = order_id
        self.broker_order = broker_order
        self.last_status = last_status


class LiveOrderTrackingResumeService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
        status_client_for_account: "Callable[[str | None], LiveOrderStatusClient]",
        *,
        fill_reconciliation_service: PartialFillReconciliationService | None = None,
        notification_client: LiveOrderNotificationClient | None = None,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        # Each brokerage account authenticates separately, so the status client is
        # resolved per order rather than shared.
        self.status_client_for_account = status_client_for_account
        self.notification_client = notification_client
        self.fill_reconciliation_service = (
            fill_reconciliation_service
            or PartialFillReconciliationService(state_store, audit_logger)
        )
        self._status_services: dict[str | None, LiveOrderStatusService] = {}

    def _status_service_for(self, account_id: str | None) -> LiveOrderStatusService:
        service = self._status_services.get(account_id)
        if service is None:
            service = LiveOrderStatusService(
                self.state_store,
                self.audit_logger,
                self.status_client_for_account(account_id),
            )
            self._status_services[account_id] = service
        return service

    def list_outstanding_orders(self, *, limit: int = 100) -> list[OutstandingOrder]:
        """Accepted orders without a later terminal status snapshot.

        Tracking-incomplete events are the normal source. Accepted submission
        results are also scanned so orders created before that event existed, or
        abandoned by an exceptional lifecycle exit, remain recoverable.
        """
        rows = self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_TRACKING_INCOMPLETE, limit=limit
        )
        resolved = self._resolved_broker_order_ids(limit=max(limit, 1000))
        outstanding: list[OutstandingOrder] = []
        seen: set[str] = set()
        for row in rows:
            payload = row["payload"]
            broker_order_payload = payload.get("broker_order")
            if not isinstance(broker_order_payload, dict):
                continue
            broker_order = BrokerOrderId.model_validate(broker_order_payload)
            broker_order_id = broker_order.broker_order_id
            # Events are newest first, so the first sighting is the current one.
            if broker_order_id in seen:
                continue
            seen.add(broker_order_id)
            if broker_order_id in resolved:
                continue
            outstanding.append(
                OutstandingOrder(
                    event_id=int(row["id"]),
                    run_id=str(row["run_id"]),
                    order_id=str(payload.get("order_id") or broker_order.order_id),
                    broker_order=broker_order,
                    last_status=str(payload.get("last_status") or OrderStatus.OPEN.value),
                )
            )
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=max(limit, 1000)
        ):
            payload = row.get("payload") or {}
            result_payload = payload.get("result") or {}
            broker_order_payload = result_payload.get("broker_order")
            if not isinstance(broker_order_payload, dict):
                continue
            broker_order = BrokerOrderId.model_validate(broker_order_payload)
            broker_order_id = broker_order.broker_order_id
            if broker_order_id in seen or broker_order_id in resolved:
                continue
            status = str(result_payload.get("status") or "")
            if status in {value.value for value in TERMINAL_ORDER_STATUSES}:
                continue
            seen.add(broker_order_id)
            outstanding.append(
                OutstandingOrder(
                    event_id=int(row["id"]),
                    run_id=str(row["run_id"]),
                    order_id=str(result_payload.get("order_id") or broker_order.order_id),
                    broker_order=broker_order,
                    last_status=status or OrderStatus.ACCEPTED_BY_BROKER.value,
                )
            )
            if len(outstanding) >= limit:
                break
        return outstanding

    def resume(self, run_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Re-poll outstanding orders and reconcile any fills they picked up."""
        outstanding = self.list_outstanding_orders(limit=limit)
        polled: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        resolved: list[str] = []
        still_open: list[str] = []
        terminal: list[tuple[OutstandingOrder, LiveOrderStatusSnapshot]] = []
        for order in outstanding:
            try:
                status_service = self._status_service_for(order.broker_order.account_id)
                snapshot = status_service.poll_order_status(run_id, order.broker_order)
            except Exception as exc:
                # One unreachable order must not stop the others from being recovered.
                failures.append(
                    {
                        "order_id": order.order_id,
                        "broker_order_id": order.broker_order.broker_order_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                continue
            polled.append(
                {
                    "order_id": order.order_id,
                    "broker_order_id": order.broker_order.broker_order_id,
                    "previous_status": order.last_status,
                    "status": snapshot.status.value,
                    "filled_quantity": snapshot.partial_fill.filled_quantity,
                    "remaining_quantity": snapshot.partial_fill.remaining_quantity,
                }
            )
            if snapshot.status in TERMINAL_ORDER_STATUSES:
                terminal.append((order, snapshot))
            else:
                still_open.append(order.order_id)

        fill_result: FillReconciliationResult | None = None
        if polled or self._has_unreconciled_statuses(limit=max(limit, 1000)):
            fill_result = self.fill_reconciliation_service.reconcile_latest(run_id)
        # A terminal broker status is not operationally resolved until its fill is
        # safely in the ledger. Persisting this marker first can make a failed
        # reconciliation invisible to every later resume run.
        for order, snapshot in terminal:
            self._persist_resolved(run_id, order, snapshot.status)
            resolved.append(order.order_id)
            self._notify(
                run_id,
                order,
                snapshot.status,
                f"Live order reached {snapshot.status.value} after tracking resumed; "
                f"filled {snapshot.partial_fill.filled_quantity:g}"
                f"/{snapshot.partial_fill.ordered_quantity:g}.",
            )

        summary = {
            "checked_at": utc_now().isoformat(),
            "outstanding_orders": len(outstanding),
            "polled": polled,
            "resolved_order_ids": resolved,
            "still_open_order_ids": still_open,
            "failures": failures,
            "applied_fills": (
                [fill.model_dump(mode="json") for fill in fill_result.applied_fills]
                if fill_result is not None
                else []
            ),
        }
        return summary

    def _has_unreconciled_statuses(self, *, limit: int) -> bool:
        watermarks = self.state_store.load_fill_watermarks()
        cost_watermarks = self.state_store.load_fill_cost_watermarks()
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS, limit=limit
        ):
            payload = row.get("payload") or {}
            nested = payload.get("broker_order") or {}
            broker_order_id = str(nested.get("broker_order_id") or "")
            if not broker_order_id:
                continue
            partial_fill = payload.get("partial_fill") or {}
            filled_quantity = float(partial_fill.get("filled_quantity") or 0.0)
            applied_quantity = float(watermarks.get(broker_order_id, (0.0, 0.0))[0])
            if filled_quantity > applied_quantity + 1e-9:
                return True
            commission = float(payload.get("cumulative_commission") or 0.0)
            tax = float(payload.get("cumulative_tax") or 0.0)
            applied_commission, applied_tax = cost_watermarks.get(broker_order_id, (0.0, 0.0))
            if commission > applied_commission + 1e-9 or tax > applied_tax + 1e-9:
                return True
        return False

    def _notify(
        self,
        run_id: str,
        order: OutstandingOrder,
        status: OrderStatus,
        message: str,
    ) -> None:
        if self.notification_client is None:
            return
        try:
            self.notification_client.notify(
                LiveOrderLifecycleNotification(
                    run_id=run_id,
                    order_id=order.order_id,
                    status=status,
                    message=message,
                    broker_order_id=order.broker_order.broker_order_id,
                )
            )
        except Exception as exc:
            # A dropped message must not lose the fill that was just recovered.
            save_audited_system_event(
                self.state_store,
                self.audit_logger,
                run_id,
                "live_order_notification_failed",
                {
                    "order_id": order.order_id,
                    "status": status.value,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )

    def _resolved_broker_order_ids(self, *, limit: int) -> set[str]:
        """Broker order ids that no longer need re-polling.

        system_events only indexes broker_order_id for result-shaped payloads, and
        both event types read here nest the broker order instead, so these are
        matched on the payload rather than the column.
        """
        resolved: set[str] = set()
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_TRACKING_RESOLVED, limit=limit
        ):
            broker_order_id = row["payload"].get("broker_order_id")
            if broker_order_id:
                resolved.add(str(broker_order_id))

        terminal_values = {status.value for status in TERMINAL_ORDER_STATUSES}
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS, limit=limit
        ):
            payload = row["payload"]
            if payload.get("status") not in terminal_values:
                continue
            nested = payload.get("broker_order")
            if isinstance(nested, dict) and nested.get("broker_order_id"):
                resolved.add(str(nested["broker_order_id"]))
        return resolved

    def _persist_resolved(
        self,
        run_id: str,
        order: OutstandingOrder,
        final_status: OrderStatus,
    ) -> None:
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            run_id,
            SystemEventType.LIVE_ORDER_TRACKING_RESOLVED,
            {
                "order_id": order.order_id,
                "broker_order_id": order.broker_order.broker_order_id,
                "broker_order": order.broker_order.model_dump(mode="json"),
                "final_status": final_status.value,
                "tracking_incomplete_event_id": order.event_id,
                "checked_at": utc_now().isoformat(),
            },
        )


__all__ = [
    "LiveOrderTrackingResumeService",
    "OutstandingOrder",
    "TERMINAL_ORDER_STATUSES",
    "list_unreconciled_live_order_fills",
]
