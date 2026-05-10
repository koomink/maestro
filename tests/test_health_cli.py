import logging
from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.ids import new_run_id
from maestro.monitoring.health import HealthService
from maestro.monitoring.logging import JsonFormatter
from maestro.state.store import StateStore


def test_health_cli_reports_local_checks_without_kis_network(monkeypatch, tmp_path):
    monkeypatch.delenv("KIS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCESS_TOKEN", raising=False)
    config_path = _readonly_config(tmp_path)

    result = CliRunner().invoke(app, ["health", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "status=warn" in result.output
    assert "check=config status=ok" in result.output
    assert "check=state_db status=ok" in result.output
    assert "check=kis_env status=warn message=missing_required_env" in result.output
    assert "check=broker_snapshot status=warn message=missing" in result.output
    assert "check=reconciliation status=warn message=missing" in result.output
    assert "app-key" not in result.output
    assert "app-secret" not in result.output
    assert "access-token" not in result.output


def test_health_reports_recent_broker_snapshot_and_reconciliation(tmp_path):
    config_path = _readonly_config(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    run_id = new_run_id()
    store.save_broker_account_snapshot(
        run_id,
        "BROKER-ACCOUNT",
        {"account": {"account_id": "BROKER-ACCOUNT", "cash": 100.0, "positions": []}},
    )
    store.save_system_event(run_id, "broker_reconciliation", {"passed": True, "issues": []})

    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert checks["broker_snapshot"].status == "ok"
    assert checks["reconciliation"].status == "ok"


def test_health_reports_failed_reconciliation(tmp_path):
    config_path = _readonly_config(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(new_run_id(), "broker_reconciliation", {"passed": False})

    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert report.status == "fail"
    assert checks["reconciliation"].status == "fail"


def test_structured_logging_redacts_secret_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="maestro.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.payload = {
        "app_key": "app-key",
        "app_secret": "app-secret",
        "access_token": "access-token",
        "symbol": "AAPL",
    }

    output = formatter.format(record)

    assert "app-key" not in output
    assert "app-secret" not in output
    assert "access-token" not in output
    assert "[REDACTED]" in output
    assert "AAPL" in output


def _readonly_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/kis_overseas_readonly.example.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "kis_access_token.json")
    config_path = tmp_path / "kis_overseas_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path
