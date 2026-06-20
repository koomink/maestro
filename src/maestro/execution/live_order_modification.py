from collections.abc import Callable

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_models import (
    LiveOrderModifyRequest,
    LiveOrderModifyResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import LiveOrderModifyClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class LiveOrderModificationService:
    def __init__(
        self,
        config: ExecutionConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        modify_client: LiveOrderModifyClient,
        *,
        attribution_validator: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.modify_client = modify_client
        self.attribution_validator = attribution_validator

    def modify_order(
        self,
        request: LiveOrderModifyRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderModifyResult:
        latest_status = self._validate(request, approval_decision)
        result = self.modify_client.modify_order(request)
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            request.run_id,
            "live_order_modify",
            {
                "request": request.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "latest_status": latest_status.model_dump(mode="json"),
                "previous_broker_order_id": request.broker_order.broker_order_id,
                "replacement_broker_order_id": result.broker_order.broker_order_id,
            },
        )
        return result

    def _validate(
        self,
        request: LiveOrderModifyRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderStatusSnapshot:
        if not self.config.live_order_enabled:
            raise ValueError("Live order modification is disabled")
        if approval_decision.approval_id != request.approval_id:
            raise ValueError("Approval decision does not match the modify request")
        if approval_decision.run_id != request.run_id:
            raise ValueError("Approval decision run_id does not match the modify request")
        if (
            approval_decision.status != "approved"
            or not approval_decision.decided_by.startswith("telegram:")
        ):
            raise ValueError("Telegram approval is required before live order modification")
        if self._is_duplicate(request.broker_order.broker_order_id):
            raise ValueError("Duplicate live order modification rejected")
        latest_reconciliation = self.state_store.load_latest_system_event(
            SystemEventType.BROKER_RECONCILIATION
        )
        if (
            latest_reconciliation is None
            or latest_reconciliation["payload"].get("passed") is not True
        ):
            raise ValueError(
                "Latest broker reconciliation must pass before live order modification"
            )
        latest_status = self._load_latest_status(request.broker_order.broker_order_id)
        if latest_status is None:
            raise ValueError("Latest live order status is required before modification")
        if latest_status.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError(
                f"Live order modification is forbidden for status={latest_status.status}"
            )
        remaining = latest_status.partial_fill.remaining_quantity
        quantity = request.quantity if request.quantity is not None else remaining
        if quantity <= 0:
            raise ValueError("Live order modification requires a remaining open quantity")
        max_notional = self.config.live_order_limits.max_order_notional_for(request.currency)
        if max_notional is None:
            raise ValueError("Live order modification currency is missing a per-order cap")
        if quantity * request.limit_price > max_notional:
            raise ValueError("Live order modification exceeds per-order cap")
        if self.attribution_validator is not None:
            account_id = request.broker_order.account_id
            if not account_id:
                raise ValueError("Live order modification requires account_id")
            self.attribution_validator(account_id)
        return latest_status

    def _load_latest_status(self, broker_order_id: str) -> LiveOrderStatusSnapshot | None:
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS,
            limit=2000,
        ):
            snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
            if snapshot.broker_order.broker_order_id == broker_order_id:
                return snapshot
        return None

    def _is_duplicate(self, broker_order_id: str) -> bool:
        for row in self.state_store.list_system_events_by_type(
            "live_order_modify",
            limit=2000,
        ):
            if row["payload"].get("previous_broker_order_id") == broker_order_id:
                return True
        return False


__all__ = ["LiveOrderModificationService"]
