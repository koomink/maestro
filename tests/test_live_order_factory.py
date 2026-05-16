import pytest

from maestro.approval.models import ApprovalDecision
from maestro.config.models import (
    ApprovalConfig,
    AuditConfig,
    ExecutionConfig,
    KISConfig,
    MaestroConfig,
    PortfolioConfig,
    ReconciliationConfig,
    RiskConfig,
    StateConfig,
)
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, RunMode
from maestro.execution.brokers.kis.domestic_live_order import KISRestDomesticStockLiveOrderClient
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_order_factory import (
    _build_live_order_client,
    build_live_approval_dependencies,
)
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderCancelClient,
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderClient,
    LiveOrderLifecycleNotification,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_live_approval_factory_wires_fake_clients_and_lifecycle_success(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    config = _config(tmp_path)
    submit_client = FakeLiveOrderClient()
    status_client = FakeLiveOrderStatusClient(OrderStatus.FILLED, filled=2.0)
    notification_client = FakeNotificationClient()
    store.save_system_event("run_reconcile_initial", "broker_reconciliation", {"passed": True})

    dependencies = build_live_approval_dependencies(
        config,
        store,
        audit,
        live_order_client=submit_client,
        status_client=status_client,
        notification_client=notification_client,
        broker_reconciliation_service=FakeBrokerReconciliation(),
    )
    request = _request()
    result = dependencies.lifecycle_service.run(request, _approval(request))

    assert result.final_status == OrderStatus.FILLED, result.failed_reason
    assert submit_client.requests == [request]
    assert status_client.requests == ["KIS-1"]
    assert notification_client.events[-1].status == OrderStatus.FILLED
    assert store.load_latest_portfolio_state().positions == {"005930": 2.0}


def test_live_approval_factory_refreshes_kis_snapshot_before_reconciliation(
    tmp_path,
    monkeypatch,
):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config = _config(tmp_path).model_copy(
        update={"kis": KISConfig(enabled=True, provider="kis", account_id="12345678-01")}
    )
    refresh_calls = []
    store.save_system_event("run_reconcile_initial", "broker_reconciliation", {"passed": True})

    def fetch_snapshot(self, symbols):
        refresh_calls.append(list(symbols))
        self.state_store.save_broker_account_snapshot(
            "run_refreshed_broker_snapshot",
            "TEST-ACCOUNT",
            {
                "account": {
                    "account_id": "TEST-ACCOUNT",
                    "cash": 860_000.0,
                    "buying_power": 860_000.0,
                    "positions": [
                        {
                            "symbol": "005930",
                            "quantity": 2.0,
                            "average_price": 70_000.0,
                            "current_price": 70_000.0,
                        }
                    ],
                    "source": "test",
                },
                "current_prices": {"005930": 70_000.0},
                "order_fills": [],
                "unfilled_orders": [],
            },
        )

    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)

    dependencies = build_live_approval_dependencies(
        config,
        store,
        audit,
        live_order_client=FakeLiveOrderClient(),
        status_client=FakeLiveOrderStatusClient(OrderStatus.FILLED, filled=2.0),
    )
    request = _request()
    result = dependencies.lifecycle_service.run(request, _approval(request))

    reconciliation_events = [
        event
        for event in store.list_system_events()
        if event["event_type"] == "broker_reconciliation"
    ]
    assert result.final_status == OrderStatus.FILLED, result.failed_reason
    assert refresh_calls == [["CASH", "005930"]]
    assert reconciliation_events[0]["payload"]["passed"] is True


def test_live_approval_factory_can_wire_cancel_service_with_fake_client(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    config = _config(tmp_path)
    cancel_client = FakeCancelClient()
    broker_order = _broker_order()
    store.save_system_event("run_reconcile_initial", "broker_reconciliation", {"passed": True})
    store.save_system_event(
        "run_status",
        "live_order_status",
        LiveOrderStatusSnapshot(
            broker_order=broker_order,
            status=OrderStatus.OPEN,
            checked_at=utc_now().isoformat(),
            symbol="005930",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=2.0,
                filled_quantity=0.0,
                remaining_quantity=2.0,
            ),
        ).model_dump(mode="json"),
    )

    dependencies = build_live_approval_dependencies(
        config,
        store,
        audit,
        live_order_client=FakeLiveOrderClient(),
        status_client=FakeLiveOrderStatusClient(OrderStatus.OPEN),
        cancel_client=cancel_client,
    )
    request = LiveOrderCancelRequest(
        run_id="run_live",
        approval_id="appr_live",
        broker_order=broker_order,
        reason="operator approved cancel",
    )
    result = dependencies.cancel_service.cancel_order(request, _cancel_approval())

    assert result.status == OrderStatus.CANCELED
    assert cancel_client.requests == [request]


def test_live_approval_factory_requires_injected_clients_for_mock_provider(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    with pytest.raises(ValueError, match="injected fake client"):
        build_live_approval_dependencies(_config(tmp_path), store, audit)


def test_single_broker_products_list_selects_live_order_product_without_default(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config = _config(tmp_path).model_copy(
        update={
            "kis": KISConfig(
                enabled=True,
                provider="kis",
                account_id="12345678-01",
                broker_products=["kis_domestic_stock"],
            )
        }
    )

    client = _build_live_order_client(config)

    assert isinstance(client, KISRestDomesticStockLiveOrderClient)


def _config(tmp_path) -> MaestroConfig:
    return MaestroConfig(
        mode=RunMode.LIVE_APPROVAL,
        portfolio=PortfolioConfig(
            allowed_symbols=["CASH", "005930"],
        ),
        strategies=[],
        execution=ExecutionConfig(
            live_order_enabled=True,
            max_live_order_notional=200_000.0,
            max_daily_live_notional=300_000.0,
            order_status_max_polls=1,
            order_status_poll_interval_seconds=0.0,
        ),
        risk=RiskConfig(max_single_asset_weight=0.8, min_cash_weight=0.0),
        state=StateConfig(sqlite_path=str(tmp_path / "state.db")),
        audit=AuditConfig(jsonl_path=str(tmp_path / "audit.jsonl")),
        approval=ApprovalConfig(enabled=True, require_approval=True),
        kis=KISConfig(enabled=True, provider="mock"),
        reconciliation=ReconciliationConfig(),
    )


def _request() -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id="ord_live",
        symbol="005930",
        side=OrderSide.BUY,
        quantity=2.0,
        limit_price=70_000.0,
        approval_id="appr_live",
        run_id="run_live",
        duplicate_key="run_live:ord_live",
    )


def _approval(request: LiveOrderRequest) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _cancel_approval() -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="appr_live",
        run_id="run_live",
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _broker_order() -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id="KIS-1",
        broker_order_org_no="KRX",
        order_id="ord_live",
        submitted_at=utc_now().isoformat(),
    )


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=_broker_order(),
        )


class FakeLiveOrderStatusClient(LiveOrderStatusClient):
    def __init__(self, status: OrderStatus, *, filled: float = 0.0) -> None:
        self.status = status
        self.filled = filled
        self.requests: list[str] = []

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        self.requests.append(broker_order_id.broker_order_id)
        average_fill_price = 70_000.0 if self.filled else None
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=self.status,
            checked_at=utc_now().isoformat(),
            symbol="005930",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=2.0,
                filled_quantity=self.filled,
                remaining_quantity=max(2.0 - self.filled, 0.0),
                average_fill_price=average_fill_price,
                fill_count=1 if self.filled else 0,
            ),
        )


class FakeNotificationClient(LiveOrderNotificationClient):
    def __init__(self) -> None:
        self.events: list[LiveOrderLifecycleNotification] = []

    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        self.events.append(event)


class FakeCancelClient(LiveOrderCancelClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderCancelRequest] = []

    def cancel_order(self, request: LiveOrderCancelRequest) -> LiveOrderCancelResult:
        self.requests.append(request)
        return LiveOrderCancelResult(
            broker_order=request.broker_order,
            status=OrderStatus.CANCELED,
            canceled_quantity=2.0,
            message="fake cancel accepted",
        )


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id="run_broker_reconcile",
            passed=True,
            checked_at=utc_now().isoformat(),
            issues=[],
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )
