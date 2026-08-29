"""단계 2 — 아무것도 하지 않은 날도 한 줄로 알린다.

침묵은 "오늘은 거래가 없었다"와 "봇이 죽었다"를 구분해 주지 않는다. 카드가
아니라 한 줄 알림이므로 lifecycle을 거치지 않는다.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro import cli as _cli
from maestro.cli import _run_daily_signal_approval
from maestro.config.loader import load_config
from maestro.integrations.telegram.ui.catalog import NO_ACTION_NOTICE
from maestro.state.store import StateStore


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
    no_order_reasons: list[str] = []

    def __init__(self, strategies):
        self.loaded_strategies = list(strategies)


def _drive_no_action_day(
    monkeypatch,
    config,
    *,
    funding_present=False,
    budget_present=False,
    funding_requests: list[dict[str, Any]] | None = None,
    budget_requests: list[dict[str, Any]] | None = None,
    strategies=("tranquillo",),
    failing_chats=(),
    order=None,
    crash_after_send=False,
):
    sent: list[tuple[int, str]] = []
    unreachable = set(failing_chats)
    trace = order if order is not None else []

    class FakeBotClient:
        def __init__(self, **kwargs):
            del kwargs

        def send_message(self, chat_id, text, reply_markup=None):
            del reply_markup
            if chat_id in unreachable:
                raise RuntimeError(f"telegram unreachable for chat {chat_id}")
            trace.append("send")
            sent.append((chat_id, text))
            if crash_after_send:
                raise KeyboardInterrupt("process died before recording the send")
            return {"ok": True, "result": {"message_id": len(sent)}}

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run_signal(self, **kwargs):
            del kwargs
            return _Summary(strategies)

    # Seed signal package in StateStore based on funding_present / budget_present
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    frs = funding_requests if funding_requests is not None else ([{"request_id": "fund_1", "card_delivery_version": 1}] if funding_present else [])
    brs = budget_requests if budget_requests is not None else ([{"request_id": "budget_1", "card_delivery_version": 1}] if budget_present else [])
    store.save_signal_package(
        "signal_quiet",
        {
            "orders_preview": [],
            "funding_requests": frs,
            "budget_requests": brs,
        },
    )

    monkeypatch.setenv(config.approval.telegram_bot_token_env, "test-token")
    monkeypatch.setattr("maestro.cli._load_operator_config", lambda path: (config, None))
    monkeypatch.setattr("maestro.cli._refresh_daily_readonly", lambda config, identity: None)
    monkeypatch.setattr("maestro.cli.MaestroOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", FakeBotClient)
    real_save = _cli.save_audited_system_event

    def traced_save(store, audit, run_id, event_type, payload):
        if event_type == "telegram_no_action_notice":
            trace.append("claim")
        return real_save(store, audit, run_id, event_type, payload)

    monkeypatch.setattr("maestro.cli.save_audited_system_event", traced_save)
    monkeypatch.setattr(
        "maestro.cli._send_signal_summary_notification", lambda config, summary: None
    )

    try:
        _run_daily_signal_approval(
            readonly_config=Path("readonly.yaml"),
            signal_config=Path("signal.yaml"),
            approval_config=Path("approval.yaml"),
            stop_telegram_operator=False,
            telegram_operator_service="maestro-telegram-operator.service",
        )
    except KeyboardInterrupt:
        pass  # 프로세스가 죽은 자리를 그대로 흉내낸 것
    return sent


def test_a_quiet_day_still_says_something(monkeypatch, tmp_path):
    """침묵은 '거래가 없었다'와 '봇이 죽었다'를 구분해 주지 않는다."""
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config)

    assert sent == [(100, NO_ACTION_NOTICE), (200, NO_ACTION_NOTICE)]


def test_a_funding_request_day_is_not_a_no_action_day(monkeypatch, tmp_path, capsys):
    """요청이 있는 날에는 '매매할 것이 없어요'를 보내지 않는다."""
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, funding_present=True)

    assert sent == []
    out = capsys.readouterr().out
    assert "status=funding_required" in out
    assert "status=no_action" not in out
    assert "request_delivery_failed" not in out
    assert "telegram_funding_request=" not in out
    assert "telegram_budget_request=" not in out


def test_a_budget_request_day_is_not_a_no_action_day(monkeypatch, tmp_path, capsys):
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, budget_present=True)

    assert sent == []
    out = capsys.readouterr().out
    assert "status=budget_required" in out
    assert "status=no_action" not in out
    assert "request_delivery_failed" not in out
    assert "telegram_funding_request=" not in out
    assert "telegram_budget_request=" not in out


def test_both_funding_and_budget_requests_emit_both_statuses(monkeypatch, tmp_path, capsys):
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, funding_present=True, budget_present=True)

    assert sent == []
    out = capsys.readouterr().out
    assert "status=budget_required" in out
    assert "status=funding_required" in out
    assert "status=no_action" not in out
    assert "request_delivery_failed" not in out


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


def test_the_notice_is_sent_once_per_day_and_scope(monkeypatch, tmp_path):
    """같은 날 같은 전략 묶음을 다시 돌려도 알림이 다시 가면 안 된다.

    스펙 「중복 방지」: 카드 생성·노옵 알림은 duplicate_key로 멱등 처리한다.
    """
    config = _telegram_config(tmp_path)

    first = _drive_no_action_day(monkeypatch, config)
    second = _drive_no_action_day(monkeypatch, config)

    assert first == [(100, NO_ACTION_NOTICE), (200, NO_ACTION_NOTICE)]
    assert second == [], "재실행이 같은 알림을 다시 보냈다"


def test_the_other_market_run_still_gets_its_own_notice(monkeypatch, tmp_path):
    """KR·US 런은 같은 날 따로 돈다. 날짜만으로 접으면 한쪽이 침묵한다."""
    config = _telegram_config(tmp_path)

    _drive_no_action_day(monkeypatch, config)
    sent = _drive_no_action_day(monkeypatch, config, strategies=["crescendo_us"])

    assert sent == [(100, NO_ACTION_NOTICE), (200, NO_ACTION_NOTICE)]


def test_one_unreachable_chat_does_not_silence_the_rest(monkeypatch, tmp_path):
    """첫 채팅이 실패했다고 뒤 채팅이 시도조차 못 하면 안 된다."""
    config = _telegram_config(tmp_path)

    sent = _drive_no_action_day(monkeypatch, config, failing_chats={100})

    assert sent == [(200, NO_ACTION_NOTICE)]


def test_the_claim_is_written_before_the_send(monkeypatch, tmp_path):
    """전송 후 기록 전에 죽으면 다음 실행이 같은 알림을 다시 보낸다.

    승인 카드와 달리 이 알림에는 버튼이 없으므로, 세 상태(intent/result/failure)
    까지 가지 않고 전송 전 원자적 claim으로 at-most-once를 취한다.
    """
    config = _telegram_config(tmp_path)
    order: list[str] = []

    sent = _drive_no_action_day(monkeypatch, config, order=order)

    assert sent, "알림이 나가야 한다"
    assert order[:2] == ["claim", "send"], f"claim이 먼저여야 한다: {order}"


def test_a_notice_lost_to_a_crash_after_sending_is_not_repeated(monkeypatch, tmp_path):
    """전송은 성공했는데 프로세스가 죽은 경우 — claim이 이미 남아 있다."""
    config = _telegram_config(tmp_path)
    crashed = _drive_no_action_day(monkeypatch, config, crash_after_send=True)
    assert crashed == [(100, NO_ACTION_NOTICE)]

    sent = _drive_no_action_day(monkeypatch, config)

    assert sent == [(200, NO_ACTION_NOTICE)], "중단된 실행이 보낸 알림을 다시 보냈다"


def test_a_chat_that_failed_is_not_resent_the_next_run(monkeypatch, tmp_path):
    """at-most-once의 대가: 전송이 실패한 채팅은 그날 알림을 잃는다.

    claim을 먼저 쓰기 때문이다. 놓친 채팅은 로그로 남고, 일간 실행이 통째로
    실패한 경우는 별도의 실패 알림이 덮는다.
    """
    config = _telegram_config(tmp_path)
    _drive_no_action_day(monkeypatch, config, failing_chats={100})

    sent = _drive_no_action_day(monkeypatch, config)

    assert sent == []
