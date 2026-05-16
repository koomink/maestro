from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


def test_run_once_pipeline_with_csv_datahub(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/paper_csv.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "csv_paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)
    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    assert summary.orders_created == 2
    assert store.status()["counts"]["orders"] == 2
    assert len(store.list_strategy_runs()) == 1
    assert len(store.list_orders()) == 2
