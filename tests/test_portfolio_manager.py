import pytest

from maestro.config.models import StrategyPluginConfig
from maestro.core.clock import utc_now
from maestro.portfolio.manager import PortfolioManager
from maestro.sdk import TargetAllocationResult


def test_portfolio_manager_rejects_mixed_sleeve_and_plain_results():
    manager = _manager()

    with pytest.raises(
        ValueError, match="sleeves=\\['sleeve_strategy'\\].*plain=\\['plain_strategy'\\]"
    ):
        manager.build_target(
            [
                _sleeve_result("sleeve_strategy"),
                _plain_result("plain_strategy"),
            ]
        )


def test_portfolio_manager_allows_sleeve_only_results():
    target = _manager().build_target([_sleeve_result("sleeve_strategy")])

    assert target.allocations == {}
    assert target.allocation_sleeves == {"KRW": {"MOCK_ETF_A": 1.0}}
    assert target.source_strategy_ids == ["sleeve_strategy"]


def test_portfolio_manager_allows_plain_only_results():
    target = _manager().build_target([_plain_result("plain_strategy")])

    assert target.allocations == {"MOCK_ETF_B": 1.0}
    assert target.allocation_sleeves == {}
    assert target.source_strategy_ids == ["plain_strategy"]


def test_portfolio_manager_allows_empty_result_with_sleeve_result():
    target = _manager().build_target(
        [
            _sleeve_result("sleeve_strategy"),
            _empty_result("empty_strategy"),
        ]
    )

    assert target.allocation_sleeves == {"KRW": {"MOCK_ETF_A": 1.0}}
    assert target.source_strategy_ids == ["sleeve_strategy", "empty_strategy"]


def _manager() -> PortfolioManager:
    return PortfolioManager(
        [
            _strategy_config("sleeve_strategy"),
            _strategy_config("plain_strategy"),
            _strategy_config("empty_strategy"),
        ]
    )


def _strategy_config(strategy_id: str) -> StrategyPluginConfig:
    return StrategyPluginConfig(
        id=strategy_id,
        entrypoint="tests.fake:Strategy",
        weight=1.0,
    )


def _sleeve_result(strategy_id: str) -> TargetAllocationResult:
    return TargetAllocationResult(
        strategy_id=strategy_id,
        strategy_version="1.0",
        timestamp=utc_now(),
        allocations={},
        allocation_sleeves={"KRW": {"MOCK_ETF_A": 1.0}},
        confidence=1.0,
        time_horizon="static",
        rationale="sleeve",
    )


def _plain_result(strategy_id: str) -> TargetAllocationResult:
    return TargetAllocationResult(
        strategy_id=strategy_id,
        strategy_version="1.0",
        timestamp=utc_now(),
        allocations={"MOCK_ETF_B": 1.0},
        confidence=1.0,
        time_horizon="static",
        rationale="plain",
    )


def _empty_result(strategy_id: str) -> TargetAllocationResult:
    return TargetAllocationResult(
        strategy_id=strategy_id,
        strategy_version="1.0",
        timestamp=utc_now(),
        allocations={},
        confidence=1.0,
        time_horizon="static",
        rationale="empty",
    )
