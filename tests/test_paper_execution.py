from datetime import UTC, datetime

from maestro.core.enums import OrderSide, OrderStatus
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
