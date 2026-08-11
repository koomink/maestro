import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.state.store import StateStore


def _telegram_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "telegram_operator.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def test_preflight_exits_zero_when_no_unresolved_approvals(tmp_path):
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run_1",
        "telegram_approval_ack",
        {"approval_id": "appr_1", "status": "approved", "schema_version": 2},
    )
    store.save_system_event(
        "run_1",
        "telegram_approval_resolution_completed",
        {"approval_id": "appr_1", "status": "approved"},
    )

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "status=safe" in result.stdout


def test_preflight_exits_nonzero_and_names_unresolved_approvals(tmp_path):
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run_1",
        "telegram_approval_ack",
        {"approval_id": "appr_unresolved", "status": "approved", "schema_version": 2},
    )

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "appr_unresolved" in result.stdout


def test_preflight_ignores_legacy_acks(tmp_path):
    """schema_version이 없는 ack는 구버전 의미론에서도 종결이라 롤백을 막지 않는다."""
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run_1",
        "telegram_approval_ack",
        {"approval_id": "appr_legacy", "status": "approved"},
    )

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 0


def test_preflight_require_quiesce_fails_when_operator_service_is_active(tmp_path, monkeypatch):
    config_path = _telegram_config_path(tmp_path)
    monkeypatch.setattr("maestro.cli._service_is_active", lambda unit: True)

    result = CliRunner().invoke(
        app,
        ["approval-rollback-preflight", "--config", str(config_path), "--require-quiesce"],
    )

    assert result.exit_code == 1
    assert "operator_still_running" in result.stdout


def test_preflight_require_quiesce_proceeds_when_operator_service_is_inactive(
    tmp_path, monkeypatch
):
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run_1",
        "telegram_approval_ack",
        {"approval_id": "appr_1", "status": "approved", "schema_version": 2},
    )
    store.save_system_event(
        "run_1",
        "telegram_approval_resolution_completed",
        {"approval_id": "appr_1", "status": "approved"},
    )
    monkeypatch.setattr("maestro.cli._service_is_active", lambda unit: False)

    result = CliRunner().invoke(
        app,
        ["approval-rollback-preflight", "--config", str(config_path), "--require-quiesce"],
    )

    assert result.exit_code == 0
    assert "status=safe" in result.stdout


def test_preflight_applies_no_time_window(tmp_path):
    """F7: 시간 창을 적용하면 오래된 미완 승인을 잃는 롤백이 조용히 통과한다.
    preflight가 막아야 하는 것이 바로 그 상태다."""
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event(
        "run_1",
        "telegram_approval_ack",
        {"approval_id": "appr_ancient", "status": "approved", "schema_version": 2},
    )
    backdated = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE system_events SET created_at = ?", (backdated,))

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "appr_ancient" in result.stdout
