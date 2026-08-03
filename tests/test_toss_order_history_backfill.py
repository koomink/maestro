import json
import sqlite3
from datetime import UTC, date, datetime

from maestro.execution.brokers.readonly import BrokerOrderSummary
from maestro.execution.brokers.toss.order_history_backfill import (
    TossOrderHistoryBackfillService,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class _Client:
    def __init__(self, orders):
        self.orders = orders

    def list_orders(self, *, status, from_date, to_date):
        del from_date, to_date
        return [order for order in self.orders if (status == "CLOSED" or order.status == "OPEN")]


def _order(
    order_id: str,
    *,
    filled_quantity: float = 2.0,
    commission: float = 1.0,
) -> BrokerOrderSummary:
    return BrokerOrderSummary(
        order_id=order_id,
        symbol="QQQ",
        side="buy",
        quantity=filled_quantity,
        status="FILLED",
        submitted_at=datetime(2026, 7, 1, tzinfo=UTC),
        filled_quantity=filled_quantity,
        average_fill_price=25.0,
        currency="USD",
        cumulative_commission=commission,
        cumulative_tax=0.0,
    )


def test_maestro_order_history_backfill_is_cost_only(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=100.0)
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=100.0, cash_by_currency={"USD": 100.0}, positions={}),
        account_id="toss_brokerage",
    )
    store.save_system_event(
        "run_submit",
        "live_order_result",
        {
            "request": {"order_id": "maestro-order", "account_id": "toss_brokerage"},
            "result": {
                "broker_order": {"broker_order_id": "maestro-order"},
            },
        },
    )
    service = TossOrderHistoryBackfillService(
        _Client([_order("maestro-order")]),
        store,
        AuditLogger(str(tmp_path / "audit.jsonl")),
    )

    payload = service.backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )

    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state is not None
    assert state.positions == {}
    assert state.cash_by_currency == {"USD": 99.0}
    assert payload["applied_count"] == 1
    assert store.load_fill_watermarks()["maestro-order"] == (0.0, 0.0)


def test_external_order_history_backfill_applies_principal_and_cost(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=100.0)
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=100.0, cash_by_currency={"USD": 100.0}, positions={}),
        account_id="toss_brokerage",
    )
    store.save_system_event(
        "signal-preflight",
        "broker_snapshot_adopted",
        {
            "account_id": "toss_brokerage",
            "reason": "account-scoped signal preflight",
            "positions": {"QQQ": 2.0},
        },
    )
    service = TossOrderHistoryBackfillService(
        _Client([_order("external-order")]),
        store,
        AuditLogger(str(tmp_path / "audit.jsonl")),
    )

    service.backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )

    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state is not None
    assert state.positions == {"QQQ": 2.0}
    assert state.cash_by_currency == {"USD": 49.0}


def test_order_history_before_opening_baseline_is_not_reapplied(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=49.0)
    baseline = PortfolioState(
        cash=49.0,
        cash_by_currency={"USD": 49.0},
        positions={"QQQ": 2.0},
    )
    store.save_portfolio_snapshot("baseline", baseline, account_id="toss_brokerage")
    store.save_system_event(
        "baseline",
        "ledger_opening_baseline",
        {
            "account_id": "toss_brokerage",
            "currency": "USD",
            "amount": 49.0,
            "effective_at": "2026-07-02T00:00:00+00:00",
        },
    )
    service = TossOrderHistoryBackfillService(
        _Client([_order("pre-baseline")]),
        store,
        AuditLogger(str(tmp_path / "audit.jsonl")),
    )

    service.backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )

    assert store.load_latest_account_portfolio_state("toss_brokerage") == baseline
    assert store.load_fill_watermarks()["pre-baseline"] == (2.0, 50.0)


def test_legacy_cash_snapshot_adoption_is_a_cash_baseline(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=49.0)
    baseline = PortfolioState(
        cash=49.0,
        cash_by_currency={"USD": 49.0},
        positions={"QQQ": 2.0},
    )
    store.save_portfolio_snapshot("baseline", baseline, account_id="toss_brokerage")
    store.save_system_event(
        "baseline",
        "broker_snapshot_adopted",
        {
            "account_id": "toss_brokerage",
            "broker_snapshot_id": 10,
            # Version-1 adoption events predate include_cash. Those commands
            # always copied broker cash and positions into the ledger.
            "cash": 49.0,
            "cash_by_currency": {"USD": 49.0},
            "positions": {"QQQ": 2.0},
        },
    )
    service = TossOrderHistoryBackfillService(
        _Client([_order("legacy-baseline")]),
        store,
        AuditLogger(str(tmp_path / "audit.jsonl")),
    )

    service.backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )

    assert store.load_latest_account_portfolio_state("toss_brokerage") == baseline
    assert store.load_fill_watermarks()["legacy-baseline"] == (2.0, 50.0)
    assert store.load_fill_cost_watermarks()["legacy-baseline"] == (1.0, 0.0)


def test_fill_increment_after_cash_baseline_applies_only_increment(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=49.0)
    baseline = PortfolioState(
        cash=49.0,
        cash_by_currency={"USD": 49.0},
        positions={"QQQ": 2.0},
    )
    store.save_portfolio_snapshot("baseline", baseline, account_id="toss_brokerage")
    store.save_system_event(
        "baseline",
        "broker_snapshot_adopted",
        {
            "account_id": "toss_brokerage",
            "broker_snapshot_id": 10,
            "include_cash": True,
            "cash": 49.0,
            "cash_by_currency": {"USD": 49.0},
            "positions": {"QQQ": 2.0},
        },
    )
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    TossOrderHistoryBackfillService(
        _Client([_order("partial-after-baseline")]), store, audit
    ).backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )
    TossOrderHistoryBackfillService(
        _Client(
            [
                _order(
                    "partial-after-baseline",
                    filled_quantity=3.0,
                    commission=1.5,
                )
            ]
        ),
        store,
        audit,
    ).backfill(
        "toss_brokerage",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 2),
    )

    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state is not None
    assert state.positions == {"QQQ": 3.0}
    assert state.cash_by_currency == {"USD": 23.5}
    assert store.load_fill_watermarks()["partial-after-baseline"] == (3.0, 75.0)
    assert store.load_fill_cost_watermarks()["partial-after-baseline"] == (1.5, 0.0)


def test_bookkeeping_correction_updates_account_and_global_ledgers_once(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=0.0)
    store.save_portfolio_snapshot(
        "account",
        PortfolioState(cash=100.0, cash_by_currency={"USD": 100.0}, positions={}),
        account_id="toss_brokerage",
    )
    store.save_portfolio_snapshot(
        "global",
        PortfolioState(cash=10.0, cash_by_currency={"USD": 10.0}, positions={}),
    )
    payload = {
        "account_id": "toss_brokerage",
        "currency": "USD",
        "amount": -5.0,
        "reason": "reverse duplicate broker-history principal",
        "evidence": "broker_order_id=duplicate-order",
        "effective_at": "2026-08-03T00:00:00+00:00",
        "duplicate_key": "ledger-bookkeeping-correction:test-duplicate-order",
    }

    assert store.apply_ledger_bookkeeping_correction(
        "correction",
        account_id="toss_brokerage",
        currency="USD",
        amount=-5.0,
        event_payload=payload,
    )
    assert not store.apply_ledger_bookkeeping_correction(
        "correction-retry",
        account_id="toss_brokerage",
        currency="USD",
        amount=-5.0,
        event_payload=payload,
    )

    account_state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert account_state is not None
    assert account_state.cash_by_currency == {"USD": 95.0}
    assert store.load_latest_portfolio_state().cash_by_currency == {"USD": 5.0}
    events = store.list_system_events_by_type("ledger_bookkeeping_correction")
    assert len(events) == 1


def test_legacy_zeroed_baseline_watermark_migrates_from_audited_history(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(str(path), initial_cash=0.0)
    store.save_system_event(
        "legacy-backfill",
        "broker_order_history_item",
        {
            "broker_order_id": "legacy-fractional-sell",
            "filled_quantity": 0.389119,
            "cumulative_notional": 261.4504569284,
            "cumulative_commission": 0.26,
            "cumulative_tax": 0.01,
            "quantity_in_adopted_positions": True,
            "principal_in_cash_baseline": False,
            "cost_in_cash_baseline": False,
        },
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional, "
            "cumulative_commission, cumulative_tax) VALUES (?, ?, ?, ?, ?)",
            ("legacy-fractional-sell", 0.0, 261.4504569284, 0.26, 0.01),
        )

    migrated = StateStore(str(path), initial_cash=0.0)

    assert migrated.load_fill_watermarks()["legacy-fractional-sell"] == (
        0.389119,
        261.4504569284,
    )
    with sqlite3.connect(path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM system_events "
                "WHERE event_type = 'broker_order_history_item'"
            ).fetchone()[0]
        )
    assert payload["quantity_in_adopted_positions"] is True
