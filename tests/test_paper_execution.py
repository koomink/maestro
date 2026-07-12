from datetime import UTC, datetime

from maestro.core.enums import Currency, OrderSide, OrderStatus
from maestro.execution.base import OrderIntent
from maestro.execution.paper import PaperExecutionEngine
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


def test_paper_execution_initial_buy_orders_fill_immediately():
    engine = PaperExecutionEngine()
    current = PortfolioState(cash=1000.0, positions={})
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.5, "MOCK_ETF_A": 0.3, "MOCK_ETF_B": 0.2},
        source_strategy_ids=["sample_static_allocation"],
    )

    orders, results, next_state = engine.execute(
        current,
        target,
        prices={"CASH": 1.0, "MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
    )

    assert [order.side for order in orders] == [OrderSide.BUY, OrderSide.BUY]
    assert [result.status for result in results] == [OrderStatus.FILLED, OrderStatus.FILLED]
    assert next_state.cash == 500.0
    assert next_state.positions == {"MOCK_ETF_A": 3.0, "MOCK_ETF_B": 4.0}


def test_paper_execution_generates_no_orders_when_already_at_target():
    engine = PaperExecutionEngine()
    current = PortfolioState(cash=500.0, positions={"MOCK_ETF_A": 3.0, "MOCK_ETF_B": 4.0})
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.5, "MOCK_ETF_A": 0.3, "MOCK_ETF_B": 0.2},
        source_strategy_ids=["sample_static_allocation"],
    )

    orders, results, next_state = engine.execute(
        current,
        target,
        prices={"CASH": 1.0, "MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
    )

    assert orders == []
    assert results == []
    assert next_state == current


def test_paper_execution_cash_mirrors_sum_of_cash_by_currency_across_multiple_currencies():
    engine = PaperExecutionEngine()
    current = PortfolioState(
        cash=0.0,
        cash_by_currency={"KRW": 1_000_000.0, "USD": 10_000.0},
        positions={},
    )
    orders = [
        OrderIntent(
            order_id="ord-krw",
            symbol="SAMSUNG",
            side=OrderSide.BUY,
            quantity=1,
            price=100_000.0,
            notional=100_000.0,
            currency=Currency.KRW,
        ),
        OrderIntent(
            order_id="ord-usd",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=1,
            price=200.0,
            notional=200.0,
            currency=Currency.USD,
        ),
    ]

    _, next_state = engine.execute_orders(current, orders)

    assert next_state.cash_by_currency == {"KRW": 900_000.0, "USD": 9_800.0}
    assert next_state.cash == sum(next_state.cash_by_currency.values())
    assert next_state.cash == 909_800.0
