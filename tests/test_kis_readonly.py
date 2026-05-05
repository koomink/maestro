from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_kis_readonly_service_stores_snapshot(tmp_path):
    config = _live_readonly_config(tmp_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)

    snapshot = KISReadOnlyService(config.kis, store, audit).fetch_and_store_snapshot(
        config.portfolio.allowed_symbols
    )

    latest = store.load_latest_broker_account_snapshot()
    assert snapshot.account.account_id == "MOCK-ACCOUNT"
    assert snapshot.account.total_value == 10_000_000
    assert latest is not None
    assert latest["payload"]["account"]["account_id"] == "MOCK-ACCOUNT"
    assert store.status()["counts"]["broker_account_snapshots"] == 1


def test_kis_cli_sync_and_account(tmp_path):
    config = _live_readonly_config(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    account_result = runner.invoke(app, ["kis-account", "--config", str(config_path)])

    assert sync_result.exit_code == 0
    assert "account_id=MOCK-ACCOUNT" in sync_result.output
    assert account_result.exit_code == 0
    assert "positions=2" in account_result.output


def _live_readonly_config(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    config_path = tmp_path / "source_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)
