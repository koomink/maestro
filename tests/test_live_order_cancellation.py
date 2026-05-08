import pytest

from maestro.approval.models import ApprovalDecision
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_orders import (
    BrokerOrderId,
    LiveOrderCancelClient,
    LiveOrderCancellationService,
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_open_order_cancel_allowed(tmp_path):
    service, store, client, request, approval = _context(tmp_path, OrderStatus.OPEN)

    result = service.cancel_order(request, approval)

    events = store.list_system_events_by_type("live_order_cancel")
    assert result.status == OrderStatus.CANCELED
    assert result.canceled_quantity == 2.0
    assert client.requests == [request]
    assert events[0]["payload"]["request"]["broker_order"]["broker_order_id"] == "KIS-1"


def test_partial_fill_cancel_allowed_for_remaining_quantity(tmp_path):
    service, store, client, request, approval = _context(
        tmp_path,
        OrderStatus.PARTIALLY_FILLED,
        filled=1.0,
        remaining=2.0,
        with_fill_reconciliation=True,
    )

    result = service.cancel_order(request, approval)

    assert result.status == OrderStatus.CANCELED
    assert result.canceled_quantity == 2.0
    assert client.requests == [request]
    assert (
        store.list_system_events_by_type("live_order_cancel")[0]["payload"]["result"][
            "canceled_quantity"
        ]
        == 2.0
    )


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.HALTED,
        OrderStatus.UNKNOWN,
    ],
)
def test_terminal_and_unknown_status_cancel_rejected(tmp_path, status):
    service, _, client, request, approval = _context(tmp_path, status, filled=2.0, remaining=0.0)

    with pytest.raises(ValueError):
        service.cancel_order(request, approval)

    assert client.requests == []


def test_duplicate_cancel_rejected(tmp_path):
    service, _, client, request, approval = _context(tmp_path, OrderStatus.OPEN)
    service.cancel_order(request, approval)

    with pytest.raises(ValueError, match="Duplicate"):
        service.cancel_order(request, approval)

    assert client.requests == [request]


def test_cancel_requires_telegram_approval(tmp_path):
    service, _, client, request, _ = _context(tmp_path, OrderStatus.OPEN)
    approval = _approval(request, decided_by="console:default_decision")

    with pytest.raises(ValueError, match="Telegram approval"):
        service.cancel_order(request, approval)

    assert client.requests == []


def test_partial_fill_cancel_requires_fill_reconciliation(tmp_path):
    service, _, client, request, approval = _context(
        tmp_path,
        OrderStatus.PARTIALLY_FILLED,
        filled=1.0,
        remaining=1.0,
        with_fill_reconciliation=False,
    )

    with pytest.raises(ValueError, match="fill reconciliation"):
        service.cancel_order(request, approval)

    assert client.requests == []


def test_cancel_result_persisted_as_system_and_audit_event(tmp_path):
    service, store, _, request, approval = _context(tmp_path, OrderStatus.OPEN)

    service.cancel_order(request, approval)

    events = store.list_system_events_by_type("live_order_cancel")
    assert events[0]["payload"]["result"]["status"] == "canceled"
    assert "live_order_cancel" in service.audit_logger.path.read_text()


def _context(
    tmp_path,
    status: OrderStatus,
    *,
    filled: float = 0.0,
    remaining: float = 2.0,
    with_fill_reconciliation: bool = False,
):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    broker_order = _broker_order()
    request = LiveOrderCancelRequest(
        run_id="run_cancel_1",
        approval_id="appr_cancel_1",
        broker_order=broker_order,
        reason="test cancel",
    )
    store.save_system_event("run_reconcile", "broker_reconciliation", {"passed": True})
    store.save_system_event(
        "run_status",
        "live_order_status",
        _status(broker_order, status, filled=filled, remaining=remaining).model_dump(mode="json"),
    )
    if with_fill_reconciliation:
        store.save_system_event(
            "run_fill",
            "fill_reconciliation",
            {"applied_fills": [{"broker_order_id": broker_order.broker_order_id}]},
        )
    client = FakeCancelClient(canceled_quantity=remaining)
    service = LiveOrderCancellationService(store, audit, client)
    return service, store, client, request, _approval(request)


def _broker_order() -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id="KIS-1",
        broker_order_org_no="KRX",
        order_id="ord_live_1",
        submitted_at=utc_now().isoformat(),
    )


def _status(
    broker_order: BrokerOrderId,
    status: OrderStatus,
    *,
    filled: float,
    remaining: float,
) -> LiveOrderStatusSnapshot:
    return LiveOrderStatusSnapshot(
        broker_order=broker_order,
        status=status,
        checked_at=utc_now().isoformat(),
        symbol="005930",
        side=OrderSide.BUY,
        partial_fill=PartialFillSummary(
            ordered_quantity=filled + remaining,
            filled_quantity=filled,
            remaining_quantity=remaining,
            average_fill_price=70_000.0 if filled else None,
            fill_count=1 if filled else 0,
        ),
        raw_status=status.value,
    )


def _approval(
    request: LiveOrderCancelRequest,
    *,
    status: str = "approved",
    decided_by: str = "telegram:fake",
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status=status,
        decided_at=utc_now(),
        decided_by=decided_by,
    )


class FakeCancelClient(LiveOrderCancelClient):
    def __init__(self, canceled_quantity: float) -> None:
        self.canceled_quantity = canceled_quantity
        self.requests: list[LiveOrderCancelRequest] = []

    def cancel_order(self, request: LiveOrderCancelRequest) -> LiveOrderCancelResult:
        self.requests.append(request)
        return LiveOrderCancelResult(
            broker_order=request.broker_order,
            status=OrderStatus.CANCELED,
            canceled_quantity=self.canceled_quantity,
            message="fake cancel accepted",
        )
