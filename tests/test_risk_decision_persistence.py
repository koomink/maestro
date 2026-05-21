from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.portfolio.manager import PortfolioTarget
from maestro.risk.manager import RiskDecision
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_risk_decision_is_saved_on_approved_run(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)

    MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    decisions = store.list_risk_decisions()
    assert len(decisions) == 1
    assert decisions[0]["payload"]["approved"] is True
    assert store.status()["counts"]["risk_decisions"] == 1


def test_risk_decision_is_saved_when_risk_check_rejects(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    orchestrator = MaestroOrchestrator(config)
    orchestrator.risk_manager = RejectingRiskManager()

    with pytest.raises(ValueError, match="Risk check failed"):
        orchestrator.run_once()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    decisions = store.list_risk_decisions()
    assert len(decisions) == 1
    assert decisions[0]["payload"]["approved"] is False
    assert decisions[0]["payload"]["violations"] == ["forced rejection"]


def test_state_store_existing_methods_still_work_with_wal_settings(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    state = PortfolioState(cash=1000, positions={})

    store.save_portfolio_snapshot("run_test", state)

    assert store.load_latest_portfolio_state() == state
    assert len(store.list_portfolio_snapshots()) == 1


class RejectingRiskManager:
    def check(self, target: PortfolioTarget) -> RiskDecision:
        return RiskDecision(
            approved=False,
            target=PortfolioTarget(
                timestamp=datetime.now(UTC),
                allocations=target.allocations,
                source_strategy_ids=target.source_strategy_ids,
            ),
            violations=["forced rejection"],
        )
