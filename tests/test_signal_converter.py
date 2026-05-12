from datetime import UTC, datetime

import pytest

from maestro.config.models import SignalToAllocationConfig
from maestro.sdk import StrategySignalResult, TargetAllocationResult
from maestro.signals.converter import (
    normalize_strategy_result,
    strategy_signal_to_target_allocation,
)


def _policy(cash_symbol: str = "CASH") -> SignalToAllocationConfig:
    return SignalToAllocationConfig(
        type="single_symbol_action_map",
        cash_symbol=cash_symbol,
        action_target_weights={"buy": 0.3, "hold": 0.0, "sell": 0.0},
    )


def _signal(action: str = "buy", symbol: str = "NVDA") -> StrategySignalResult:
    return StrategySignalResult(
        strategy_id="tradingagents",
        strategy_version="0.2.4",
        timestamp=datetime(2026, 1, 15, tzinfo=UTC),
        symbol=symbol,
        action=action,
        rating="Buy",
        confidence=0.82,
        price_target=195.0,
        time_horizon="3-6 months",
        rationale="Portfolio manager decision.",
        risk_flags=["earnings"],
        metadata={"model_provider": "openai"},
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("buy", {"NVDA": 0.3, "CASH": 0.7}),
        ("hold", {"CASH": 1.0}),
        ("sell", {"CASH": 1.0}),
    ],
)
def test_strategy_signal_to_target_allocation_maps_actions(action, expected):
    result = strategy_signal_to_target_allocation(_signal(action), _policy())

    assert result.allocations == expected
    assert sum(result.allocations.values()) == 1.0


def test_strategy_signal_to_target_allocation_preserves_signal_fields():
    result = strategy_signal_to_target_allocation(_signal(), _policy())

    assert result.confidence == 0.82
    assert result.time_horizon == "3-6 months"
    assert result.rationale == "Portfolio manager decision."
    assert result.risk_flags == ["earnings"]
    assert result.metadata["model_provider"] == "openai"
    assert result.metadata["source_signal"]["symbol"] == "NVDA"
    assert result.metadata["source_signal"]["metadata"] == {"model_provider": "openai"}


def test_strategy_signal_to_target_allocation_uses_cash_symbol_override():
    result = strategy_signal_to_target_allocation(_signal(), _policy("CASH_USD"))

    assert result.allocations == {"NVDA": 0.3, "CASH_USD": 0.7}


def test_normalize_strategy_result_returns_target_allocation_unchanged():
    target = TargetAllocationResult(
        strategy_id="static",
        strategy_version="0.1.0",
        timestamp=datetime(2026, 1, 15, tzinfo=UTC),
        allocations={"CASH": 1.0},
        confidence=1.0,
    )

    assert normalize_strategy_result(target, None) is target


def test_normalize_strategy_result_requires_policy_for_signal():
    with pytest.raises(ValueError, match="requires signal_to_allocation"):
        normalize_strategy_result(_signal(), None)
