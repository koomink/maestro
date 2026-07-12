from collections.abc import Callable
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_fills import PartialFillReconciliationService
from maestro.execution.live_order_models import (
    AppliedFill,
    FillReconciliationResult,
    LiveOrderLifecycleNotification,
    LiveOrderLifecycleResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import (
    BrokerReconciliationRunner,
    LiveOrderNotificationClient,
)
from maestro.execution.live_order_status import LiveOrderStatusService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore

if TYPE_CHECKING:
    from maestro.execution.live_order_safety import LiveOrderSafetyService


class LiveOrderLifecycleService:
    def __init__(
        self,
        config: ExecutionConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        safety_service: "LiveOrderSafetyService",
        status_service: LiveOrderStatusService,
        fill_reconciliation_service: "PartialFillReconciliationService",
        broker_reconciliation_service: BrokerReconciliationRunner | None = None,
        notification_client: LiveOrderNotificationClient | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.safety_service = safety_service
        self.status_service = status_service
        self.fill_reconciliation_service = fill_reconciliation_service
        self.broker_reconciliation_service = broker_reconciliation_service
        self.notification_client = notification_client
        self.sleep_fn = sleep_fn or sleep

    def run(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderLifecycleResult:
        notifications: list[LiveOrderLifecycleNotification] = []
        status_snapshots: list[LiveOrderStatusSnapshot] = []
        fill_reconciliations: list[FillReconciliationResult] = []
        broker_reconciliations: list[dict[str, Any]] = []
        applied_fills: list[AppliedFill] = []
        submitted_order: LiveOrderResult | None = None
        broker_order_id: str | None = None
        try:
            self._notify(
                notifications,
                request.run_id,
                request.order_id,
                OrderStatus.SUBMITTED,
                "Live order submitted to safety service.",
            )
            submitted_order = self.safety_service.submit_approved_order(request, approval_decision)
            broker_order = submitted_order.broker_order
            broker_order_id = broker_order.broker_order_id if broker_order else None
            self._notify(
                notifications,
                request.run_id,
                request.order_id,
                submitted_order.status,
                "Live order submission result received.",
                broker_order_id,
            )
            if submitted_order.status == OrderStatus.HALTED or broker_order is None:
                result = self._result(
                    request=request,
                    final_status=OrderStatus.HALTED,
                    submitted_order=submitted_order,
                    broker_order_id=broker_order_id,
                    notifications_sent=notifications,
                    halt_reason=submitted_order.message or "Live order submission halted.",
                )
                self._persist_summary(result)
                return result

            max_polls_reached = False
            poll_count = 0
            final_status = submitted_order.status
            poll_started_at = monotonic()
            for poll_index in range(self.config.order_status_max_polls):
                if (
                    self.config.order_status_terminal_timeout_seconds > 0
                    and monotonic() - poll_started_at
                    > self.config.order_status_terminal_timeout_seconds
                ):
                    max_polls_reached = True
                    break
                if poll_index > 0:
                    self._sleep_between_polls()
                poll_count += 1
                snapshot = self.status_service.poll_order_status(request.run_id, broker_order)
                status_snapshots.append(snapshot)
                final_status = snapshot.status
                self._notify(
                    notifications,
                    request.run_id,
                    request.order_id,
                    snapshot.status,
                    "Live order status polled.",
                    broker_order_id,
                )
                if snapshot.status == OrderStatus.HALTED:
                    result = self._result(
                        request=request,
                        final_status=OrderStatus.HALTED,
                        submitted_order=submitted_order,
                        broker_order_id=broker_order_id,
                        poll_count=poll_count,
                        status_snapshots=status_snapshots,
                        fill_reconciliations=fill_reconciliations,
                        broker_reconciliations=broker_reconciliations,
                        applied_fills=applied_fills,
                        notifications_sent=notifications,
                        halt_reason=snapshot.message or "Live order lifecycle halted.",
                    )
                    self._persist_summary(result)
                    return result

                fill_result = self.fill_reconciliation_service.reconcile_latest(request.run_id)
                fill_reconciliations.append(fill_result)
                applied_fills.extend(fill_result.applied_fills)
                if fill_result.applied_fills and self.broker_reconciliation_service is not None:
                    broker_result = self.broker_reconciliation_service.reconcile_latest()
                    broker_reconciliations.append(broker_result.model_dump(mode="json"))
                    if broker_result.passed is not True:
                        self._notify(
                            notifications,
                            request.run_id,
                            request.order_id,
                            OrderStatus.FAILED,
                            "Live order lifecycle failed.",
                            broker_order_id,
                        )
                        result = self._result(
                            request=request,
                            final_status=OrderStatus.FAILED,
                            submitted_order=submitted_order,
                            broker_order_id=broker_order_id,
                            poll_count=poll_count,
                            status_snapshots=status_snapshots,
                            fill_reconciliations=fill_reconciliations,
                            broker_reconciliations=broker_reconciliations,
                            applied_fills=applied_fills,
                            notifications_sent=notifications,
                            failed_reason="Broker reconciliation failed after fill update.",
                        )
                        self._persist_summary(result)
                        return result

                if snapshot.status in _TERMINAL_LIFECYCLE_STATUSES:
                    result = self._result(
                        request=request,
                        final_status=snapshot.status,
                        submitted_order=submitted_order,
                        broker_order_id=broker_order_id,
                        poll_count=poll_count,
                        status_snapshots=status_snapshots,
                        fill_reconciliations=fill_reconciliations,
                        broker_reconciliations=broker_reconciliations,
                        applied_fills=applied_fills,
                        notifications_sent=notifications,
                    )
                    self._persist_summary(result)
                    return result
            max_polls_reached = True
            result = self._result(
                request=request,
                final_status=final_status,
                submitted_order=submitted_order,
                broker_order_id=broker_order_id,
                poll_count=poll_count,
                status_snapshots=status_snapshots,
                fill_reconciliations=fill_reconciliations,
                broker_reconciliations=broker_reconciliations,
                applied_fills=applied_fills,
                notifications_sent=notifications,
                max_polls_reached=max_polls_reached,
            )
        except Exception as exc:
            self._notify(
                notifications,
                request.run_id,
                request.order_id,
                OrderStatus.FAILED,
                "Live order lifecycle failed.",
                broker_order_id,
            )
            if submitted_order is not None and submitted_order.broker_order is not None:
                self._persist_recovery_required_after_submit(request, submitted_order, exc)
            result = self._result(
                request=request,
                final_status=OrderStatus.FAILED,
                submitted_order=submitted_order,
                broker_order_id=broker_order_id,
                poll_count=len(status_snapshots),
                status_snapshots=status_snapshots,
                fill_reconciliations=fill_reconciliations,
                broker_reconciliations=broker_reconciliations,
                applied_fills=applied_fills,
                notifications_sent=notifications,
                failed_reason=str(exc),
            )
        self._persist_summary(result)
        return result

    def _notify(
        self,
        notifications: list[LiveOrderLifecycleNotification],
        run_id: str,
        order_id: str,
        status: OrderStatus,
        message: str,
        broker_order_id: str | None = None,
    ) -> None:
        event = LiveOrderLifecycleNotification(
            run_id=run_id,
            order_id=order_id,
            status=status,
            message=message,
            broker_order_id=broker_order_id,
        )
        notifications.append(event)
        if self.notification_client is not None:
            try:
                self.notification_client.notify(event)
            except Exception as exc:
                save_audited_system_event(
                    self.state_store,
                    self.audit_logger,
                    run_id,
                    "live_order_notification_failed",
                    {
                        "order_id": order_id,
                        "status": status.value,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    def _persist_recovery_required_after_submit(
        self,
        request: LiveOrderRequest,
        submitted_order: LiveOrderResult,
        exc: Exception,
    ) -> None:
        broker_order = submitted_order.broker_order
        if broker_order is None:
            return
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            request.run_id,
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
            {
                "reason": "lifecycle_exception_after_submit",
                "order_id": request.order_id,
                "request": {"order_id": request.order_id},
                "result": {
                    "broker_order": broker_order.model_dump(mode="json"),
                    "message": str(exc),
                },
            },
        )

    def _sleep_between_polls(self) -> None:
        if self.config.order_status_poll_interval_seconds <= 0:
            return
        if self.sleep_fn is not None:
            self.sleep_fn(self.config.order_status_poll_interval_seconds)

    def _result(
        self,
        *,
        request: LiveOrderRequest,
        final_status: OrderStatus,
        submitted_order: LiveOrderResult | None = None,
        broker_order_id: str | None = None,
        poll_count: int = 0,
        status_snapshots: list[LiveOrderStatusSnapshot] | None = None,
        fill_reconciliations: list[FillReconciliationResult] | None = None,
        broker_reconciliations: list[dict[str, Any]] | None = None,
        applied_fills: list[AppliedFill] | None = None,
        notifications_sent: list[LiveOrderLifecycleNotification] | None = None,
        max_polls_reached: bool = False,
        halt_reason: str | None = None,
        failed_reason: str | None = None,
    ) -> LiveOrderLifecycleResult:
        return LiveOrderLifecycleResult(
            run_id=request.run_id,
            order_id=request.order_id,
            final_status=final_status,
            broker_order_id=broker_order_id,
            signal_run_id=request.signal_run_id,
            submitted_order=submitted_order,
            poll_count=poll_count,
            status_snapshots=status_snapshots or [],
            applied_fills=applied_fills or [],
            fill_reconciliations=fill_reconciliations or [],
            broker_reconciliations=broker_reconciliations or [],
            notifications_sent=notifications_sent or [],
            max_polls_reached=max_polls_reached,
            halt_reason=halt_reason,
            failed_reason=failed_reason,
            checked_at=utc_now().isoformat(),
        )

    def _persist_summary(self, result: LiveOrderLifecycleResult) -> None:
        if self._lifecycle_summary_exists(result.run_id, result.order_id):
            return
        payload = result.model_dump(mode="json")
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            result.run_id,
            SystemEventType.LIVE_ORDER_LIFECYCLE,
            payload,
        )

    def _lifecycle_summary_exists(self, run_id: str, order_id: str) -> bool:
        return self.state_store.system_event_exists(
            str(SystemEventType.LIVE_ORDER_LIFECYCLE), order_id, run_id=run_id
        )


_TERMINAL_LIFECYCLE_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELED,
    OrderStatus.HALTED,
    OrderStatus.FAILED,
}


__all__ = ["LiveOrderLifecycleService"]
