from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderSafetyService,
    LiveOrderStatusClient,
    LiveOrderStatusService,
    LiveOrderStatusSnapshot,
    LiveOrderWorkflowService,
    PartialFillReconciliationService,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_workflow_accepted_order_open_status_no_fills(tmp_path):
    workflow, store, submit_client, status_client, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.OPEN,
    )

    result = workflow.run(request, approval)

    assert result.workflow_status == OrderStatus.OPEN
    assert result.applied_fills == []
    assert submit_client.requests == [request]
    assert status_client.requests == [request.order_id]
    assert store.load_latest_portfolio_state().cash == 1_000_000.0
    assert (
        store.list_system_events_by_type("live_order_workflow")[0]["payload"]["workflow_status"]
        == "open"
    )


def test_workflow_accepted_order_partial_fill_reconciles_fill(tmp_path):
    workflow, store, _, _, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.PARTIALLY_FILLED,
        filled=1.0,
        remaining=1.0,
        average_fill_price=70_000.0,
    )

    result = workflow.run(request, approval)

    state = store.load_latest_portfolio_state()
    assert result.workflow_status == OrderStatus.PARTIALLY_FILLED
    assert result.applied_fills[0].quantity == 1.0
    assert state.cash == 930_000.0
    assert state.positions == {"005930": 1.0}


def test_workflow_accepted_order_filled_reconciles_fill(tmp_path):
    workflow, store, _, _, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.FILLED,
        filled=2.0,
        remaining=0.0,
        average_fill_price=70_000.0,
    )

    result = workflow.run(request, approval)

    state = store.load_latest_portfolio_state()
    assert result.workflow_status == OrderStatus.FILLED
    assert result.applied_fills[0].quantity == 2.0
    assert state.cash == 860_000.0
    assert state.positions == {"005930": 2.0}


def test_workflow_unknown_submit_result_halts_without_polling(tmp_path):
    workflow, _, _, status_client, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.UNKNOWN,
        poll_status=OrderStatus.OPEN,
    )

    result = workflow.run(request, approval)

    assert result.workflow_status == OrderStatus.HALTED
    assert "unknown order state" in (result.halt_reason or "")
    assert status_client.requests == []


def test_workflow_definitive_submit_rejection_does_not_poll(tmp_path):
    workflow, _, _, status_client, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.REJECTED,
        poll_status=OrderStatus.OPEN,
    )

    result = workflow.run(request, approval)

    assert result.workflow_status == OrderStatus.REJECTED
    assert result.broker_order_id is None
    assert status_client.requests == []


def test_workflow_unknown_status_polling_halts(tmp_path):
    workflow, store, _, status_client, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.UNKNOWN,
    )

    result = workflow.run(request, approval)

    assert result.workflow_status == OrderStatus.HALTED
    assert status_client.requests == [request.order_id]
    assert store.list_system_events_by_type("fill_reconciliation") == []


def test_workflow_rejected_and_canceled_status_do_not_update_portfolio(tmp_path):
    for status in [OrderStatus.REJECTED, OrderStatus.CANCELED]:
        workflow, store, _, _, request, approval = _context(
            tmp_path / status.value,
            submit_status=OrderStatus.ACCEPTED_BY_BROKER,
            poll_status=status,
        )

        result = workflow.run(request, approval)

        assert result.workflow_status == status
        assert result.applied_fills == []
        assert store.load_latest_portfolio_state().positions == {}


def test_workflow_failed_broker_reconciliation_after_fill_returns_failed(tmp_path):
    workflow, _, _, _, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.FILLED,
        filled=2.0,
        remaining=0.0,
        average_fill_price=70_000.0,
        broker_reconciliation_passed=False,
    )

    result = workflow.run(request, approval)

    assert result.workflow_status == OrderStatus.FAILED
    assert result.broker_reconciliation is not None
    assert result.broker_reconciliation["passed"] is False
    assert "Broker reconciliation failed" in (result.halt_reason or "")


def test_workflow_duplicate_order_blocked_by_safety_service(tmp_path):
    workflow, _, _, status_client, request, approval = _context(
        tmp_path,
        submit_status=OrderStatus.ACCEPTED_BY_BROKER,
        poll_status=OrderStatus.OPEN,
    )
    first = workflow.run(request, approval)

    second = workflow.run(request, approval)

    assert first.workflow_status == OrderStatus.OPEN
    assert second.workflow_status == OrderStatus.FAILED
    assert "Duplicate live order request rejected" in (second.failed_reason or "")
    assert status_client.requests == [request.order_id]


def _context(
    tmp_path,
    *,
    submit_status: OrderStatus,
    poll_status: OrderStatus,
    filled: float = 0.0,
    remaining: float = 2.0,
    average_fill_price: float | None = None,
    broker_reconciliation_passed: bool = True,
):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    request = LiveOrderRequest(
        order_id="ord_live_1",
        symbol="005930",
        side=OrderSide.BUY,
        quantity=2.0,
        limit_price=70_000.0,
        approval_id="appr_live_1",
        run_id="run_live_1",
        duplicate_key="signal-1",
    )
    store.save_system_event("run_reconcile_initial", "broker_reconciliation", {"passed": True})
    submit_client = FakeSubmitClient(submit_status)
    status_client = FakeStatusClient(
        poll_status,
        filled=filled,
        remaining=remaining,
        average_fill_price=average_fill_price,
    )
    safety_service = LiveOrderSafetyService(
        ExecutionConfig(
            live_order_enabled=True,
            max_live_order_notional=200_000.0,
            max_daily_live_notional=300_000.0,
        ),
        store,
        audit,
        submit_client,
    )
    workflow = LiveOrderWorkflowService(
        store,
        audit,
        safety_service,
        LiveOrderStatusService(store, audit, status_client),
        PartialFillReconciliationService(store, audit),
        FakeBrokerReconciliation(broker_reconciliation_passed),
    )
    return workflow, store, submit_client, status_client, request, _approval(request)


def _approval(request: LiveOrderRequest) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _broker_order(order_id: str) -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id=order_id,
        broker_order_org_no="KRX",
        order_id=order_id,
        submitted_at=utc_now().isoformat(),
    )


class FakeSubmitClient(LiveOrderClient):
    def __init__(self, status: OrderStatus) -> None:
        self.status = status
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        broker_order = None
        if self.status not in {OrderStatus.UNKNOWN, OrderStatus.REJECTED}:
            broker_order = _broker_order(request.order_id)
        return LiveOrderResult(
            order_id=request.order_id,
            status=self.status,
            broker_order=broker_order,
            message="fake submit result",
        )


class FakeStatusClient(LiveOrderStatusClient):
    def __init__(
        self,
        status: OrderStatus,
        *,
        filled: float,
        remaining: float,
        average_fill_price: float | None,
    ) -> None:
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.average_fill_price = average_fill_price
        self.requests: list[str] = []

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        self.requests.append(broker_order_id.broker_order_id)
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=self.status,
            checked_at=utc_now().isoformat(),
            symbol="005930",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=self.filled + self.remaining,
                filled_quantity=self.filled,
                remaining_quantity=self.remaining,
                average_fill_price=self.average_fill_price,
                fill_count=1 if self.filled else 0,
            ),
            raw_status=self.status.value,
        )


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def __init__(self, passed: bool) -> None:
        self.passed = passed

    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id="run_broker_reconcile",
            passed=self.passed,
            checked_at=utc_now().isoformat(),
            issues=[],
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )
