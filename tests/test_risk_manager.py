from datetime import UTC, datetime

from maestro.portfolio.manager import PortfolioTarget
from maestro.risk.manager import RiskManager


def test_risk_manager_preserves_target_weights_without_weight_caps():
    manager = RiskManager(allowed_symbols=["CASH", "MOCK_ETF_A"])
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.1, "MOCK_ETF_A": 0.9},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert decision.approved
    assert decision.target.allocations == {"CASH": 0.1, "MOCK_ETF_A": 0.9}
    assert "modifications" not in decision.model_dump()


def test_risk_manager_rejects_unknown_symbol():
    manager = RiskManager(allowed_symbols=["CASH", "MOCK_ETF_A"])
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.5, "MOCK_ETF_X": 0.5},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert not decision.approved
    assert decision.violations == ["MOCK_ETF_X is outside allowed universe"]


def test_risk_manager_rejects_position_above_max_weight():
    manager = RiskManager(allowed_symbols=["CASH", "MOCK_ETF_A"], max_position_weight=0.5)
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.2, "MOCK_ETF_A": 0.8},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert not decision.approved
    assert decision.violations == [
        "MOCK_ETF_A weight 0.8000 exceeds max_position_weight 0.5"
    ]


def test_risk_manager_excludes_cash_from_max_position_weight():
    manager = RiskManager(allowed_symbols=["CASH", "MOCK_ETF_A"], max_position_weight=0.5)
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.8, "MOCK_ETF_A": 0.2},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert decision.approved
    assert decision.violations == []


def test_risk_manager_skips_position_weight_check_when_not_configured():
    manager = RiskManager(allowed_symbols=["CASH", "MOCK_ETF_A"], max_position_weight=None)
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.2, "MOCK_ETF_A": 0.8},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert decision.approved


def test_risk_manager_rejects_sleeve_position_above_max_weight():
    manager = RiskManager(
        allowed_symbols=["CASH", "CORE_A", "CASH_growth", "GROWTH_A"],
        max_position_weight=0.5,
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={
            "core": {"CASH": 0.6, "CORE_A": 0.4},
            "growth": {"CASH_growth": 0.2, "GROWTH_A": 0.8},
        },
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert not decision.approved
    assert decision.violations == [
        "GROWTH_A weight 0.8000 exceeds max_position_weight 0.5"
    ]
