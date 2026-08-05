from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_order_batch import (
    BatchOrderDependencies,
    LiveOrderBatchLifecycleService,
)
from maestro.execution.live_orders import (
    BrokerOrderId,
    FillReconciliationResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_batch_submits_all_orders_before_first_poll_and_suppresses_repeated_open(tmp_path):
    calls: list[str] = []
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    fill_service = _FillService()
    notifier = _Notifier()
    items = []
    for order_id in ("ord_1", "ord_2"):
        request = _request(order_id)
        items.append(
            (
                request,
                BatchOrderDependencies(
                    safety_service=_SafetyService(calls),
                    status_service=_StatusService(calls),
                    fill_reconciliation_service=fill_service,
                ),
            )
        )
    service = LiveOrderBatchLifecycleService(
        ExecutionConfig(
            live_order_enabled=True,
            order_status_max_polls=2,
            order_status_poll_interval_seconds=0,
        ),
        store,
        audit,
        notifier,
    )

    result = service.run(items, _approval())

    assert calls[:2] == ["submit:ord_1", "submit:ord_2"]
    assert calls[2:4] == ["poll:ord_1", "poll:ord_2"]
    assert result.poll_rounds == 2
    assert result.max_polls_reached is True
    assert fill_service.call_count == 2
    assert [event.status for event in notifier.events].count(OrderStatus.OPEN) == 2
    assert len(store.list_system_events_by_type("live_order_batch_lifecycle")) == 1


def test_batch_continues_after_definite_pre_submit_failure(tmp_path):
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (_request("ord_1"), _dependencies(_FailingSafetyService(calls), calls, fill_service)),
        (_request("ord_2"), _dependencies(_SafetyService(calls), calls, fill_service)),
    ]

    result = service.run(items, _approval())

    assert calls[:2] == ["submit:ord_1", "submit:ord_2"]
    assert [item.lifecycle.final_status for item in result.items] == [
        OrderStatus.FAILED,
        OrderStatus.OPEN,
    ]


def test_batch_continues_after_definitive_broker_rejection(tmp_path):
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (_request("ord_1"), _dependencies(_RejectedSafetyService(calls), calls, fill_service)),
        (_request("ord_2"), _dependencies(_SafetyService(calls), calls, fill_service)),
    ]

    result = service.run(items, _approval())

    assert calls[:2] == ["submit:ord_1", "submit:ord_2"]
    assert [item.lifecycle.final_status for item in result.items] == [
        OrderStatus.REJECTED,
        OrderStatus.OPEN,
    ]


def test_batch_isolates_pdbc_rejection_and_submits_remaining_six_orders(tmp_path):
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (
            _request("ord_pdbc", symbol="PDBC"),
            _dependencies(_RejectedSafetyService(calls), calls, fill_service),
        ),
        *[
            (
                _request(f"ord_{index}", symbol=f"ETF_{index}"),
                _dependencies(_SafetyService(calls), calls, fill_service),
            )
            for index in range(1, 7)
        ],
    ]

    result = service.run(items, _approval())

    assert calls[:7] == [
        "submit:ord_pdbc",
        "submit:ord_1",
        "submit:ord_2",
        "submit:ord_3",
        "submit:ord_4",
        "submit:ord_5",
        "submit:ord_6",
    ]
    assert result.items[0].lifecycle.final_status == OrderStatus.REJECTED
    assert all(
        item.lifecycle.final_status == OrderStatus.OPEN for item in result.items[1:]
    )
    assert all(
        item.lifecycle.final_status != OrderStatus.HALTED for item in result.items
    )


def test_batch_stops_later_submissions_after_ambiguous_halt(tmp_path):
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (_request("ord_1"), _dependencies(_HaltedSafetyService(calls), calls, fill_service)),
        (_request("ord_2"), _dependencies(_SafetyService(calls), calls, fill_service)),
    ]

    result = service.run(items, _approval())

    assert calls == ["submit:ord_1"]
    assert [item.lifecycle.final_status for item in result.items] == [
        OrderStatus.HALTED,
        OrderStatus.HALTED,
    ]


def test_batch_submits_sells_before_buys(tmp_path):
    """A rotation's buy is funded by the sell filed alongside it.

    The order builder sizes the buy against the proceeds of the same batch, so
    reaching the broker buy-first asks it to spend cash the account has not
    raised yet and earns an insufficient-funds rejection.
    """
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (
            _request("ord_buy", symbol="TLT", side=OrderSide.BUY),
            _dependencies(_SafetyService(calls), calls, fill_service),
        ),
        (
            _request("ord_sell", symbol="QQQ", side=OrderSide.SELL),
            _dependencies(_SafetyService(calls), calls, fill_service),
        ),
    ]

    service.run(items, _approval())

    assert calls[:2] == ["submit:ord_sell", "submit:ord_buy"]


def test_batch_keeps_declared_order_within_a_side(tmp_path):
    calls: list[str] = []
    service, fill_service = _batch_service(tmp_path)
    items = [
        (
            _request(f"ord_{index}", symbol=f"ETF_{index}", side=side),
            _dependencies(_SafetyService(calls), calls, fill_service),
        )
        for index, side in enumerate([OrderSide.BUY, OrderSide.SELL, OrderSide.BUY, OrderSide.SELL])
    ]

    service.run(items, _approval())

    assert calls[:4] == ["submit:ord_1", "submit:ord_3", "submit:ord_0", "submit:ord_2"]


def _batch_service(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    fill_service = _FillService()
    return (
        LiveOrderBatchLifecycleService(
            ExecutionConfig(
                live_order_enabled=True,
                order_status_max_polls=1,
                order_status_poll_interval_seconds=0,
            ),
            store,
            AuditLogger(str(tmp_path / "audit.jsonl")),
        ),
        fill_service,
    )


def _dependencies(safety_service, calls, fill_service):
    return BatchOrderDependencies(
        safety_service=safety_service,
        status_service=_StatusService(calls),
        fill_reconciliation_service=fill_service,
    )


def _request(
    order_id: str,
    *,
    symbol: str = "005930",
    side: OrderSide = OrderSide.BUY,
) -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=2,
        limit_price=70_000,
        approval_id="appr_1",
        run_id="run_1",
        account_id="kis_ps",
    )


def _approval() -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="appr_1",
        run_id="run_1",
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:test",
    )


class _SafetyService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=BrokerOrderId(
                broker="kis",
                broker_order_id=request.order_id,
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
                account_id=request.account_id,
            ),
        )


class _FailingSafetyService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        raise ValueError("definite pre-submit rejection")


class _HaltedSafetyService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.HALTED,
            message="ambiguous broker submission",
        )


class _RejectedSafetyService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.REJECTED,
            message="kis order rejected: APBK1497",
        )


class _StatusService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def poll_order_status(self, run_id, broker_order):
        del run_id
        self.calls.append(f"poll:{broker_order.broker_order_id}")
        return LiveOrderStatusSnapshot(
            broker_order=broker_order,
            status=OrderStatus.OPEN,
            checked_at=utc_now().isoformat(),
            symbol="005930",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=2,
                filled_quantity=0,
                remaining_quantity=2,
            ),
        )


class _FillService:
    def __init__(self) -> None:
        self.call_count = 0

    def reconcile_latest(self, run_id):
        self.call_count += 1
        return FillReconciliationResult(
            run_id=run_id,
            checked_at=utc_now().isoformat(),
            cash=1_000_000,
            positions={},
        )


class _Notifier:
    def __init__(self) -> None:
        self.events = []
        self.batches = []

    def notify(self, event):
        self.events.append(event)

    def notify_batch(self, batch):
        self.batches.append(batch)
