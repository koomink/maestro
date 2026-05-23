import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataRequest, StrategySignalResult
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
    assert details["strategy_book_snapshots"][0]["book_id"] == "sample_static_allocation"
    assert len(details["paper_orders"]) == 2


def test_run_once_cli_sends_telegram_success_notification(tmp_path, monkeypatch):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["provider"] = "telegram"
    raw["approval"]["telegram_allowed_chat_ids"] = [100, 200]
    config_path = tmp_path / "telegram_success.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> "FakeTelegramClient":
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    result = CliRunner().invoke(app, ["run-once", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "telegram_notification=sent chats=2" in result.output
    assert [message["chat_id"] for message in fake_clients[0].sent_messages] == [100, 200]
    text = fake_clients[0].sent_messages[0]["text"]
    assert "Maestro run-once completed" in text
    assert "mode: paper" in text
    assert "strategies: sample_static_allocation" in text
    assert "orders: 2" in text
    assert "total_value: 10,000,000.00 KRW" in text
    assert "cash: 5,000,000.00 KRW" in text


def test_run_once_cli_sends_telegram_failure_notification(tmp_path, monkeypatch):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["provider"] = "telegram"
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["strategies"][0]["config"]["allocations"] = {
        "CASH": 0.6,
        "MOCK_ETF_A": 0.6,
    }
    config_path = tmp_path / "telegram_failure.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> "FakeTelegramClient":
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    result = CliRunner().invoke(app, ["run-once", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "telegram_notification=sent chats=1" in result.output
    text = fake_clients[0].sent_messages[0]["text"]
    assert "Maestro run-once failed" in text
    assert "mode: paper" in text
    assert "error_type: ValueError" in text
    assert "Invalid strategy result" in text


def test_run_once_cli_telegram_notification_warning_does_not_fail_run(tmp_path, monkeypatch):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["provider"] = "telegram"
    raw["approval"]["telegram_bot_token_env"] = "MISSING_TEST_TELEGRAM_TOKEN"
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    config_path = tmp_path / "telegram_missing_token.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    monkeypatch.delenv("MISSING_TEST_TELEGRAM_TOKEN", raising=False)

    result = CliRunner().invoke(app, ["run-once", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "run_id=" in result.output
    assert "telegram_notification=warn message=missing_bot_token" in result.output


def test_run_once_records_runtime_data_requests(tmp_path, monkeypatch):
    import sample_static_allocation.strategy as strategy_module

    def run_with_runtime(self, data_bundle, context, runtime):
        runtime.get_data(
            DataRequest(
                symbol="MOCK_ETF_A",
                asset_type="domestic_etf",
                data_type="price",
                intended_use="tradable",
            )
        )
        return self.run(data_bundle, context)

    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "run_with_runtime",
        run_with_runtime,
    )
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    MaestroOrchestrator(load_config(config_path)).run_once()

    audit_event = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    runtime = audit_event["details"]["data_requests"]["sample_static_allocation"]["runtime"]

    assert runtime["requests"][0]["symbol"] == "MOCK_ETF_A"
    assert runtime["requests"][0]["data_type"] == "price"
    assert runtime["bundles"][0]["source"] == "mock"
    assert runtime["errors"] == []


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


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages = []

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"ok": True}


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
