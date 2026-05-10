from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderLifecycleNotification,
    LiveOrderLifecycleService,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderSafetyService,
    LiveOrderStatusClient,
    LiveOrderStatusService,
    LiveOrderStatusSnapshot,
    PartialFillReconciliationService,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_lifecycle_open_open_filled(tmp_path):
    lifecycle, store, status_client, notifier, request, approval, _ = _context(
        tmp_path,
        statuses=[
            _poll(OrderStatus.OPEN),
            _poll(OrderStatus.OPEN),
            _poll(OrderStatus.FILLED, filled=2.0, remaining=0.0, average_fill_price=70_000.0),
        ],
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.FILLED
    assert result.poll_count == 3
    assert status_client.call_count == 3
    assert result.applied_fills[0].quantity == 2.0
    assert store.load_latest_portfolio_state().positions == {"005930": 2.0}
    assert [event.status for event in notifier.events] == [
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED_BY_BROKER,
        OrderStatus.OPEN,
        OrderStatus.OPEN,
        OrderStatus.FILLED,
    ]


def test_lifecycle_open_partial_filled(tmp_path):
    lifecycle, store, _, _, request, approval, _ = _context(
        tmp_path,
        statuses=[
            _poll(OrderStatus.OPEN),
            _poll(
                OrderStatus.PARTIALLY_FILLED,
                filled=1.0,
                remaining=1.0,
                average_fill_price=70_000.0,
            ),
            _poll(OrderStatus.FILLED, filled=2.0, remaining=0.0, average_fill_price=75_000.0),
        ],
    )

    result = lifecycle.run(request, approval)

    state = store.load_latest_portfolio_state()
    assert result.final_status == OrderStatus.FILLED
    assert [fill.quantity for fill in result.applied_fills] == [1.0, 1.0]
    assert result.applied_fills[-1].notional == 80_000.0
    assert state.cash == 850_000.0
    assert state.positions == {"005930": 2.0}


def test_lifecycle_rejected_terminal(tmp_path):
    lifecycle, store, status_client, notifier, request, approval, _ = _context(
        tmp_path,
        statuses=[_poll(OrderStatus.REJECTED)],
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.REJECTED
    assert result.poll_count == 1
    assert status_client.call_count == 1
    assert store.load_latest_portfolio_state().positions == {}
    assert notifier.events[-1].status == OrderStatus.REJECTED


def test_lifecycle_canceled_terminal(tmp_path):
    lifecycle, store, status_client, _, request, approval, _ = _context(
        tmp_path,
        statuses=[_poll(OrderStatus.CANCELED)],
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.CANCELED
    assert result.poll_count == 1
    assert status_client.call_count == 1
    assert store.load_latest_portfolio_state().positions == {}


def test_lifecycle_unknown_status_halts(tmp_path):
    lifecycle, store, _, notifier, request, approval, _ = _context(
        tmp_path,
        statuses=[_poll(OrderStatus.UNKNOWN)],
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.HALTED
    assert "unknown order state" in (result.halt_reason or "")
    assert store.list_system_events_by_type("fill_reconciliation") == []
    assert notifier.events[-1].status == OrderStatus.HALTED


def test_lifecycle_submit_exception_records_recovery_required_and_halts(tmp_path):
    lifecycle, store, status_client, notifier, request, approval, _ = _context(
        tmp_path,
        statuses=[_poll(OrderStatus.FILLED)],
        submit_client=FailingSubmitClient(),
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.HALTED
    assert "ambiguous" in (result.halt_reason or "")
    assert status_client.call_count == 0
    recovery = store.list_system_events_by_type("live_order_recovery_required")[0]["payload"]
    assert recovery["request"]["order_id"] == request.order_id
    assert recovery["result"]["raw"]["exception_type"] == "TimeoutError"
    assert notifier.events[-1].status == OrderStatus.HALTED


def test_lifecycle_summary_persistence_is_idempotent_for_same_order(tmp_path):
    lifecycle, store, _, _, request, approval, _ = _context(
        tmp_path,
        statuses=[
            _poll(OrderStatus.FILLED, filled=2.0, remaining=0.0, average_fill_price=70_000.0)
        ],
    )

    first = lifecycle.run(request, approval)
    second = lifecycle.run(request, approval)

    assert first.final_status == OrderStatus.FILLED
    assert second.final_status == OrderStatus.FAILED
    assert len(store.list_system_events_by_type("live_order_lifecycle", limit=100)) == 1


def test_lifecycle_max_polls_reached_while_open(tmp_path):
    sleeps: list[float] = []
    lifecycle, _, status_client, _, request, approval, _ = _context(
        tmp_path,
        statuses=[_poll(OrderStatus.OPEN), _poll(OrderStatus.OPEN), _poll(OrderStatus.OPEN)],
        max_polls=2,
        poll_interval=0.0,
        sleep_fn=sleeps.append,
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.OPEN
    assert result.max_polls_reached is True
    assert result.poll_count == 2
    assert status_client.call_count == 2
    assert sleeps == []


def test_lifecycle_fill_reconciliation_called_after_each_fill_bearing_status(tmp_path):
    lifecycle, _, _, _, request, approval, broker_reconciliation = _context(
        tmp_path,
        statuses=[
            _poll(
                OrderStatus.PARTIALLY_FILLED,
                filled=1.0,
                remaining=1.0,
                average_fill_price=70_000.0,
            ),
            _poll(OrderStatus.FILLED, filled=2.0, remaining=0.0, average_fill_price=75_000.0),
        ],
    )

    result = lifecycle.run(request, approval)

    assert result.final_status == OrderStatus.FILLED
    assert len(result.fill_reconciliations) == 2
    assert [len(item.applied_fills) for item in result.fill_reconciliations] == [1, 1]
    assert broker_reconciliation.call_count == 2


def test_lifecycle_notifications_emitted_for_key_transitions(tmp_path):
    lifecycle, store, _, notifier, request, approval, _ = _context(
        tmp_path,
        statuses=[
            _poll(
                OrderStatus.PARTIALLY_FILLED,
                filled=1.0,
                remaining=1.0,
                average_fill_price=70_000.0,
            ),
            _poll(OrderStatus.FILLED, filled=2.0, remaining=0.0, average_fill_price=75_000.0),
        ],
    )

    result = lifecycle.run(request, approval)

    assert [event.status for event in notifier.events] == [
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED_BY_BROKER,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    ]
    event = store.list_system_events_by_type("live_order_lifecycle")[0]
    assert event["payload"]["final_status"] == "filled"
    assert len(event["payload"]["notifications_sent"]) == 4
    assert result.notifications_sent == notifier.events


def _context(
    tmp_path,
    *,
    statuses: list[LiveOrderStatusSnapshot],
    max_polls: int = 10,
    poll_interval: float = 0.0,
    sleep_fn=None,
    submit_client: LiveOrderClient | None = None,
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
    status_client = FakePollingStatusClient(statuses)
    notifier = FakeNotificationClient()
    broker_reconciliation = FakeBrokerReconciliation()
    lifecycle = LiveOrderLifecycleService(
        ExecutionConfig(
            live_order_enabled=True,
            max_live_order_notional=200_000.0,
            max_daily_live_notional=300_000.0,
            order_status_max_polls=max_polls,
            order_status_poll_interval_seconds=poll_interval,
        ),
        store,
        audit,
        LiveOrderSafetyService(
            ExecutionConfig(
                live_order_enabled=True,
                max_live_order_notional=200_000.0,
                max_daily_live_notional=300_000.0,
            ),
            store,
            audit,
            submit_client or FakeSubmitClient(),
        ),
        LiveOrderStatusService(store, audit, status_client),
        PartialFillReconciliationService(store, audit),
        broker_reconciliation,
        notifier,
        sleep_fn=sleep_fn,
    )
    return (
        lifecycle,
        store,
        status_client,
        notifier,
        request,
        _approval(request),
        broker_reconciliation,
    )


def _approval(request: LiveOrderRequest) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _poll(
    status: OrderStatus,
    *,
    filled: float = 0.0,
    remaining: float = 2.0,
    average_fill_price: float | None = None,
) -> LiveOrderStatusSnapshot:
    return LiveOrderStatusSnapshot(
        broker_order=_broker_order("ord_live_1"),
        status=status,
        checked_at=utc_now().isoformat(),
        symbol="005930",
        side=OrderSide.BUY,
        partial_fill=PartialFillSummary(
            ordered_quantity=filled + remaining,
            filled_quantity=filled,
            remaining_quantity=remaining,
            average_fill_price=average_fill_price,
            fill_count=1 if filled else 0,
        ),
        raw_status=status.value,
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
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=_broker_order(request.order_id),
        )


class FailingSubmitClient(LiveOrderClient):
    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        del request
        raise TimeoutError("submit timed out")


class FakePollingStatusClient(LiveOrderStatusClient):
    def __init__(self, statuses: list[LiveOrderStatusSnapshot]) -> None:
        self.statuses = statuses
        self.call_count = 0

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        index = min(self.call_count, len(self.statuses) - 1)
        self.call_count += 1
        return self.statuses[index].model_copy(update={"broker_order": broker_order_id})


class FakeNotificationClient(LiveOrderNotificationClient):
    def __init__(self) -> None:
        self.events: list[LiveOrderLifecycleNotification] = []

    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        self.events.append(event)


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def __init__(self) -> None:
        self.call_count = 0

    def reconcile_latest(self) -> ReconciliationResult:
        self.call_count += 1
        return ReconciliationResult(
            run_id=f"run_broker_reconcile_{self.call_count}",
            passed=True,
            checked_at=utc_now().isoformat(),
            issues=[],
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )
