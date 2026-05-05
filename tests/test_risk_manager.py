from datetime import UTC, datetime

from maestro.config.models import RiskConfig
from maestro.portfolio.manager import PortfolioTarget
from maestro.risk.manager import RiskManager


def test_risk_manager_caps_max_weight_and_moves_excess_to_cash():
    manager = RiskManager(
        allowed_symbols=["CASH", "MOCK_ETF_A"],
        config=RiskConfig(max_single_asset_weight=0.4, min_cash_weight=0.05),
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.1, "MOCK_ETF_A": 0.9},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert decision.approved
    assert decision.target.allocations["MOCK_ETF_A"] == 0.4
    assert decision.target.allocations["CASH"] == 0.6
    assert decision.modifications


def test_risk_manager_enforces_min_cash():
    manager = RiskManager(
        allowed_symbols=["CASH", "MOCK_ETF_A", "MOCK_ETF_B"],
        config=RiskConfig(max_single_asset_weight=0.8, min_cash_weight=0.2),
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={"CASH": 0.0, "MOCK_ETF_A": 0.5, "MOCK_ETF_B": 0.5},
        source_strategy_ids=["s"],
    )

    decision = manager.check(target)

    assert decision.approved
    assert decision.target.allocations["CASH"] == 0.2
    assert round(decision.target.allocations["MOCK_ETF_A"], 6) == 0.4
    assert round(decision.target.allocations["MOCK_ETF_B"], 6) == 0.4
    assert decision.modifications == ["Raised CASH to 0.200000"]
