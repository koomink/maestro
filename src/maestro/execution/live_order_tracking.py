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
from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_fills import PartialFillReconciliationService
from maestro.execution.live_order_models import BrokerOrderId, FillReconciliationResult
from maestro.execution.live_order_ports import LiveOrderStatusClient
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
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        # Each brokerage account authenticates separately, so the status client is
        # resolved per order rather than shared.
        self.status_client_for_account = status_client_for_account
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
        """Orders marked outstanding that have not since reached a terminal state."""
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
        return outstanding

    def resume(self, run_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Re-poll outstanding orders and reconcile any fills they picked up."""
        outstanding = self.list_outstanding_orders(limit=limit)
        polled: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        resolved: list[str] = []
        still_open: list[str] = []
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
                self._persist_resolved(run_id, order, snapshot.status)
                resolved.append(order.order_id)
            else:
                still_open.append(order.order_id)

        fill_result: FillReconciliationResult | None = None
        if polled:
            fill_result = self.fill_reconciliation_service.reconcile_latest(run_id)

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
]
