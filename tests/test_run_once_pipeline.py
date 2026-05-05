from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator


def test_run_once_pipeline(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    summary = MaestroOrchestrator(load_config(config_path)).run_once()

    assert summary.loaded_strategies == ["sample_static_allocation"]
    assert summary.orders_created == 2
    assert summary.total_value == 10000000
    assert (tmp_path / "state.db").exists()
    assert (tmp_path / "audit.jsonl").read_text().strip()
