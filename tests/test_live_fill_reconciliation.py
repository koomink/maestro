import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_orders import (
    BrokerOrderId,
    FillEvent,
    LiveOrderStatusSnapshot,
    PartialFillReconciliationService,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_first_partial_fill_updates_cash_and_position(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))

    result = PartialFillReconciliationService(store, audit).reconcile_latest("run_fill_1")

    state = store.load_latest_portfolio_state()
    assert result.portfolio_updated is True
    assert result.applied_fills[0].quantity == 1.0
    assert result.applied_fills[0].notional == 70_000.0
    assert state.cash == 930_000.0
    assert state.positions == {"005930": 1.0}


def test_duplicate_snapshot_does_not_double_count(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    snapshot = _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0)
    _save_status(store, snapshot)
    service = PartialFillReconciliationService(store, audit)
    service.reconcile_latest("run_fill_1")
    _save_status(store, snapshot)

    result = service.reconcile_latest("run_fill_2")

    state = store.load_latest_portfolio_state()
    assert result.portfolio_updated is False
    assert result.applied_fills == []
    assert result.skipped_fills[-1].reason == "duplicate_or_no_new_fill"
    assert state.cash == 930_000.0
    assert state.positions == {"005930": 1.0}


def test_later_additional_fill_applies_only_delta(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    service = PartialFillReconciliationService(store, audit)
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))
    service.reconcile_latest("run_fill_1")
    _save_status(store, _status(order_id="KIS-1", filled=2.0, ordered=3.0, avg_price=75_000.0))

    result = service.reconcile_latest("run_fill_2")

    state = store.load_latest_portfolio_state()
    assert result.applied_fills[0].quantity == 1.0
    assert result.applied_fills[0].notional == 80_000.0
    assert result.applied_fills[0].price == 80_000.0
    assert state.cash == 850_000.0
    assert state.positions == {"005930": 2.0}


def test_full_fill_after_partial_applies_remaining_delta(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    service = PartialFillReconciliationService(store, audit)
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))
    service.reconcile_latest("run_fill_1")
    _save_status(
        store,
        _status(
            order_id="KIS-1",
            status=OrderStatus.FILLED,
            filled=3.0,
            ordered=3.0,
            avg_price=80_000.0,
        ),
    )

    result = service.reconcile_latest("run_fill_2")

    state = store.load_latest_portfolio_state()
    assert result.applied_fills[0].quantity == 2.0
    assert result.applied_fills[0].notional == 170_000.0
    assert result.applied_fills[0].price == 85_000.0
    assert state.cash == 760_000.0
    assert state.positions == {"005930": 3.0}


def test_sell_fill_updates_cash_and_position(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=100_000.0, positions={"005930": 3.0}))
    _save_status(
        store,
        _status(
            order_id="KIS-SELL-1",
            side=OrderSide.SELL,
            status=OrderStatus.FILLED,
            filled=2.0,
            ordered=2.0,
            avg_price=90_000.0,
        ),
    )

    result = PartialFillReconciliationService(store, audit).reconcile_latest("run_sell_1")

    state = store.load_latest_portfolio_state()
    assert result.applied_fills[0].side == OrderSide.SELL
    assert state.cash == 280_000.0
    assert state.positions == {"005930": 1.0}


def test_rejected_canceled_halted_and_unknown_do_not_update_portfolio(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    for status in [
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.HALTED,
        OrderStatus.UNKNOWN,
    ]:
        _save_status(
            store,
            _status(order_id=f"KIS-{status}", status=status, filled=0.0, ordered=2.0),
        )

    result = PartialFillReconciliationService(store, audit).reconcile_latest("run_skip_1")

    state = store.load_latest_portfolio_state()
    assert result.portfolio_updated is False
    assert result.applied_fills == []
    assert len(result.skipped_fills) == 4
    assert state.cash == 1_000_000.0
    assert state.positions == {}


def test_fill_reconciliation_event_and_audit_are_persisted(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))

    PartialFillReconciliationService(store, audit).reconcile_latest("run_fill_1")

    events = store.list_system_events_by_type("fill_reconciliation")
    assert events[0]["payload"]["applied_fills"][0]["broker_order_id"] == "KIS-1"
    assert "fill_reconciliation" in audit.path.read_text()


def test_reconcile_fills_cli_outputs_result(tmp_path):
    store, _ = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "live_readonly",
                "portfolio": {
                    "base_currency": "KRW",
                    "allowed_symbols": ["CASH", "005930"],
                },
                "strategies": [],
                "datahub": {"provider": "mock"},
                "execution": {"engine": "paper"},
                "risk": {"max_single_asset_weight": 0.4, "min_cash_weight": 0.05},
                "state": {"sqlite_path": str(tmp_path / "state.db")},
                "audit": {"jsonl_path": str(tmp_path / "audit.jsonl")},
                "kis": {"enabled": True, "provider": "mock", "account_id": "MOCK-ACCOUNT"},
            }
        )
    )

    result = CliRunner().invoke(app, ["reconcile-fills", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "applied_fills=1" in result.output
    assert "portfolio_updated=true" in result.output


def _context(tmp_path, state: PortfolioState) -> tuple[StateStore, AuditLogger]:
    store = StateStore(str(tmp_path / "state.db"), initial_cash=state.cash)
    store.save_portfolio_snapshot("run_initial", state)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    return store, audit


def _save_status(store: StateStore, snapshot: LiveOrderStatusSnapshot) -> None:
    store.save_system_event("run_status", "live_order_status", snapshot.model_dump(mode="json"))


def _status(
    *,
    order_id: str,
    filled: float,
    ordered: float,
    avg_price: float = 70_000.0,
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.PARTIALLY_FILLED,
) -> LiveOrderStatusSnapshot:
    checked_at = utc_now().isoformat()
    return LiveOrderStatusSnapshot(
        broker_order=BrokerOrderId(
            broker="kis",
            broker_order_id=order_id,
            broker_order_org_no="KRX",
            order_id=f"ord_{order_id}",
            submitted_at=checked_at,
        ),
        status=status,
        checked_at=checked_at,
        symbol="005930",
        side=side,
        partial_fill=PartialFillSummary(
            ordered_quantity=ordered,
            filled_quantity=filled,
            remaining_quantity=max(ordered - filled, 0.0),
            average_fill_price=avg_price,
            fill_count=1 if filled else 0,
        ),
        fills=[
            FillEvent(
                broker_order_id=order_id,
                symbol="005930",
                quantity=filled,
                price=avg_price,
                filled_at=checked_at,
            )
        ]
        if filled
        else [],
        raw_status=status.value,
    )
