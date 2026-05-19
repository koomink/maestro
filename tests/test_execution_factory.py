from pathlib import Path

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.config.models import ExecutionConfig
from maestro.execution.factory import build_execution_engine
from maestro.execution.paper import PaperExecutionEngine
from maestro.orchestration.orchestrator import MaestroOrchestrator


def test_execution_engine_factory_builds_paper_engine():
    engine = build_execution_engine(ExecutionConfig(proposal_engine="paper"))

    assert isinstance(engine, PaperExecutionEngine)


@pytest.mark.parametrize("engine_name", ["live", "kis_live"])
def test_execution_engine_factory_rejects_unsupported_engines(engine_name):
    with pytest.raises(
        ValueError,
        match=f"Unsupported execution proposal engine: {engine_name}",
    ):
        build_execution_engine(ExecutionConfig(proposal_engine=engine_name))


def test_run_once_rejects_unsupported_execution_engine(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["proposal_engine"] = "kis_live"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "unsupported_engine.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match="Maestro v0.1.1 supports only 'paper'"):
        MaestroOrchestrator(load_config(config_path)).run_once()
