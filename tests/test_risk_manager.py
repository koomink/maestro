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
