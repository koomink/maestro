from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


def test_approval_required_approved_executes_orders(tmp_path):
    config = _config_with_approval(tmp_path, "approved")

    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    approvals = store.list_approvals()

    assert summary.orders_created == 2
    assert summary.cash == 5000000
    assert store.status()["counts"]["approvals"] == 1
    assert approvals[0]["payload"]["decision"]["status"] == "approved"
    assert "🔔 Maestro Approval" in approvals[0]["payload"]["message"]


def test_approval_required_rejected_skips_execution(tmp_path):
    config = _config_with_approval(tmp_path, "rejected")

    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    current = store.load_latest_portfolio_state()
    orders = store.list_orders()

    assert summary.orders_created == 2
    assert current.cash == 10000000
    assert current.positions == {}
    assert orders[0]["payload"]["approval_status"] == "rejected"


def _config_with_approval(tmp_path, decision):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["default_decision"] = decision
    config_path = tmp_path / "approval_paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)
