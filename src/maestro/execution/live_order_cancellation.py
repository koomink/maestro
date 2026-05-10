from maestro.approval.models import ApprovalDecision
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_models import (
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import LiveOrderCancelClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class LiveOrderCancellationService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
        cancel_client: LiveOrderCancelClient,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.cancel_client = cancel_client

    def cancel_order(
        self,
        request: LiveOrderCancelRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderCancelResult:
        latest_status = self._validate_cancellation_policy(request, approval_decision)
        result = self.cancel_client.cancel_order(request)
        payload = {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "latest_status": latest_status.model_dump(mode="json"),
        }
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            request.run_id,
            SystemEventType.LIVE_ORDER_CANCEL,
            payload,
        )
        return result

    def _validate_cancellation_policy(
        self,
        request: LiveOrderCancelRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderStatusSnapshot:
        if approval_decision.approval_id != request.approval_id:
            raise ValueError("Approval decision does not match the cancel request")
        if approval_decision.run_id != request.run_id:
            raise ValueError("Approval decision run_id does not match the cancel request")
        if approval_decision.status != "approved":
            raise ValueError("Telegram approval is required before live order cancellation")
        if not approval_decision.decided_by.startswith("telegram:"):
            raise ValueError("Telegram approval is required before live order cancellation")
        if self._is_duplicate_cancel(request.broker_order.broker_order_id):
            raise ValueError("Duplicate live order cancellation rejected")
        latest_reconciliation = self.state_store.load_latest_system_event(
            SystemEventType.BROKER_RECONCILIATION
        )
        if (
            latest_reconciliation is None
            or latest_reconciliation["payload"].get("passed") is not True
        ):
            raise ValueError(
                "Latest broker reconciliation must pass before live order cancellation"
            )

        latest_status = self._load_latest_status(request.broker_order.broker_order_id)
        if latest_status is None:
            raise ValueError("Latest live order status is required before cancellation")
        if latest_status.status == OrderStatus.UNKNOWN:
            raise ValueError("Unknown broker state blocks live order cancellation")
        if latest_status.status == OrderStatus.HALTED:
            raise ValueError("Halted live order state blocks cancellation")
        if latest_status.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError(
                f"Live order cancellation is forbidden for status={latest_status.status}"
            )
        if latest_status.partial_fill.remaining_quantity <= 0:
            raise ValueError("Live order cancellation requires a remaining open quantity")
        if latest_status.status == OrderStatus.PARTIALLY_FILLED:
            latest_fill_reconciliation = self.state_store.load_latest_system_event(
                SystemEventType.FILL_RECONCILIATION
            )
            if latest_fill_reconciliation is None:
                raise ValueError(
                    "Latest fill reconciliation is required before partial-fill cancellation"
                )
        return latest_status

    def _load_latest_status(self, broker_order_id: str) -> LiveOrderStatusSnapshot | None:
        rows = self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS, limit=1000
        )
        for row in rows:
            snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
            if snapshot.broker_order.broker_order_id == broker_order_id:
                return snapshot
        return None

    def _is_duplicate_cancel(self, broker_order_id: str) -> bool:
        rows = self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_CANCEL, limit=1000
        )
        for row in rows:
            broker_order = row["payload"].get("request", {}).get("broker_order", {})
            if broker_order.get("broker_order_id") == broker_order_id:
                return True
        return False


__all__ = ["LiveOrderCancellationService"]
