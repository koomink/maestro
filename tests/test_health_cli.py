import json
import logging
from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config, load_config_with_identity
from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.monitoring.logging import JsonFormatter
from maestro.state.store import StateStore


def test_health_cli_reports_local_checks_without_kis_network(monkeypatch, tmp_path):
    monkeypatch.delenv("KIS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCESS_TOKEN", raising=False)
    config_path = _readonly_config(tmp_path)
    monkeypatch.chdir(tmp_path)

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


def test_cli_uses_maestro_config_env_when_config_option_is_omitted(monkeypatch, tmp_path):
    config_path = _live_approval_config(tmp_path)
    monkeypatch.setenv("MAESTRO_CONFIG", str(config_path))

    result = CliRunner().invoke(app, ["heartbeat"])

    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert result.exit_code == 0
    assert "heartbeat run_id=" in result.output
    assert store.list_system_events_by_type("maestro_heartbeat")


def test_cli_requires_config_option_or_maestro_config_env(monkeypatch):
    monkeypatch.delenv("MAESTRO_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["health"], env={"MAESTRO_CONFIG": ""})

    assert result.exit_code != 0
    assert "MAESTRO_CONFIG" in result.output


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


def test_health_reports_missing_heartbeat_and_scheduled_run_when_configured(tmp_path):
    config_path = _live_approval_config(
        tmp_path,
        heartbeat_max_age_seconds=60,
        scheduled_run_max_age_seconds=60,
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert checks["heartbeat"].status == "fail"
    assert checks["heartbeat"].message == "missing"
    assert checks["scheduled_run"].status == "fail"
    assert checks["scheduled_run"].message == "missing"


def test_heartbeat_cli_records_event_and_hash_chained_audit(tmp_path):
    config_path = _live_approval_config(tmp_path)

    result = CliRunner().invoke(app, ["heartbeat", "--config", str(config_path)])

    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit_line = Path(config.audit.jsonl_path).read_text().splitlines()[0]
    audit_event = json.loads(audit_line)
    assert result.exit_code == 0
    assert store.list_system_events_by_type("maestro_heartbeat")
    heartbeat = store.list_system_events_by_type("maestro_heartbeat")[0]["payload"]
    operator_config = store.status()["operator_config"]
    assert operator_config is not None
    assert heartbeat["config"] == operator_config
    assert heartbeat["state_path"] == str(Path(config.state.sqlite_path).resolve())
    assert heartbeat["audit_path"] == str(Path(config.audit.jsonl_path).resolve())
    assert audit_event["event_type"] == "maestro_heartbeat"
    assert audit_event["details"]["config"] == operator_config
    assert audit_event["event_hash"]


def test_state_store_rejects_same_db_with_different_config_identity(tmp_path):
    config_path = _live_approval_config(tmp_path)
    config, identity = load_config_with_identity(config_path)
    StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )

    raw = yaml.safe_load(config_path.read_text())
    raw["approval"]["timeout_seconds"] = 123
    second_config_path = tmp_path / "live_approval_changed.yaml"
    second_config_path.write_text(yaml.safe_dump(raw))
    changed_config, changed_identity = load_config_with_identity(second_config_path)

    try:
        StateStore(
            changed_config.state.sqlite_path,
            changed_config.portfolio.initial_cash,
            changed_config.portfolio.cash_by_currency,
            config_identity=changed_identity,
        )
    except ValueError as exc:
        assert "config identity mismatch" in str(exc)
    else:
        raise AssertionError("expected config identity mismatch")


def test_audit_integrity_check_detects_hash_tampering(tmp_path):
    config_path = _live_approval_config(tmp_path)
    config = load_config(config_path)
    audit = AuditLogger(config.audit.jsonl_path)
    audit.log("run_1", "event_1", {"value": 1})
    audit.log("run_2", "event_2", {"value": 2})
    path = Path(config.audit.jsonl_path)
    lines = path.read_text().splitlines()
    second = json.loads(lines[1])
    second["details"]["value"] = 3
    lines[1] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n")

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert checks["audit_integrity"].status == "fail"
    assert checks["audit_integrity"].message == "hash_mismatch"


def test_ops_alerts_cli_reports_health_alerts_without_network(tmp_path):
    config_path = _live_approval_config(tmp_path, heartbeat_max_age_seconds=60)

    result = CliRunner().invoke(
        app,
        ["ops-alerts", "--config", str(config_path), "--allow-mock"],
    )

    assert result.exit_code == 0
    assert "ops_alerts status=ok mock=true" in result.output


def test_health_live_approval_preflight_reports_ready_when_config_is_safe(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config_path = _live_approval_config(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert checks["live_approval_preflight"].status == "ok"
    assert checks["live_approval_preflight"].message == "ready"


def test_live_preflight_cli_exits_zero_when_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config_path = _live_approval_config(tmp_path)

    result = CliRunner().invoke(app, ["live-preflight", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "check=live_approval_preflight status=ok message=ready" in result.output


def test_live_preflight_cli_exits_one_when_failed(tmp_path):
    config_path = _live_approval_config(tmp_path, require_reconciliation_pass=False)

    result = CliRunner().invoke(app, ["live-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "status=fail" in result.output
    assert "reconciliation_not_required" in result.output


def test_beta_preflight_cli_exits_zero_when_private_beta_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config_path = _live_approval_config(
        tmp_path,
        require_market_session=True,
        require_broker_quote_validation=True,
        require_broker_risk_validation=True,
        daily_loss_limit=100.0,
        heartbeat_max_age_seconds=60,
        scheduled_run_max_age_seconds=60,
        datahub_provider="yahoo",
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_beta_ready_events(store)

    result = CliRunner().invoke(app, ["beta-preflight", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "check=private_beta_preflight status=ok message=ready" in result.output


def test_beta_preflight_cli_exits_one_when_hardening_missing(tmp_path):
    config_path = _live_approval_config(tmp_path)

    result = CliRunner().invoke(app, ["beta-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "status=fail" in result.output
    assert "market_session_not_required" in result.output
    assert "broker_risk_validation_not_required" in result.output


def test_beta_preflight_cli_fails_when_kis_paper_trading_is_enabled(tmp_path):
    config_path = _live_approval_config(
        tmp_path,
        require_market_session=True,
        require_broker_quote_validation=True,
        require_broker_risk_validation=True,
        daily_loss_limit=100.0,
        heartbeat_max_age_seconds=60,
        scheduled_run_max_age_seconds=60,
        datahub_provider="yahoo",
        kis_paper_trading=True,
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_beta_ready_events(store)

    result = CliRunner().invoke(app, ["beta-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "kis_paper_trading_enabled" in result.output


def test_beta_preflight_cli_fails_when_datahub_is_mock(tmp_path):
    config_path = _live_approval_config(
        tmp_path,
        require_market_session=True,
        require_broker_quote_validation=True,
        require_broker_risk_validation=True,
        daily_loss_limit=100.0,
        heartbeat_max_age_seconds=60,
        scheduled_run_max_age_seconds=60,
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_beta_ready_events(store)

    result = CliRunner().invoke(app, ["beta-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "datahub_mock_provider" in result.output


def test_health_live_approval_preflight_fails_unsafe_config(tmp_path):
    config_path = _live_approval_config(
        tmp_path,
        live_order_enabled=False,
        require_reconciliation_pass=False,
        max_daily_live_order_count=0,
    )
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    report = HealthService(config, store).run()
    checks = {check.name: check for check in report.checks}

    assert checks["live_approval_preflight"].status == "fail"
    assert "reconciliation_not_required" in checks["live_approval_preflight"].details["failures"]
    assert "order_posture_disabled" in checks["live_approval_preflight"].details["warnings"]


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


def test_recover_live_order_cli_records_completion_after_reconciliation(tmp_path):
    config_path = _live_approval_config(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "run_snapshot",
        "BROKER-ACCOUNT",
        {
            "account": {
                "account_id": "BROKER-ACCOUNT",
                "cash": 100.0,
                "buying_power": 100.0,
                "positions": [],
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )
    store.save_system_event("run_reconcile", "broker_reconciliation", {"passed": True})

    result = CliRunner().invoke(
        app,
        [
            "recover-live-order",
            "--config",
            str(config_path),
            "--reason",
            "broker truth reconciled",
        ],
    )

    assert result.exit_code == 0
    assert "recovery_completed" in result.output
    assert store.list_system_events_by_type("live_order_recovery_completed")


def _readonly_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/examples/live_readonly_multi_asset_kis.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "kis_access_token.json")
    config_path = tmp_path / "multi_asset_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _live_approval_config(
    tmp_path: Path,
    *,
    live_order_enabled: bool = True,
    require_reconciliation_pass: bool = True,
    max_daily_live_order_count: int = 3,
    require_market_session: bool = False,
    require_broker_quote_validation: bool = False,
    require_broker_risk_validation: bool = False,
    daily_loss_limit: float | None = None,
    heartbeat_max_age_seconds: int = 0,
    scheduled_run_max_age_seconds: int = 0,
    datahub_provider: str | None = None,
    kis_paper_trading: bool = False,
) -> Path:
    raw = yaml.safe_load(Path("configs/examples/live_approval_us_etf.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "kis_access_token.json")
    raw["kis"]["paper_trading"] = kis_paper_trading
    if datahub_provider is not None:
        raw["datahub"] = {"provider": datahub_provider}
    raw["execution"]["order_posture"] = "armed" if live_order_enabled else "disabled"
    raw["execution"]["require_reconciliation_pass"] = require_reconciliation_pass
    raw["execution"]["live_order_limits"]["max_daily_order_count"] = max_daily_live_order_count
    raw["execution"]["market_session"]["required"] = require_market_session
    raw["execution"]["broker_validation"]["require_quote_validation"] = (
        require_broker_quote_validation
    )
    raw["execution"]["broker_validation"]["require_risk_validation"] = (
        require_broker_risk_validation
    )
    raw["execution"]["live_order_limits"]["daily_loss_limit"] = daily_loss_limit
    raw["monitoring"] = {
        "heartbeat_max_age_seconds": heartbeat_max_age_seconds,
        "scheduled_run_max_age_seconds": scheduled_run_max_age_seconds,
    }
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _save_beta_ready_events(store: StateStore) -> None:
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
