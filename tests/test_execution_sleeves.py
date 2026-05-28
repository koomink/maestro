from datetime import UTC, datetime

import pytest

from maestro.execution.execution_sleeves import (
    ExecutionScopeDraft,
    allocate_cash_rebalanced_scope_states,
    validate_unique_execution_scope_symbols,
)
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


def test_cash_rebalance_allocates_cash_to_underweight_execution_sleeve():
    state = PortfolioState(
        cash=1_000,
        cash_by_currency={"USD": 1_000},
        positions={"AAPL": 30, "MSFT": 70},
    )
    scopes = [
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="snowball_us",
            currency_sleeve="USD",
            target_weight=0.4,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"AAPL": 1.0},
                source_strategy_ids=["snowball_us"],
            ),
        ),
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="trading_agents_us",
            currency_sleeve="USD",
            target_weight=0.6,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"MSFT": 1.0},
                source_strategy_ids=["trading_agents"],
            ),
        ),
    ]

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=scopes,
        prices={"AAPL": 100, "MSFT": 100},
    )

    by_sleeve = {scope.execution_sleeve: scope for scope in allocated}
    assert by_sleeve["snowball_us"].state.cash_by_currency == {"USD": 1_000}
    assert by_sleeve["snowball_us"].state.positions == {"AAPL": 30}
    assert by_sleeve["trading_agents_us"].state.cash_by_currency == {"USD": 0}
    assert by_sleeve["trading_agents_us"].state.positions == {"MSFT": 70}
    assert by_sleeve["snowball_us"].current_weight == pytest.approx(3_000 / 11_000)
    assert by_sleeve["snowball_us"].drift == pytest.approx((3_000 / 11_000) - 0.4)


def test_cash_rebalance_uses_target_weights_when_no_sleeve_is_underweight():
    state = PortfolioState(
        cash=1_000,
        cash_by_currency={"USD": 1_000},
        positions={"AAPL": 40, "MSFT": 60},
    )
    scopes = [
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="snowball_us",
            currency_sleeve="USD",
            target_weight=0.4,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"AAPL": 1.0},
                source_strategy_ids=["snowball_us"],
            ),
        ),
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="trading_agents_us",
            currency_sleeve="USD",
            target_weight=0.6,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"MSFT": 1.0},
                source_strategy_ids=["trading_agents"],
            ),
        ),
    ]

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=scopes,
        prices={"AAPL": 100, "MSFT": 100},
    )

    by_sleeve = {scope.execution_sleeve: scope for scope in allocated}
    assert by_sleeve["snowball_us"].state.cash_by_currency == {"USD": 400}
    assert by_sleeve["trading_agents_us"].state.cash_by_currency == {"USD": 600}


def test_execution_sleeves_reject_shared_target_symbol_within_account():
    target_a = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"AAPL": 1.0},
        source_strategy_ids=["snowball_us"],
    )
    target_b = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"AAPL": 1.0},
        source_strategy_ids=["trading_agents"],
    )

    with pytest.raises(ValueError, match="shared target symbols"):
        validate_unique_execution_scope_symbols(
            [
                ExecutionScopeDraft(
                    account_id="kis_brokerage",
                    execution_sleeve="snowball_us",
                    currency_sleeve="USD",
                    target_weight=0.4,
                    target=target_a,
                ),
                ExecutionScopeDraft(
                    account_id="kis_brokerage",
                    execution_sleeve="trading_agents_us",
                    currency_sleeve="USD",
                    target_weight=0.6,
                    target=target_b,
                ),
            ]
        )
