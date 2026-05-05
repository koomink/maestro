import json
from pathlib import Path

import pytest
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
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert audit_lines
    audit_event = json.loads(audit_lines[-1])
    assert audit_event["event_type"] == "run_once_completed"
    details = audit_event["details"]
    assert details["loaded_strategies"] == ["sample_static_allocation"]
    assert details["portfolio_target"]["allocations"] == {
        "CASH": 0.5,
        "MOCK_ETF_A": 0.3,
        "MOCK_ETF_B": 0.2,
    }
    assert len(details["paper_orders"]) == 2


def test_run_once_failure_audit_includes_exception_metadata(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["strategies"][0]["config"]["allocations"] = {
        "CASH": 0.6,
        "MOCK_ETF_A": 0.6,
    }
    config_path = tmp_path / "invalid_strategy_result.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError):
        MaestroOrchestrator(load_config(config_path)).run_once()

    audit_event = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert audit_event["event_type"] == "run_once_failed"
    details = audit_event["details"]
    assert details["error_type"] == "ValueError"
    assert "Invalid strategy result" in details["error_message"]
    assert "traceback" in details
