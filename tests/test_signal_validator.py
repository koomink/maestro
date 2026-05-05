from datetime import UTC, datetime

from maestro.sdk import TargetAllocationResult
from maestro.signals.validator import SignalValidator


def test_signal_validator_rejects_unknown_strategy_negative_and_unknown_symbol():
    validator = SignalValidator(
        allowed_symbols=["CASH", "MOCK_ETF_A"],
        strategy_ids={"sample_static_allocation"},
    )
    result = TargetAllocationResult(
        strategy_id="unknown",
        strategy_version="0.1.0",
        timestamp=datetime.now(UTC),
        allocations={"MOCK_ETF_A": -0.1, "MOCK_ETF_X": 0.2},
        confidence=0.5,
    )

    validation = validator.validate(result)

    assert not validation.ok
    assert "Unknown strategy_id: unknown" in validation.errors
    assert "allocation for MOCK_ETF_A must be non-negative" in validation.errors
    assert "allocation symbol MOCK_ETF_X is not in allowed universe" in validation.errors


def test_signal_validator_rejects_allocation_sum_above_one():
    validator = SignalValidator(
        allowed_symbols=["CASH", "MOCK_ETF_A"],
        strategy_ids={"sample_static_allocation"},
    )
    result = TargetAllocationResult(
        strategy_id="sample_static_allocation",
        strategy_version="0.1.0",
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.5, "MOCK_ETF_A": 0.6},
        confidence=0.5,
    )

    validation = validator.validate(result)

    assert not validation.ok
    assert "allocation sum must be 1.0 or less" in validation.errors
