import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import _load_dotenv, app
from maestro.config.loader import load_config
from maestro.state.store import StateStore


def test_init_personal_creates_safe_operator_config(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "real-app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "real-app-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "real-telegram-token")
    output = tmp_path / "operator" / "maestro_personal.yaml"

    result = CliRunner().invoke(app, ["init-personal", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert output.exists()
    text = output.read_text()
    assert "real-app-key" not in text
    assert "real-app-secret" not in text
    assert "real-telegram-token" not in text

    config = load_config(output)
    assert config.mode.value == "live_approval"
    assert config.execution.live_order_enabled is False
    assert config.execution.live_order_dry_run is True
    assert config.execution.allowed_order_type.value == "limit"
    assert config.approval.provider == "telegram"
    assert config.approval.telegram_allowed_chat_ids == []
    assert config.approval.whitelisted_user_ids == []
    assert config.kis.provider == "kis"
    assert config.kis.account_id is None
    assert config.kis.account_id_env == "KIS_ACCOUNT_ID"
    assert str(tmp_path / "operator" / "var") in config.state.sqlite_path


def test_init_personal_refuses_overwrite_without_force(tmp_path):
    output = tmp_path / "maestro_personal.yaml"
    output.write_text("existing")

    result = CliRunner().invoke(app, ["init-personal", "--output", str(output)])

    assert result.exit_code == 2
    assert "output already exists" in result.output
    assert output.read_text() == "existing"


def test_cli_loads_dotenv_without_overriding_shell_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KIS_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "shell-app-key")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "KIS_ACCOUNT_ID=12345678-01",
                "KIS_APP_KEY=dotenv-app-key",
                "TELEGRAM_BOT_TOKEN=dotenv-telegram-token",
            ]
        )
    )

    _load_dotenv()

    assert os.environ["KIS_ACCOUNT_ID"] == "12345678-01"
    assert os.environ["KIS_APP_KEY"] == "shell-app-key"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "dotenv-telegram-token"


def test_personal_check_reports_default_blocked_stages(tmp_path):
    config_path = tmp_path / "maestro_personal.yaml"
    init = CliRunner().invoke(app, ["init-personal", "--output", str(config_path)])
    assert init.exit_code == 0, init.output

    result = CliRunner().invoke(app, ["personal-check", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "personal_check status=blocked" in result.output
    assert "stage=paper_ready status=ok" in result.output
    assert "stage=readonly_ready status=fail" in result.output
    assert "stage=telegram_ready status=fail" in result.output
    assert "stage=dry_run_ready status=fail" in result.output
    assert "stage=minimum_live_ready status=fail" in result.output


def test_personal_check_reports_ready_operator_gates(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_ACCOUNT_ID", "12345678-01")
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = _personal_config(
        tmp_path,
        live_order_enabled=True,
        live_order_dry_run=False,
        require_broker_quote_validation=True,
        require_broker_risk_validation=True,
        daily_loss_limit=100.0,
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event("run_heartbeat", "maestro_heartbeat", {"source": "test"})
    store.save_system_event("run_completed", "run_once_completed", {"orders_created": 0})
    store.save_broker_account_snapshot(
        "run_snapshot",
        "BROKER-ACCOUNT",
        {
            "account": {
                "account_id": "BROKER-ACCOUNT",
                "cash": 1000.0,
                "buying_power": 1000.0,
                "positions": [],
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )
    snapshot = store.load_latest_broker_account_snapshot()
    assert snapshot is not None
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {"passed": True, "issues": [], "broker_snapshot_id": snapshot["id"]},
    )

    result = CliRunner().invoke(app, ["personal-check", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "stage=readonly_ready status=ok" in result.output
    assert "stage=telegram_ready status=ok" in result.output
    assert "stage=minimum_live_ready status=ok" in result.output
    assert "app-key" not in result.output
    assert "app-secret" not in result.output
    assert "telegram-token" not in result.output


def _personal_config(
    tmp_path: Path,
    *,
    live_order_enabled: bool = False,
    live_order_dry_run: bool = True,
    require_broker_quote_validation: bool = False,
    require_broker_risk_validation: bool = False,
    daily_loss_limit: float | None = None,
) -> Path:
    output = tmp_path / "maestro_personal.yaml"
    result = CliRunner().invoke(app, ["init-personal", "--output", str(output)])
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load(output.read_text())
    raw["approval"]["telegram_allowed_chat_ids"] = [123456789]
    raw["approval"]["whitelisted_user_ids"] = [123456789]
    raw["execution"]["live_order_enabled"] = live_order_enabled
    raw["execution"]["live_order_dry_run"] = live_order_dry_run
    raw["execution"]["require_broker_quote_validation"] = require_broker_quote_validation
    raw["execution"]["require_broker_risk_validation"] = require_broker_risk_validation
    raw["execution"]["daily_loss_limit"] = daily_loss_limit
    output.write_text(yaml.safe_dump(raw))
    return output
