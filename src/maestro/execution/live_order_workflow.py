from typing import TYPE_CHECKING

from maestro.approval.models import ApprovalDecision
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_fills import PartialFillReconciliationService
from maestro.execution.live_order_models import (
    LiveOrderRequest,
    LiveOrderWorkflowResult,
)
from maestro.execution.live_order_ports import BrokerReconciliationRunner
from maestro.execution.live_order_status import LiveOrderStatusService
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore

if TYPE_CHECKING:
    from maestro.execution.live_order_safety import LiveOrderSafetyService


class LiveOrderWorkflowService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
        safety_service: "LiveOrderSafetyService",
        status_service: LiveOrderStatusService,
        fill_reconciliation_service: "PartialFillReconciliationService",
        broker_reconciliation_service: BrokerReconciliationRunner | None = None,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.safety_service = safety_service
        self.status_service = status_service
        self.fill_reconciliation_service = fill_reconciliation_service
        self.broker_reconciliation_service = broker_reconciliation_service

    def run(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderWorkflowResult:
        try:
            result = self._run(request, approval_decision)
        except Exception as exc:
            result = LiveOrderWorkflowResult(
                run_id=request.run_id,
                workflow_status=OrderStatus.FAILED,
                order_id=request.order_id,
                failed_reason=str(exc),
                checked_at=utc_now().isoformat(),
            )
        self._persist_summary(result)
        return result

    def _run(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderWorkflowResult:
        submitted = self.safety_service.submit_approved_order(request, approval_decision)
        broker_order = submitted.broker_order
        if submitted.status == OrderStatus.HALTED or broker_order is None:
            return LiveOrderWorkflowResult(
                run_id=request.run_id,
                workflow_status=OrderStatus.HALTED,
                order_id=request.order_id,
                broker_order_id=broker_order.broker_order_id if broker_order else None,
                submitted_order=submitted,
                halt_reason=submitted.message or "Live order submission halted.",
                checked_at=utc_now().isoformat(),
            )

        status_snapshot = self.status_service.poll_order_status(request.run_id, broker_order)
        if status_snapshot.status == OrderStatus.HALTED:
            return LiveOrderWorkflowResult(
                run_id=request.run_id,
                workflow_status=OrderStatus.HALTED,
                order_id=request.order_id,
                broker_order_id=broker_order.broker_order_id,
                submitted_order=submitted,
                status_snapshot=status_snapshot,
                halt_reason=status_snapshot.message or "Live order status polling halted.",
                checked_at=utc_now().isoformat(),
            )

        fill_result = self.fill_reconciliation_service.reconcile_latest(request.run_id)
        broker_reconciliation = self._reconcile_broker_if_possible()
        if broker_reconciliation is not None and broker_reconciliation.passed is not True:
            return LiveOrderWorkflowResult(
                run_id=request.run_id,
                workflow_status=OrderStatus.FAILED,
                order_id=request.order_id,
                broker_order_id=broker_order.broker_order_id,
                submitted_order=submitted,
                status_snapshot=status_snapshot,
                fill_reconciliation=fill_result,
                broker_reconciliation=broker_reconciliation.model_dump(mode="json"),
                applied_fills=fill_result.applied_fills,
                halt_reason="Broker reconciliation failed after fill reconciliation.",
                checked_at=utc_now().isoformat(),
            )

        return LiveOrderWorkflowResult(
            run_id=request.run_id,
            workflow_status=status_snapshot.status,
            order_id=request.order_id,
            broker_order_id=broker_order.broker_order_id,
            submitted_order=submitted,
            status_snapshot=status_snapshot,
            fill_reconciliation=fill_result,
            broker_reconciliation=broker_reconciliation.model_dump(mode="json")
            if broker_reconciliation
            else None,
            applied_fills=fill_result.applied_fills,
            checked_at=utc_now().isoformat(),
        )

    def _reconcile_broker_if_possible(self) -> ReconciliationResult | None:
        if self.broker_reconciliation_service is None:
            return None
        return self.broker_reconciliation_service.reconcile_latest()

    def _persist_summary(self, result: LiveOrderWorkflowResult) -> None:
        payload = result.model_dump(mode="json")
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            result.run_id,
            SystemEventType.LIVE_ORDER_WORKFLOW,
            payload,
        )


__all__ = ["LiveOrderWorkflowService"]
