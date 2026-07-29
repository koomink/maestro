from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_order_tracking import LiveOrderTrackingResumeService
from maestro.execution.live_orders import (
    BrokerOrderId,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_resume_applies_a_fill_that_arrived_after_the_poll_window_closed(tmp_path):
    """The whole point of the resume path: recover an unobserved fill.

    Fill reconciliation replays recorded status snapshots, so a fill that landed
    after the last poll is invisible until something polls the broker again.
    """
    store, audit = _stores(tmp_path)
    _record_outstanding(store, broker_order_id="0004931100")
    filled = _snapshot(OrderStatus.FILLED, filled=37.0, remaining=0.0, average_fill_price=13_395.0)
    service = _service(store, audit, {"0004931100": filled})

    summary = service.resume("run_resume_1")

    assert summary["outstanding_orders"] == 1
    assert summary["resolved_order_ids"] == ["ord_kodex_1"]
    assert summary["still_open_order_ids"] == []
    assert [fill["quantity"] for fill in summary["applied_fills"]] == [37.0]
    assert store.load_latest_portfolio_state().positions == {"KODEX": 37.0}
    resolved = store.list_system_events_by_type("live_order_tracking_resolved", limit=10)
    assert resolved[0]["payload"]["final_status"] == OrderStatus.FILLED.value


def test_resume_leaves_a_still_working_order_outstanding_for_the_next_run(tmp_path):
    store, audit = _stores(tmp_path)
    _record_outstanding(store, broker_order_id="0004931100")
    service = _service(store, audit, {"0004931100": _snapshot(OrderStatus.OPEN)})

    summary = service.resume("run_resume_1")

    assert summary["still_open_order_ids"] == ["ord_kodex_1"]
    assert summary["resolved_order_ids"] == []
    assert store.list_system_events_by_type("live_order_tracking_resolved", limit=10) == []
    # Still outstanding, so a later run picks it up again.
    assert [order.order_id for order in service.list_outstanding_orders()] == ["ord_kodex_1"]


def test_resume_skips_an_order_that_already_reached_a_terminal_status(tmp_path):
    store, audit = _stores(tmp_path)
    _record_outstanding(store, broker_order_id="0004931100")
    store.save_system_event(
        "run_later",
        "live_order_status",
        _snapshot(OrderStatus.CANCELED).model_dump(mode="json"),
    )
    service = _service(store, audit, {"0004931100": _snapshot(OrderStatus.FILLED, filled=37.0)})

    summary = service.resume("run_resume_1")

    assert summary["outstanding_orders"] == 0
    assert summary["polled"] == []


def test_resume_continues_past_an_order_whose_poll_fails(tmp_path):
    store, audit = _stores(tmp_path)
    _record_outstanding(store, broker_order_id="0004931100", order_id="ord_broken")
    _record_outstanding(store, broker_order_id="0004931200", order_id="ord_ok")
    filled = _snapshot(
        OrderStatus.FILLED,
        broker_order_id="0004931200",
        filled=5.0,
        remaining=0.0,
        average_fill_price=100.0,
    )
    service = _service(
        store,
        audit,
        {"0004931200": filled},
        failing_broker_order_ids={"0004931100"},
    )

    summary = service.resume("run_resume_1")

    assert summary["resolved_order_ids"] == ["ord_ok"]
    assert [failure["order_id"] for failure in summary["failures"]] == ["ord_broken"]
    # The reachable order was still reconciled despite the other one failing.
    assert store.load_latest_portfolio_state().positions == {"KODEX": 5.0}


def test_resume_polls_each_order_with_its_own_account_client(tmp_path):
    store, audit = _stores(tmp_path)
    _record_outstanding(store, broker_order_id="0004931100", order_id="ord_ps", account_id="kis_ps")
    _record_outstanding(
        store, broker_order_id="0004931200", order_id="ord_isa", account_id="kis_isa"
    )
    requested_accounts: list[str | None] = []

    def status_client_for(account_id: str | None) -> LiveOrderStatusClient:
        requested_accounts.append(account_id)
        return FakeStatusClient({}, failing_broker_order_ids=set())

    service = LiveOrderTrackingResumeService(store, audit, status_client_for)
    service.resume("run_resume_1")

    assert sorted(requested_accounts) == ["kis_isa", "kis_ps"]


class FakeStatusClient(LiveOrderStatusClient):
    def __init__(
        self,
        snapshots: dict[str, LiveOrderStatusSnapshot],
        failing_broker_order_ids: set[str],
    ) -> None:
        self.snapshots = snapshots
        self.failing_broker_order_ids = failing_broker_order_ids
        self.polled: list[str] = []

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        self.polled.append(broker_order_id.broker_order_id)
        if broker_order_id.broker_order_id in self.failing_broker_order_ids:
            raise RuntimeError("broker unreachable")
        snapshot = self.snapshots.get(broker_order_id.broker_order_id)
        if snapshot is None:
            return _snapshot(OrderStatus.OPEN, broker_order_id=broker_order_id.broker_order_id)
        return snapshot


def _stores(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    return store, audit


def _service(store, audit, snapshots, *, failing_broker_order_ids=None):
    client = FakeStatusClient(snapshots, failing_broker_order_ids or set())
    return LiveOrderTrackingResumeService(store, audit, lambda account_id: client)


def _broker_order(broker_order_id: str, order_id: str, account_id: str) -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id=broker_order_id,
        order_id=order_id,
        submitted_at=utc_now().isoformat(),
        account_id=account_id,
        broker_product="kis_domestic_stock",
    )


def _record_outstanding(
    store,
    *,
    broker_order_id: str,
    order_id: str = "ord_kodex_1",
    account_id: str = "kis_ps",
) -> None:
    store.save_system_event(
        "run_signal_1",
        "live_order_tracking_incomplete",
        {
            "reason": "poll_window_closed_before_terminal_status",
            "order_id": order_id,
            "broker_order": _broker_order(broker_order_id, order_id, account_id).model_dump(
                mode="json"
            ),
            "broker_order_id": broker_order_id,
            "last_status": OrderStatus.OPEN.value,
            "poll_count": 20,
            "checked_at": utc_now().isoformat(),
        },
    )


def _snapshot(
    status: OrderStatus,
    *,
    broker_order_id: str = "0004931100",
    order_id: str = "ord_kodex_1",
    filled: float = 0.0,
    remaining: float = 37.0,
    average_fill_price: float | None = None,
) -> LiveOrderStatusSnapshot:
    return LiveOrderStatusSnapshot(
        broker_order=_broker_order(broker_order_id, order_id, "kis_ps"),
        status=status,
        checked_at=utc_now().isoformat(),
        symbol="KODEX",
        side=OrderSide.BUY,
        partial_fill=PartialFillSummary(
            ordered_quantity=37.0,
            filled_quantity=filled,
            remaining_quantity=remaining,
            average_fill_price=average_fill_price,
            fill_count=1 if filled else 0,
        ),
    )
