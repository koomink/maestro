from datetime import UTC, datetime

import pytest

from maestro.execution.execution_sleeves import (
    ExecutionScopeDraft,
    allocate_cash_rebalanced_scope_states,
    validate_unique_execution_scope_symbols,
)
from maestro.execution.order_builder import OrderBuilder
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
            execution_sleeve="crescendo_us",
            currency_sleeve="USD",
            target_weight=0.4,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"AAPL": 1.0},
                source_strategy_ids=["crescendo_us"],
            ),
        ),
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="fugue_us",
            currency_sleeve="USD",
            target_weight=0.6,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"MSFT": 1.0},
                source_strategy_ids=["fugue"],
            ),
        ),
    ]

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=scopes,
        prices={"AAPL": 100, "MSFT": 100},
    )

    by_sleeve = {scope.execution_sleeve: scope for scope in allocated}
    assert by_sleeve["crescendo_us"].state.cash_by_currency == {"USD": 1_000}
    assert by_sleeve["crescendo_us"].state.positions == {"AAPL": 30}
    assert by_sleeve["fugue_us"].state.cash_by_currency == {"USD": 0}
    assert by_sleeve["fugue_us"].state.positions == {"MSFT": 70}
    assert by_sleeve["crescendo_us"].current_weight == pytest.approx(3_000 / 11_000)
    assert by_sleeve["crescendo_us"].drift == pytest.approx((3_000 / 11_000) - 0.4)


def test_cash_rebalance_uses_target_weights_when_no_sleeve_is_underweight():
    state = PortfolioState(
        cash=1_000,
        cash_by_currency={"USD": 1_000},
        positions={"AAPL": 40, "MSFT": 60},
    )
    scopes = [
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="crescendo_us",
            currency_sleeve="USD",
            target_weight=0.4,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"AAPL": 1.0},
                source_strategy_ids=["crescendo_us"],
            ),
        ),
        ExecutionScopeDraft(
            account_id="kis_brokerage",
            execution_sleeve="fugue_us",
            currency_sleeve="USD",
            target_weight=0.6,
            target=PortfolioTarget(
                timestamp=datetime(2026, 5, 28, tzinfo=UTC),
                allocations={"MSFT": 1.0},
                source_strategy_ids=["fugue"],
            ),
        ),
    ]

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=scopes,
        prices={"AAPL": 100, "MSFT": 100},
    )

    by_sleeve = {scope.execution_sleeve: scope for scope in allocated}
    assert by_sleeve["crescendo_us"].state.cash_by_currency == {"USD": 400}
    assert by_sleeve["fugue_us"].state.cash_by_currency == {"USD": 600}


def test_cash_rebalance_uses_attributed_positions_for_scope_value():
    state = PortfolioState(
        cash=100,
        cash_by_currency={"USD": 100},
        positions={"QQQ": 10},
    )
    target = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"QQQ": 1.0},
        source_strategy_ids=["crescendo_us"],
    )

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=[
            ExecutionScopeDraft(
                account_id="toss_brokerage",
                execution_sleeve="crescendo_us",
                currency_sleeve="USD",
                target_weight=0.7,
                target=target,
                attributed_positions={"QQQ": 4},
            )
        ],
        prices={"QQQ": 10},
    )

    assert allocated[0].current_value == 40
    assert allocated[0].state.positions == {"QQQ": 4}


@pytest.mark.parametrize(
    ("manual_quantity", "expected_allocated_cash"),
    [
        (2.0, 40.0),
        (3.0, 40.0),
        (4.0, 30.0),
    ],
)
def test_account_target_weight_reserves_manual_capacity(
    manual_quantity,
    expected_allocated_cash,
):
    state = PortfolioState(
        cash=70.0 - manual_quantity * 10.0,
        positions={"QQQ": 3.0 + manual_quantity},
    )
    target = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"QQQ": 1.0},
        source_strategy_ids=["crescendo_us"],
    )

    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=[
            ExecutionScopeDraft(
                account_id="toss_brokerage",
                execution_sleeve="crescendo_us",
                currency_sleeve=None,
                target_weight=0.7,
                target=target,
                attributed_positions={"QQQ": 3.0},
            )
        ],
        prices={"QQQ": 10.0},
    )

    assert allocated[0].allocated_cash == pytest.approx(expected_allocated_cash)
    assert allocated[0].current_weight == pytest.approx(0.3)
    assert allocated[0].state.positions == {"QQQ": 3.0}


def test_manual_overweight_capacity_flows_into_generated_strategy_buy_orders():
    state = PortfolioState(
        cash=30.0,
        positions={"QQQ": 7.0},
    )
    target = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"QQQ": 1.0},
        source_strategy_ids=["crescendo_us"],
    )
    allocated = allocate_cash_rebalanced_scope_states(
        current_state=state,
        scopes=[
            ExecutionScopeDraft(
                account_id="toss_brokerage",
                execution_sleeve="crescendo_us",
                currency_sleeve=None,
                target_weight=0.7,
                target=target,
                attributed_positions={"QQQ": 3.0},
            )
        ],
        prices={"QQQ": 10.0},
    )[0]

    orders = OrderBuilder().build_orders(allocated.state, allocated.target, {"QQQ": 10.0})

    assert allocated.allocated_cash == pytest.approx(30.0)
    assert allocated.state.positions == {"QQQ": 3.0}
    assert [(order.side.value, order.symbol, order.notional) for order in orders] == [
        ("buy", "QQQ", 30.0)
    ]


def test_execution_sleeves_reject_shared_target_symbol_within_account():
    target_a = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"AAPL": 1.0},
        source_strategy_ids=["crescendo_us"],
    )
    target_b = PortfolioTarget(
        timestamp=datetime(2026, 5, 28, tzinfo=UTC),
        allocations={"AAPL": 1.0},
        source_strategy_ids=["fugue"],
    )

    with pytest.raises(ValueError, match="shared target symbols"):
        validate_unique_execution_scope_symbols(
            [
                ExecutionScopeDraft(
                    account_id="kis_brokerage",
                    execution_sleeve="crescendo_us",
                    currency_sleeve="USD",
                    target_weight=0.4,
                    target=target_a,
                ),
                ExecutionScopeDraft(
                    account_id="kis_brokerage",
                    execution_sleeve="fugue_us",
                    currency_sleeve="USD",
                    target_weight=0.6,
                    target=target_b,
                ),
            ]
        )
