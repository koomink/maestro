"""단계 2 — 아무것도 하지 않은 날도 한 줄로 알린다.

침묵은 "오늘은 거래가 없었다"와 "봇이 죽었다"를 구분해 주지 않는다. 카드가
아니라 한 줄 알림이므로 lifecycle을 거치지 않는다.
"""

from pathlib import Path

import pytest
import yaml

from maestro.cli import _run_daily_signal_approval
from maestro.config.loader import load_config
from maestro.integrations.telegram.ui.catalog import NO_ACTION_NOTICE


def _telegram_config(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": [100, 200],
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "no_action.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


class _Summary:
    signal_run_id = "signal_quiet"
    action_required = False
    orders_preview_count = 0
    contribution_override = False
    loaded_strategies = ["tranquillo"]
    no_order_reasons: list[str] = []


def _drive_no_action_day(monkeypatch, config, *, funding_sent=False, budget_sent=False):
    sent: list[tuple[int, str]] = []

    class FakeBotClient:
        def __init__(self, **kwargs):
            del kwargs

        def send_message(self, chat_id, text, reply_markup=None):
            del reply_markup
            sent.append((chat_id, text))
            return {"ok": True, "result": {"message_id": len(sent)}}

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run_signal(self, **kwargs):
            del kwargs
            return _Summary()

    monkeypatch.setenv(config.approval.telegram_bot_token_env, "test-token")
    monkeypatch.setattr("maestro.cli._load_operator_config", lambda path: (config, None))
    monkeypatch.setattr("maestro.cli._refresh_daily_readonly", lambda config, identity: None)
    monkeypatch.setattr("maestro.cli.MaestroOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", FakeBotClient)
    monkeypatch.setattr(
        "maestro.cli._send_signal_summary_notification", lambda config, summary: None
    )
    monkeypatch.setattr(
        "maestro.cli._send_signal_budget_request_notifications",
        lambda config, signal_run_id: budget_sent,
    )
    monkeypatch.setattr(
        "maestro.cli._send_signal_funding_request_notifications",
        lambda config, signal_run_id: funding_sent,
    )

    _run_daily_signal_approval(
        readonly_config=Path("readonly.yaml"),
        signal_config=Path("signal.yaml"),
        approval_config=Path("approval.yaml"),
        stop_telegram_operator=False,
        telegram_operator_service="maestro-telegram-operator.service",
    )
    return sent


def test_a_quiet_day_still_says_something(monkeypatch, tmp_path):
    """침묵은 '거래가 없었다'와 '봇이 죽었다'를 구분해 주지 않는다."""
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config)

    assert sent == [(100, NO_ACTION_NOTICE), (200, NO_ACTION_NOTICE)]


def test_a_funding_request_day_is_not_a_no_action_day(monkeypatch, tmp_path):
    """요청을 보낸 날에 '매매할 것이 없어요'를 덧붙이면 서로 모순된다."""
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, funding_sent=True)

    assert sent == []


def test_a_budget_request_day_is_not_a_no_action_day(monkeypatch, tmp_path):
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, budget_sent=True)

    assert sent == []


def test_the_notice_is_one_line_and_names_no_internals():
    """운영자가 읽는 문구다. run_id도 signal도 등장하지 않는다."""
    assert "\n" not in NO_ACTION_NOTICE
    assert "run_id" not in NO_ACTION_NOTICE
    assert "signal" not in NO_ACTION_NOTICE.lower()


@pytest.mark.parametrize("provider", ["console", "none"])
def test_a_non_telegram_provider_sends_nothing(monkeypatch, tmp_path, provider):
    config = _telegram_config(tmp_path)
    config.approval.provider = provider

    assert _drive_no_action_day(monkeypatch, config) == []
