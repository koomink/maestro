import json
from pathlib import Path

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import StrategySignalResult
from maestro.state.store import StateStore


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


def test_run_once_normalizes_strategy_signal_to_target_allocation(tmp_path, monkeypatch):
    _patch_sample_strategy_signal(monkeypatch, symbol="MOCK_ETF_A", action="buy")
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["strategies"][0]["signal_to_allocation"] = {
        "type": "single_symbol_action_map",
        "action_target_weights": {"buy": 0.3, "hold": 0.0, "sell": 0.0},
    }
    config_path = tmp_path / "signal_strategy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)
    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    strategy_run = store.list_strategy_runs(limit=1)[0]["payload"]

    assert summary.orders_created == 1
    assert strategy_run["result"]["allocations"] == {"MOCK_ETF_A": 0.3, "CASH": 0.7}
    assert strategy_run["source_signal"]["symbol"] == "MOCK_ETF_A"
    assert strategy_run["result"]["metadata"]["source_signal"]["action"] == "buy"


def test_run_once_rejects_signal_allocation_outside_allowed_universe(tmp_path, monkeypatch):
    _patch_sample_strategy_signal(monkeypatch, symbol="NVDA", action="buy")
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["strategies"][0]["signal_to_allocation"] = {
        "type": "single_symbol_action_map",
        "action_target_weights": {"buy": 0.3, "hold": 0.0, "sell": 0.0},
    }
    config_path = tmp_path / "signal_strategy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match="not in allowed universe"):
        MaestroOrchestrator(load_config(config_path)).run_once()


def test_run_once_rejects_signal_allocation_to_research_only_symbol(tmp_path, monkeypatch):
    _patch_sample_strategy_signal(monkeypatch, symbol="MOCK_ETF_A", action="buy")
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["universe"] = {"research_symbols": ["MOCK_ETF_A"]}
    raw["strategies"][0]["signal_to_allocation"] = {
        "type": "single_symbol_action_map",
        "action_target_weights": {"buy": 0.3, "hold": 0.0, "sell": 0.0},
    }
    config_path = tmp_path / "signal_strategy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match="research-only"):
        MaestroOrchestrator(load_config(config_path)).run_once()


def _patch_sample_strategy_signal(
    monkeypatch,
    *,
    symbol: str,
    action: str,
) -> None:
    import sample_static_allocation.strategy as strategy_module

    original_manifest = strategy_module.SampleStaticAllocationStrategy.manifest

    def signal_manifest(self):
        manifest = original_manifest(self)
        return manifest.model_copy(update={"result_type": "strategy_signal"})

    def signal_run(self, data_bundle, context):
        del data_bundle
        return StrategySignalResult(
            strategy_id=context.strategy_id,
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            symbol=symbol,
            action=action,
            rating=action.title(),
            confidence=0.8,
            time_horizon="1-3 months",
            rationale="Synthetic signal for orchestration test.",
            risk_flags=["test"],
            metadata={"source": "test"},
        )

    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "manifest",
        signal_manifest,
    )
    monkeypatch.setattr(strategy_module.SampleStaticAllocationStrategy, "run", signal_run)
