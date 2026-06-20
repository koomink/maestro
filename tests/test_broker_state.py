import pytest

from maestro.config.portfolio import PortfolioConfig
from maestro.execution.broker_state import portfolio_state_from_broker_account


def test_portfolio_config_defaults_unknown_broker_position_policy_to_fail_closed():
    config = PortfolioConfig()

    assert config.unknown_broker_position_policy == "fail_closed"


def test_portfolio_config_accepts_include_readonly_unknown_broker_position_policy():
    config = PortfolioConfig(unknown_broker_position_policy="include_readonly")

    assert config.unknown_broker_position_policy == "include_readonly"


def test_portfolio_state_rejects_unknown_broker_positions_by_default():
    account = {
        "cash": 1000.0,
        "cash_by_currency": {"KRW": 1000.0},
        "positions": [{"symbol": "005930", "quantity": 6}],
    }

    with pytest.raises(ValueError, match="positions outside"):
        portfolio_state_from_broker_account(
            account,
            allowed_symbols=["CASH_KRW"],
        )


def test_portfolio_state_can_include_unknown_broker_positions_for_readonly_adoption():
    account = {
        "cash": 1000.0,
        "cash_by_currency": {"KRW": 1000.0},
        "positions": [{"symbol": "005930", "quantity": 6}],
    }

    state = portfolio_state_from_broker_account(
        account,
        allowed_symbols=["CASH_KRW"],
        unknown_symbol_policy="include_readonly",
    )

    assert state.cash_by_currency == {"KRW": 1000.0}
    assert state.positions == {"005930": 6.0}
