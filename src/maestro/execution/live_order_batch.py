from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_order_models import (
    AppliedFill,
    FillReconciliationResult,
    LiveOrderBatchItemResult,
    LiveOrderBatchLifecycleResult,
    LiveOrderLifecycleNotification,
    LiveOrderLifecycleResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import LiveOrderNotificationClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


@dataclass
class BatchOrderDependencies:
    safety_service: Any
    status_service: Any
    fill_reconciliation_service: Any
    broker_reconciliation_service: Any | None = None


def _sells_first(
    items: list[tuple[LiveOrderRequest, BatchOrderDependencies]],
) -> list[tuple[LiveOrderRequest, BatchOrderDependencies]]:
    """Order a batch so every sell is submitted before any buy.

    A rebalance sizes its buys against the proceeds of the sells filed in the
    same batch, so submitting buy-first asks the broker to spend cash the account
    has not raised yet. Sorting is stable, so the builder's ordering survives
    within each side.
    """
    return sorted(items, key=lambda item: item[0].side != OrderSide.SELL)


@dataclass
class _OrderState:
    request: LiveOrderRequest
    dependencies: BatchOrderDependencies
    submitted: LiveOrderResult | None = None
    broker_order_id: str | None = None
    final_status: OrderStatus = OrderStatus.SUBMITTED
    snapshots: list[LiveOrderStatusSnapshot] = field(default_factory=list)
    fill_results: list[FillReconciliationResult] = field(default_factory=list)
    broker_results: list[dict[str, Any]] = field(default_factory=list)
    applied_fills: list[AppliedFill] = field(default_factory=list)
    notifications: list[LiveOrderLifecycleNotification] = field(default_factory=list)
    failed_reason: str | None = None
    halt_reason: str | None = None
    terminal: bool = False
    last_notified_status: OrderStatus | None = None


class LiveOrderBatchLifecycleService:
    """Submit an approved group first, then poll all accepted orders by rounds."""

    def __init__(
        self,
        config: ExecutionConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        notification_client: LiveOrderNotificationClient | None = None,
        *,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.notification_client = notification_client
        self.sleep_fn = sleep_fn or sleep

    def run(
        self,
        items: list[tuple[LiveOrderRequest, BatchOrderDependencies]],
        approval_decision: ApprovalDecision,
    ) -> LiveOrderBatchLifecycleResult:
        run_started = monotonic()
        states = [
            _OrderState(request=request, dependencies=deps)
            for request, deps in _sells_first(items)
        ]
        submission_started = monotonic()
        stop_submissions = False
        for state in states:
            if stop_submissions:
                state.final_status = OrderStatus.HALTED
                state.halt_reason = "Batch submission stopped after a recovery-required result."
                state.terminal = True
                continue
            self._notify(state, OrderStatus.SUBMITTED, "Live order submitted to safety service.")
            try:
                state.submitted = state.dependencies.safety_service.submit_approved_order(
                    state.request,
                    approval_decision,
                )
            except Exception as exc:
                state.final_status = OrderStatus.FAILED
                state.failed_reason = str(exc)
                state.terminal = True
                self._notify(state, OrderStatus.FAILED, "Live order pre-submit validation failed.")
                continue
            broker_order = state.submitted.broker_order
            state.broker_order_id = broker_order.broker_order_id if broker_order else None
            state.final_status = state.submitted.status
            self._notify(
                state,
                state.submitted.status,
                "Live order submission result received.",
            )
            if state.submitted.status == OrderStatus.REJECTED:
                state.failed_reason = state.submitted.message or "Broker rejected live order."
                state.terminal = True
                continue
            if state.submitted.status == OrderStatus.HALTED or broker_order is None:
                state.halt_reason = state.submitted.message or "Live order submission halted."
                state.terminal = True
                stop_submissions = True

        submission_duration = monotonic() - submission_started
        started_at = monotonic()
        reconciliation_duration = 0.0
        poll_rounds = 0
        max_polls_reached = False
        active = [state for state in states if not state.terminal]
        for round_index in range(self.config.order_status_max_polls):
            if not active:
                break
            if (
                self.config.order_status_terminal_timeout_seconds > 0
                and monotonic() - started_at
                > self.config.order_status_terminal_timeout_seconds
            ):
                max_polls_reached = True
                break
            if round_index > 0:
                self._sleep_between_rounds()
            poll_rounds += 1
            for state in list(active):
                broker_order = state.submitted.broker_order if state.submitted else None
                if broker_order is None:
                    active.remove(state)
                    continue
                try:
                    snapshot = state.dependencies.status_service.poll_order_status(
                        state.request.run_id,
                        broker_order,
                    )
                except Exception as exc:
                    state.final_status = OrderStatus.FAILED
                    state.failed_reason = str(exc)
                    state.terminal = True
                    self._persist_recovery_required(state, exc)
                    self._notify(state, OrderStatus.FAILED, "Live order status polling failed.")
                    active.remove(state)
                    continue
                state.snapshots.append(snapshot)
                state.final_status = snapshot.status
                self._notify(state, snapshot.status, "Live order status changed.")
                if snapshot.status in _TERMINAL_STATUSES:
                    state.terminal = True
                    active.remove(state)

            fill_service = active[0].dependencies.fill_reconciliation_service if active else None
            if fill_service is None:
                fill_service = next(
                    (
                        state.dependencies.fill_reconciliation_service
                        for state in states
                        if state.snapshots
                    ),
                    None,
                )
            if fill_service is not None:
                reconciliation_started = monotonic()
                fill_result = fill_service.reconcile_latest(states[0].request.run_id)
                reconciliation_duration += monotonic() - reconciliation_started
                for state in states:
                    if state.snapshots:
                        state.fill_results.append(fill_result)
                        state.applied_fills.extend(
                            fill
                            for fill in fill_result.applied_fills
                            if fill.broker_order_id == state.broker_order_id
                        )
                if fill_result.applied_fills:
                    reconciliations: dict[int, Any] = {}
                    for state in states:
                        reconciliation = state.dependencies.broker_reconciliation_service
                        if reconciliation is not None:
                            reconciliations.setdefault(id(reconciliation), reconciliation)
                    for reconciliation in reconciliations.values():
                        related = [
                            state
                            for state in states
                            if state.snapshots
                            and state.dependencies.broker_reconciliation_service
                            is reconciliation
                        ]
                        try:
                            reconciliation_started = monotonic()
                            broker_result = reconciliation.reconcile_latest().model_dump(
                                mode="json"
                            )
                            reconciliation_duration += monotonic() - reconciliation_started
                        except Exception as exc:
                            for state in related:
                                state.final_status = OrderStatus.FAILED
                                state.failed_reason = str(exc)
                                state.terminal = True
                                if state in active:
                                    active.remove(state)
                                self._persist_recovery_required(
                                    state,
                                    exc,
                                    reason="batch_reconciliation_exception_after_fill",
                                )
                                self._notify(
                                    state,
                                    OrderStatus.FAILED,
                                    "Broker reconciliation failed after fill update.",
                                )
                            continue
                        for state in related:
                            state.broker_results.append(broker_result)
                            if broker_result.get("passed") is not True:
                                state.final_status = OrderStatus.FAILED
                                state.failed_reason = (
                                    "Broker reconciliation failed after fill update."
                                )
                                state.terminal = True
                                if state in active:
                                    active.remove(state)
                                self._notify(
                                    state,
                                    OrderStatus.FAILED,
                                    "Broker reconciliation failed after fill update.",
                                )

        if active:
            max_polls_reached = True
        polling_duration = monotonic() - started_at
        lifecycle_results = [
            self._lifecycle_result(state, max_polls_reached=state in active)
            for state in states
        ]
        for result in lifecycle_results:
            if not self.state_store.system_event_exists(
                str(SystemEventType.LIVE_ORDER_LIFECYCLE),
                result.order_id,
                run_id=result.run_id,
            ):
                save_audited_system_event(
                    self.state_store,
                    self.audit_logger,
                    result.run_id,
                    SystemEventType.LIVE_ORDER_LIFECYCLE,
                    result.model_dump(mode="json"),
                )
        batch = LiveOrderBatchLifecycleResult(
            run_id=states[0].request.run_id if states else "",
            items=[
                LiveOrderBatchItemResult(request=state.request, lifecycle=result)
                for state, result in zip(states, lifecycle_results, strict=True)
            ],
            poll_rounds=poll_rounds,
            max_polls_reached=max_polls_reached,
            orders_planned=len(states),
            orders_submitted=sum(state.submitted is not None for state in states),
            orders_accepted=sum(state.broker_order_id is not None for state in states),
            orders_filled=sum(state.final_status == OrderStatus.FILLED for state in states),
            orders_failed=sum(
                state.final_status
                in {OrderStatus.FAILED, OrderStatus.REJECTED, OrderStatus.HALTED}
                for state in states
            ),
            submission_duration_seconds=submission_duration,
            polling_duration_seconds=polling_duration,
            reconciliation_duration_seconds=reconciliation_duration,
            total_duration_seconds=monotonic() - run_started,
            checked_at=utc_now().isoformat(),
        )
        if states:
            save_audited_system_event(
                self.state_store,
                self.audit_logger,
                batch.run_id,
                "live_order_batch_lifecycle",
                batch.model_dump(mode="json"),
            )
        if self.notification_client is not None and hasattr(
            self.notification_client, "notify_batch"
        ):
            try:
                self.notification_client.notify_batch(batch)
            except Exception as exc:
                save_audited_system_event(
                    self.state_store,
                    self.audit_logger,
                    batch.run_id,
                    "live_order_notification_failed",
                    {
                        "order_id": None,
                        "status": "batch_summary",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
        return batch

    def _notify(
        self,
        state: _OrderState,
        status: OrderStatus,
        message: str,
    ) -> None:
        if state.last_notified_status == status:
            return
        event = LiveOrderLifecycleNotification(
            run_id=state.request.run_id,
            order_id=state.request.order_id,
            status=status,
            message=message,
            broker_order_id=state.broker_order_id,
        )
        state.notifications.append(event)
        state.last_notified_status = status
        if self.notification_client is not None:
            try:
                self.notification_client.notify(event)
            except Exception as exc:
                save_audited_system_event(
                    self.state_store,
                    self.audit_logger,
                    state.request.run_id,
                    "live_order_notification_failed",
                    {
                        "order_id": state.request.order_id,
                        "status": status.value,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    def _lifecycle_result(
        self,
        state: _OrderState,
        *,
        max_polls_reached: bool,
    ) -> LiveOrderLifecycleResult:
        return LiveOrderLifecycleResult(
            run_id=state.request.run_id,
            order_id=state.request.order_id,
            final_status=state.final_status,
            broker_order_id=state.broker_order_id,
            signal_run_id=state.request.signal_run_id,
            submitted_order=state.submitted,
            poll_count=len(state.snapshots),
            status_snapshots=state.snapshots,
            applied_fills=state.applied_fills,
            fill_reconciliations=state.fill_results,
            broker_reconciliations=state.broker_results,
            notifications_sent=state.notifications,
            max_polls_reached=max_polls_reached,
            halt_reason=state.halt_reason,
            failed_reason=state.failed_reason,
            checked_at=utc_now().isoformat(),
        )

    def _persist_recovery_required(
        self,
        state: _OrderState,
        exc: Exception,
        *,
        reason: str = "batch_status_exception_after_submit",
    ) -> None:
        broker_order = state.submitted.broker_order if state.submitted else None
        if broker_order is None:
            return
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            state.request.run_id,
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
            {
                "reason": reason,
                "order_id": state.request.order_id,
                "request": {"order_id": state.request.order_id},
                "result": {
                    "broker_order": broker_order.model_dump(mode="json"),
                    "message": str(exc),
                },
            },
        )

    def _sleep_between_rounds(self) -> None:
        if self.config.order_status_poll_interval_seconds > 0:
            self.sleep_fn(self.config.order_status_poll_interval_seconds)


_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELED,
    OrderStatus.HALTED,
    OrderStatus.FAILED,
}


__all__ = ["BatchOrderDependencies", "LiveOrderBatchLifecycleService"]
