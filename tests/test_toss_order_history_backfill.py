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


def _order(order_id: str, *, commission: float = 1.0) -> BrokerOrderSummary:
    return BrokerOrderSummary(
        order_id=order_id,
        symbol="QQQ",
        side="buy",
        quantity=2.0,
        status="FILLED",
        submitted_at=datetime(2026, 7, 1, tzinfo=UTC),
        filled_quantity=2.0,
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
    assert store.load_fill_watermarks()["pre-baseline"] == (0.0, 0.0)
