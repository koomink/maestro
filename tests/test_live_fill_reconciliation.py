import multiprocessing
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest
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
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
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


def test_fill_watermark_table_prevents_double_apply(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    _save_status(
        store,
        _status(
            order_id="KIS-10",
            status=OrderStatus.FILLED,
            filled=10.0,
            ordered=10.0,
            avg_price=70_000.0,
        ),
    )
    service = PartialFillReconciliationService(store, audit)

    first = service.reconcile_latest("run_fill_1")
    second = service.reconcile_latest("run_fill_2")

    state = store.load_latest_portfolio_state()
    assert first.applied_fills[0].quantity == 10.0
    assert second.applied_fills == []
    assert state.cash == 300_000.0
    assert state.positions == {"005930": 10.0}
    assert store.load_fill_watermarks()["KIS-10"] == (10.0, 700_000.0)


def test_fill_reconciliation_acquires_live_order_lock_before_writer_lock(tmp_path):
    """Pins the acquisition order itself, so a future edit cannot silently re-invert it.

    This test previously asserted the opposite order. That was a characterization
    of whatever 9e87e5d happened to write when it first wrapped this method in
    locks, not a requirement: both locks are held across the entire body, so
    either order gives identical mutual exclusion. Only the deadlock exposure
    differs, and writer-first was the inverted side.
    """
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    lock_order: list[str] = []
    original_writer_lock = store.writer_lock
    original_live_order_lock = store.live_order_lock

    @contextmanager
    def recording_writer_lock(owner: str, **kwargs):
        lock_order.append("writer")
        with original_writer_lock(owner, **kwargs):
            yield

    @contextmanager
    def recording_live_order_lock(owner: str, **kwargs):
        lock_order.append("live_order")
        with original_live_order_lock(owner, **kwargs):
            yield

    store.writer_lock = recording_writer_lock
    store.live_order_lock = recording_live_order_lock

    PartialFillReconciliationService(store, audit).reconcile_latest("run_fill_lock_order")

    assert lock_order[:2] == ["live_order", "writer"]


def test_fill_watermarks_seed_from_legacy_events(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=300_000.0, positions={"005930": 10.0}))
    checked_at = utc_now().isoformat()
    store.save_system_event(
        "run_legacy",
        "fill_reconciliation",
        {
            "run_id": "run_legacy",
            "checked_at": checked_at,
            "applied_fills": [
                {
                    "broker_order_id": "KIS-LEGACY",
                    "symbol": "005930",
                    "side": "buy",
                    "quantity": 10.0,
                    "price": 70_000.0,
                    "notional": 700_000.0,
                    "cumulative_filled_quantity": 10.0,
                    "cumulative_filled_notional": 700_000.0,
                    "status_checked_at": checked_at,
                }
            ],
            "skipped_fills": [],
            "portfolio_updated": True,
            "cash": 300_000.0,
            "positions": {"005930": 10.0},
        },
    )
    _save_status(
        store,
        _status(
            order_id="KIS-LEGACY",
            status=OrderStatus.FILLED,
            filled=10.0,
            ordered=10.0,
            avg_price=70_000.0,
        ),
    )

    result = PartialFillReconciliationService(store, audit).reconcile_latest("run_fill_new")

    state = store.load_latest_portfolio_state()
    assert result.applied_fills == []
    assert state.cash == 300_000.0
    assert state.positions == {"005930": 10.0}
    assert store.load_fill_watermarks()["KIS-LEGACY"] == (10.0, 700_000.0)


def test_fill_watermark_schema_migrates_existing_database_for_toss_costs(tmp_path):
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE fill_watermarks ("
            "broker_order_id TEXT PRIMARY KEY, "
            "cumulative_quantity REAL NOT NULL, "
            "cumulative_notional REAL NOT NULL, "
            "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional) "
            "VALUES ('TOSS-OLD', 1, 185.25)"
        )

    store = StateStore(str(database), initial_cash=0.0)

    assert store.load_fill_watermarks()["TOSS-OLD"] == (1.0, 185.25)
    assert store.load_fill_cost_watermarks()["TOSS-OLD"] == (0.0, 0.0)


def test_apply_fill_reconciliation_persists_snapshot_watermark_and_event(tmp_path):
    store, _ = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    state = PortfolioState(cash=930_000.0, positions={"005930": 1.0})
    payload = {
        "run_id": "run_atomic",
        "checked_at": utc_now().isoformat(),
        "applied_fills": [
            {
                "broker_order_id": "KIS-ATOMIC",
                "symbol": "005930",
                "side": "buy",
                "quantity": 1.0,
                "price": 70_000.0,
                "notional": 70_000.0,
                "cumulative_filled_quantity": 1.0,
                "cumulative_filled_notional": 70_000.0,
                "status_checked_at": utc_now().isoformat(),
            }
        ],
        "skipped_fills": [],
        "portfolio_updated": True,
        "cash": 930_000.0,
        "positions": {"005930": 1.0},
    }

    store.apply_fill_reconciliation(
        "run_atomic",
        state,
        {"KIS-ATOMIC": (1.0, 70_000.0)},
        payload,
    )

    assert store.load_latest_portfolio_state().positions == {"005930": 1.0}
    assert store.load_fill_watermarks()["KIS-ATOMIC"] == (1.0, 70_000.0)
    events = store.list_system_events_by_type("fill_reconciliation")
    assert events[0]["payload"]["applied_fills"][0]["broker_order_id"] == "KIS-ATOMIC"


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


def test_fill_reconciliation_updates_strategy_attribution_bucket(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000.0, positions={"QQQ": 1.0}))
    attribution = AccountAttributionReconciliationService(store, audit)
    attribution.reconcile_broker_snapshot(
        run_id="run_sync",
        account_id="toss_brokerage",
        broker_snapshot_id=10,
        broker_positions={"QQQ": 1.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )
    attribution.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="verified",
        adopted_by="cli",
    )
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {
                "account_id": "toss_brokerage",
                # sleeve carries the currency sleeve; the attribution bucket is
                # the execution sleeve, which is a different value entirely.
                "sleeve": "USD",
                "execution_sleeve": "crescendo_us",
            },
            "result": {"broker_order": {"broker_order_id": "TOSS-1"}},
        },
    )
    checked_at = utc_now().isoformat()
    _save_status(
        store,
        LiveOrderStatusSnapshot(
            broker_order=BrokerOrderId(
                broker="toss",
                broker_order_id="TOSS-1",
                order_id="ord_toss_1",
                submitted_at=checked_at,
                account_id="toss_brokerage",
            ),
            status=OrderStatus.FILLED,
            checked_at=checked_at,
            symbol="QQQ",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=1.0,
                filled_quantity=1.0,
                remaining_quantity=0.0,
                average_fill_price=100.0,
                fill_count=1,
            ),
        ),
    )

    PartialFillReconciliationService(store, audit).reconcile_latest("run_fill")

    latest = store.load_latest_system_event("account_attribution_reconciliation")
    strategy = [
        position
        for position in latest["payload"]["positions"]
        if position["bucket_id"] == "crescendo_us"
    ]
    assert strategy[0]["quantity"] == 2.0


def test_fill_reconciliation_does_not_attribute_a_sell_to_the_currency_sleeve(tmp_path):
    # Regression: the attribution bucket used to be read from request["sleeve"],
    # which carries the currency sleeve ("USD"), not the execution sleeve. A live
    # sell then validated against a nonexistent "USD" bucket and raised
    # AttributionValidationError, aborting reconciliation before the fill and cash
    # were persisted.
    store, audit = _context(tmp_path, PortfolioState(cash=1_000.0, positions={"QQQ": 5.0}))
    attribution = AccountAttributionReconciliationService(store, audit)
    attribution.reconcile_broker_snapshot(
        run_id="run_sync",
        account_id="toss_brokerage",
        broker_snapshot_id=10,
        broker_positions={"QQQ": 5.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )
    attribution.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="verified",
        adopted_by="cli",
    )
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {
                "account_id": "toss_brokerage",
                "sleeve": "USD",
            },
            "result": {"broker_order": {"broker_order_id": "TOSS-NO-SLEEVE"}},
        },
    )
    _save_status(store, _toss_sell_status("TOSS-NO-SLEEVE", quantity=5.0, price=670.0))

    result = PartialFillReconciliationService(store, audit).reconcile_latest("run_fill")

    # The fill still reaches the portfolio; only attribution is skipped.
    assert result.applied_fills[0].quantity == 5.0
    state = store.load_latest_portfolio_state()
    assert state.positions == {}
    assert state.cash == 4_350.0
    assert any(
        skipped.reason == "attribution_bucket_unresolved" for skipped in result.skipped_fills
    )
    latest = store.load_latest_system_event("account_attribution_reconciliation")
    assert not any(position["bucket_id"] == "USD" for position in latest["payload"]["positions"])


def test_fill_reconciliation_records_a_maestro_strategy_sell(tmp_path):
    store, audit = _context(tmp_path, PortfolioState(cash=1_000.0, positions={"QQQ": 5.0}))
    attribution = AccountAttributionReconciliationService(store, audit)
    attribution.reconcile_broker_snapshot(
        run_id="run_sync",
        account_id="toss_brokerage",
        broker_snapshot_id=10,
        broker_positions={"QQQ": 5.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )
    attribution.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="verified",
        adopted_by="cli",
    )
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {
                "account_id": "toss_brokerage",
                "sleeve": "USD",
                "execution_sleeve": "crescendo_us",
            },
            "result": {"broker_order": {"broker_order_id": "TOSS-SELL"}},
        },
    )
    _save_status(store, _toss_sell_status("TOSS-SELL", quantity=5.0, price=670.0))

    PartialFillReconciliationService(store, audit).reconcile_latest("run_fill")

    latest = store.load_latest_system_event("account_attribution_reconciliation")
    strategy = [
        position
        for position in latest["payload"]["positions"]
        if position["bucket_id"] == "crescendo_us"
    ]
    assert strategy == [] or strategy[0]["quantity"] == 0.0
    assert any(
        change.get("event_type") == "maestro_strategy_sell"
        for change in latest["payload"].get("changes") or []
    )


def _toss_sell_status(
    broker_order_id: str,
    *,
    quantity: float,
    price: float,
) -> LiveOrderStatusSnapshot:
    checked_at = utc_now().isoformat()
    return LiveOrderStatusSnapshot(
        broker_order=BrokerOrderId(
            broker="toss",
            broker_order_id=broker_order_id,
            order_id=f"ord_{broker_order_id}",
            submitted_at=checked_at,
            account_id="toss_brokerage",
        ),
        status=OrderStatus.FILLED,
        checked_at=checked_at,
        symbol="QQQ",
        side=OrderSide.SELL,
        partial_fill=PartialFillSummary(
            ordered_quantity=quantity,
            filled_quantity=quantity,
            remaining_quantity=0.0,
            average_fill_price=price,
            fill_count=1,
        ),
    )


def test_fill_reconciliation_updates_account_cash_and_kis_transaction_costs(tmp_path):
    initial = PortfolioState(
        cash=1_000_000.0,
        cash_by_currency={"KRW": 1_000_000.0},
        positions={},
    )
    store, audit = _context(tmp_path, initial)
    store.save_portfolio_snapshot("run_initial", initial, account_id="kis_isa")
    store.save_broker_account_snapshot(
        "run_before",
        "MOCK-ISA",
        _broker_snapshot_payload(
            account_id="kis_isa",
            cash=1_000_000.0,
            positions={},
            transaction_costs_today=0.0,
        ),
    )
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {
                "account_id": "kis_isa",
                "sleeve": "KRW",
                "execution_sleeve": "tranquillo_isa",
                "currency": "KRW",
            },
            "result": {"broker_order": {"broker_order_id": "KIS-ISA-1"}},
        },
    )
    _save_status(
        store,
        _status(
            order_id="KIS-ISA-1",
            status=OrderStatus.FILLED,
            filled=1.0,
            ordered=1.0,
            avg_price=70_000.0,
        ),
    )

    def refresh(account_id: str) -> None:
        assert account_id == "kis_isa"
        store.save_broker_account_snapshot(
            "run_after",
            "MOCK-ISA",
            _broker_snapshot_payload(
                account_id="kis_isa",
                cash=929_986.0,
                positions={"005930": 1.0},
                transaction_costs_today=14.0,
            ),
        )

    result = PartialFillReconciliationService(
        store,
        audit,
        account_snapshot_refresher=refresh,
    ).reconcile_latest("run_fill")

    aggregate = store.load_latest_portfolio_state()
    account = store.load_latest_account_portfolio_state("kis_isa")
    assert aggregate.cash == 929_986.0
    assert aggregate.cash_by_currency == {"KRW": 929_986.0}
    assert aggregate.positions == {"005930": 1.0}
    assert account is not None
    assert account.cash == 929_986.0
    assert account.cash_by_currency == {"KRW": 929_986.0}
    assert account.positions == {"005930": 1.0}
    assert result.settlement_cash_adjustments[0].amount == -14.0
    assert result.settlement_cash_adjustments[0].account_id == "kis_isa"


def test_settlement_day_cash_change_preserves_projected_broker_cash(tmp_path):
    state = PortfolioState(
        cash=929_986.0,
        cash_by_currency={"KRW": 929_986.0},
        positions={"005930": 1.0},
    )
    store, _ = _context(tmp_path, state)
    store.save_portfolio_snapshot("run_account", state, account_id="kis_isa")
    store.save_broker_account_snapshot(
        "run_settled",
        "MOCK-ISA",
        _broker_snapshot_payload(
            account_id="kis_isa",
            cash=929_986.0,
            positions={"005930": 1.0},
            transaction_costs_today=0.0,
            settled_cash=929_986.0,
        ),
    )

    account = store.load_latest_account_portfolio_state("kis_isa")
    broker = store.load_latest_broker_account_snapshot()["payload"]["account"]

    assert account is not None
    assert account.cash_by_currency == broker["cash_by_currency"]
    assert broker["cash_balance"]["settled_cash"] == 929_986.0


def test_toss_execution_costs_are_applied_once_across_partial_fills(tmp_path):
    initial = PortfolioState(
        cash=1_000.0,
        cash_by_currency={"USD": 1_000.0},
        positions={},
    )
    store, audit = _context(tmp_path, initial)
    store.save_portfolio_snapshot("run_account", initial, account_id="toss_brokerage")
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {
                "account_id": "toss_brokerage",
                "sleeve": "USD",
                "execution_sleeve": "crescendo_us",
                "currency": "USD",
            },
            "result": {"broker_order": {"broker_order_id": "TOSS-FEE-1"}},
        },
    )

    def toss_status(
        *,
        filled: float,
        amount: float,
        commission: float,
        tax: float,
        status: OrderStatus,
    ) -> LiveOrderStatusSnapshot:
        checked_at = utc_now().isoformat()
        return LiveOrderStatusSnapshot(
            broker_order=BrokerOrderId(
                broker="toss",
                broker_order_id="TOSS-FEE-1",
                order_id="ord_toss_fee_1",
                submitted_at=checked_at,
                account_id="toss_brokerage",
            ),
            status=status,
            checked_at=checked_at,
            symbol="AAPL",
            side=OrderSide.BUY,
            currency="USD",
            cumulative_filled_amount=amount,
            cumulative_commission=commission,
            cumulative_tax=tax,
            settlement_date="2026-07-23",
            partial_fill=PartialFillSummary(
                ordered_quantity=2.0,
                filled_quantity=filled,
                remaining_quantity=2.0 - filled,
                average_fill_price=amount / filled,
                fill_count=1,
            ),
        )

    _save_status(
        store,
        toss_status(
            filled=1.0,
            amount=185.25,
            commission=0.18525,
            tax=0.01,
            status=OrderStatus.PARTIALLY_FILLED,
        ),
    )
    service = PartialFillReconciliationService(store, audit)
    first = service.reconcile_latest("run_toss_partial")
    duplicate = service.reconcile_latest("run_toss_duplicate")
    _save_status(
        store,
        toss_status(
            filled=1.0,
            amount=185.25,
            commission=0.2,
            tax=0.01,
            status=OrderStatus.PARTIALLY_FILLED,
        ),
    )
    late_cost = service.reconcile_latest("run_toss_late_cost")
    _save_status(
        store,
        toss_status(
            filled=2.0,
            amount=380.0,
            commission=0.38,
            tax=0.02,
            status=OrderStatus.FILLED,
        ),
    )
    final = service.reconcile_latest("run_toss_filled")

    aggregate = store.load_latest_portfolio_state()
    account = store.load_latest_account_portfolio_state("toss_brokerage")
    assert first.settlement_cash_adjustments[0].source == "toss_order_execution"
    assert first.settlement_cash_adjustments[0].amount == pytest.approx(-0.19525)
    assert duplicate.settlement_cash_adjustments == []
    assert late_cost.applied_fills == []
    assert late_cost.settlement_cash_adjustments[0].amount == pytest.approx(-0.01475)
    assert final.applied_fills[0].notional == pytest.approx(194.75)
    assert final.settlement_cash_adjustments[0].amount == pytest.approx(-0.19)
    assert aggregate.cash_by_currency == pytest.approx({"USD": 619.6})
    assert aggregate.positions == {"AAPL": 2.0}
    assert account is not None
    assert account.cash_by_currency == pytest.approx({"USD": 619.6})
    assert store.load_fill_cost_watermarks()["TOSS-FEE-1"] == pytest.approx((0.38, 0.02))


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
    assert len(store.list_portfolio_snapshots(limit=10)) == 1
    assert (
        store.list_system_events_by_type("fill_reconciliation")[0]["payload"]["applied_fills"] == []
    )


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


def _hold_live_order_lock_then_take_writer(db_path, ready, go, result) -> None:
    """Run in a separate process so flock actually contends (it is per-process)."""
    store = StateStore(db_path)
    with store.live_order_lock("outside_live_holder", timeout_seconds=10.0):
        ready.set()
        go.wait(20.0)
        try:
            with store.writer_lock("outside_writer", timeout_seconds=2.0):
                result["writer"] = "acquired"
        except TimeoutError as exc:
            result["writer"] = "timeout"
            result["message"] = str(exc)


def test_fill_reconciliation_takes_the_live_order_lock_before_the_writer_lock(tmp_path):
    """A holder of live_order_lock must still be able to take writer_lock.

    reconcile_latest used to take writer_lock first and then live_order_lock,
    the inverse of every other live-order path (resolve_pending_signal_approval,
    submit_approved_order, workflow_recovery). That inversion deadlocked the
    2026-08-11 and 2026-08-12 US rotations: the operator held live_order_lock
    across a broker submit and waited for writer_lock, while the 2-minutely
    resume-order-tracking job held writer_lock inside reconcile_latest and
    waited for live_order_lock. Both timed out and died.
    """
    store, audit = _context(tmp_path, PortfolioState(cash=1_000_000.0, positions={}))
    _save_status(store, _status(order_id="KIS-1", filled=1.0, ordered=3.0, avg_price=70_000.0))
    manager = multiprocessing.Manager()
    ready = manager.Event()
    go = manager.Event()
    result = manager.dict()
    holder = multiprocessing.Process(
        target=_hold_live_order_lock_then_take_writer,
        args=(str(tmp_path / "state.db"), ready, go, result),
    )
    reconcile_errors: list[BaseException] = []

    def _reconcile() -> None:
        try:
            PartialFillReconciliationService(store, audit).reconcile_latest("run_lock_order")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            reconcile_errors.append(exc)

    holder.start()
    reconcile = threading.Thread(target=_reconcile)
    try:
        assert ready.wait(20.0), "the outside process never took live_order_lock"
        reconcile.start()
        # Release the outside writer attempt only once reconcile_latest is
        # committed to its first lock. Under the inverted order it grabs
        # writer_lock, so this loop exits immediately and the deadlock is
        # reproduced deterministically rather than by sleeping and hoping.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            record = StateStore.read_lock_holder(store.writer_lock_path)
            if record is not None and record.get("owner") == "fill_reconciliation":
                break
            time.sleep(0.02)
        go.set()
        holder.join(30.0)
        reconcile.join(60.0)
    finally:
        go.set()
        if holder.is_alive():
            holder.terminate()
        holder.join(10.0)
        if reconcile.is_alive():
            reconcile.join(30.0)

    assert result.get("writer") == "acquired", (
        "holding live_order_lock blocked writer_lock, so reconcile_latest still "
        f"inverts the lock order: {result.get('message')}"
    )
    assert reconcile_errors == []
    assert reconcile.is_alive() is False


def _context(tmp_path, state: PortfolioState) -> tuple[StateStore, AuditLogger]:
    store = StateStore(str(tmp_path / "state.db"), initial_cash=state.cash)
    store.save_portfolio_snapshot("run_initial", state)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    return store, audit


def _save_status(store: StateStore, snapshot: LiveOrderStatusSnapshot) -> None:
    store.save_system_event("run_status", "live_order_status", snapshot.model_dump(mode="json"))


def _broker_snapshot_payload(
    *,
    account_id: str,
    cash: float,
    positions: dict[str, float],
    transaction_costs_today: float,
    settled_cash: float = 1_000_000.0,
) -> dict:
    return {
        "account_id": account_id,
        "account": {
            "account_id": "MOCK-ISA",
            "cash": cash,
            "cash_by_currency": {"KRW": cash},
            "buying_power": cash,
            "positions": [
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "average_price": 70_000.0,
                    "current_price": 70_000.0,
                    "currency": "KRW",
                }
                for symbol, quantity in positions.items()
            ],
            "cash_balance": {
                "currency": "KRW",
                "cash": cash,
                "withdrawable_cash": settled_cash,
                "settled_cash": settled_cash,
                "projected_settlement_cash": cash,
                "transaction_costs_today": transaction_costs_today,
            },
            "source": "kis_rest_readonly",
        },
    }


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
