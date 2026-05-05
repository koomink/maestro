from datetime import UTC, datetime

import pytest

from maestro.core.exceptions import MissingPriceError
from maestro.execution.order_builder import OrderBuilder
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


def test_portfolio_state_total_value_raises_for_missing_position_price():
    state = PortfolioState(cash=100.0, positions={"MOCK_ETF_A": 2.0})

    with pytest.raises(MissingPriceError, match="MOCK_ETF_A"):
        state.total_value({})


def test_order_builder_raises_for_missing_target_symbol_price():
    state = PortfolioState(cash=1000.0, positions={})
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.5, "MOCK_ETF_A": 0.5},
        source_strategy_ids=["sample_static_allocation"],
    )

    with pytest.raises(MissingPriceError, match="MOCK_ETF_A"):
        OrderBuilder().build_orders(state, target, prices={"CASH": 1.0})
