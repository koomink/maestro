from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from maestro.approval.manager import ApprovalManager
from maestro.approval.models import ApprovalRequest
from maestro.cli import app
from maestro.config.models import ApprovalConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus, RunMode
from maestro.execution.live_orders import LiveOrderLifecycleNotification
from maestro.integrations.telegram.bot import (
    TelegramApprovalService,
    TelegramBotAPIClient,
    TelegramLiveOrderNotificationClient,
)
from maestro.state.store import StateStore


class FakeTelegramClient:
    def __init__(self, updates: list[dict[str, Any]] | object) -> None:
        self.updates = updates
        self.sent_messages: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        self.get_updates_calls.append({"offset": offset, "timeout_seconds": timeout_seconds})
        if len(self.get_updates_calls) == 1:
            return {"ok": True, "result": self.updates}
        return {"ok": True, "result": []}


def approval_request() -> ApprovalRequest:
    now = utc_now()
    return ApprovalRequest(
        approval_id="appr_test",
        run_id="run_test",
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        channel="telegram",
        order_count=2,
        estimated_notional=1000.0,
        proposed_orders=[
            {"symbol": "MOCK_ETF_A", "side": "buy", "notional": 600.0},
            {"symbol": "MOCK_ETF_B", "side": "buy", "notional": 400.0},
        ],
    )


def expired_approval_request() -> ApprovalRequest:
    now = utc_now()
    return ApprovalRequest(
        approval_id="appr_expired",
        run_id="run_test",
        created_at=now - timedelta(seconds=2),
        expires_at=now - timedelta(seconds=1),
        channel="telegram",
        order_count=1,
        estimated_notional=100.0,
        proposed_orders=[{"symbol": "MOCK_ETF_A", "side": "buy", "notional": 100.0}],
    )


def update(
    text: str,
    *,
    update_id: int = 1,
    user_id: int = 10,
    chat_id: int = 100,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": "approver"},
        },
    }


def test_telegram_service_sends_request_and_receives_approval():
    request = approval_request()
    client = FakeTelegramClient([update(f"approve {request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, message = service.request_decision(request)

    assert decision.status == "approved"
    assert decision.decided_by == "telegram:approver"
    assert message == client.sent_messages[0]["text"]
    assert client.sent_messages[0]["chat_id"] == 100
    assert "proposed_orders:" in message
    assert "buy MOCK_ETF_A notional=600.00" in message
    assert "approve appr_test" in message
    assert "reject appr_test" in message


def test_telegram_service_sends_request_to_all_configured_chats():
    request = approval_request()
    client = FakeTelegramClient([update(f"approve {request.approval_id}", chat_id=200)])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100, 200],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"
    assert [item["chat_id"] for item in client.sent_messages] == [100, 200]


def test_telegram_service_receives_rejection():
    request = approval_request()
    client = FakeTelegramClient([update(f"reject {request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "rejected"


def test_telegram_service_ignores_unapproved_user_and_wrong_chat():
    request = approval_request()
    client = FakeTelegramClient(
        [
            update(f"approve {request.approval_id}", update_id=1, user_id=99),
            update(f"approve {request.approval_id}", update_id=2, chat_id=999),
            update(f"approve {request.approval_id}", update_id=3, user_id=10, chat_id=100),
        ]
    )
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"
    assert decision.decided_by == "telegram:approver"


def test_telegram_service_returns_one_decision_for_duplicate_updates():
    request = approval_request()
    client = FakeTelegramClient(
        [
            update(f"approve {request.approval_id}", update_id=1),
            update(f"reject {request.approval_id}", update_id=2),
        ]
    )
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"


def test_telegram_service_ignores_wrong_approval_id():
    request = approval_request()
    client = FakeTelegramClient(
        [
            update("approve appr_other", update_id=1),
            update(f"approve {request.approval_id}", update_id=2),
        ]
    )
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"


def test_telegram_service_times_out_to_expired():
    request = expired_approval_request()
    client = FakeTelegramClient([])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "expired"


def test_telegram_service_rejects_empty_allowed_chat_ids():
    with pytest.raises(ValueError, match="telegram_allowed_chat_ids"):
        TelegramApprovalService(
            client=FakeTelegramClient([]),
            chat_ids=[],
            allowed_user_ids=[10],
            poll_interval_seconds=0,
        )


def test_telegram_service_rejects_malformed_get_updates_response():
    request = approval_request()
    client = FakeTelegramClient({"not": "a list"})
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    with pytest.raises(ValueError, match="Malformed Telegram updates"):
        service.request_decision(request)


def test_telegram_approval_manager_allows_live_approval_mode(monkeypatch: pytest.MonkeyPatch):
    request = approval_request()
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: request.approval_id)
    manager = ApprovalManager(
        ApprovalConfig(
            enabled=True,
            provider="telegram",
            require_approval=True,
            telegram_allowed_chat_ids=[100],
            timeout_seconds=1,
        ),
        run_mode=RunMode.LIVE_APPROVAL,
        telegram_client=FakeTelegramClient([update(f"approve {request.approval_id}")]),
    )

    _, decision, _ = manager.request_approval("run_test", [], [], [])

    assert decision is not None
    assert decision.status == "approved"


def test_telegram_approval_manager_rejects_live_readonly_mode():
    manager = ApprovalManager(
        ApprovalConfig(
            enabled=True,
            provider="telegram",
            require_approval=True,
            telegram_allowed_chat_ids=[100],
            timeout_seconds=1,
        ),
        run_mode=RunMode.LIVE_READONLY,
        telegram_client=FakeTelegramClient([]),
    )

    with pytest.raises(ValueError, match="paper or live_approval"):
        manager.request_approval("run_test", [], [], [])


def test_live_smoke_telegram_approval_validates_config_without_network(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.example.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["execution"]["live_order_enabled"] = False
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(
        app,
        [
            "live-smoke",
            "--config",
            str(config_path),
            "--check",
            "telegram-approval",
            "--allow-mock",
        ],
    )

    assert result.exit_code == 0
    assert "check=telegram_approval status=ok provider=telegram mock=true" in result.output
    assert "chats=1" in result.output
    assert "whitelisted_users=1" in result.output


def test_telegram_live_order_notification_uses_fake_client_only():
    client = FakeTelegramClient([])
    notifier = TelegramLiveOrderNotificationClient(client=client, chat_ids=[100, 200])

    notifier.notify(
        LiveOrderLifecycleNotification(
            run_id="run_live",
            order_id="ord_live",
            broker_order_id="KIS-1",
            status=OrderStatus.FILLED,
            message="Live order status polled.",
        )
    )

    assert [item["chat_id"] for item in client.sent_messages] == [100, 200]
    assert "status: filled" in client.sent_messages[0]["text"]
    assert "KIS-1" in client.sent_messages[0]["text"]


def test_telegram_token_value_is_not_in_missing_env_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MAESTRO_TEST_TELEGRAM_TOKEN", raising=False)

    with pytest.raises(ValueError) as exc_info:
        TelegramBotAPIClient(token_env="MAESTRO_TEST_TELEGRAM_TOKEN")

    message = str(exc_info.value)
    assert "MAESTRO_TEST_TELEGRAM_TOKEN" in message
    assert "bot" not in message.lower().replace("telegram bot", "")


def test_state_store_rejects_duplicate_approval_decisions(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    payload = {"decision": {"status": "approved"}}

    store.save_approval("run_1", "appr_1", payload)

    assert store.approval_exists("appr_1") is True
    with pytest.raises(ValueError, match="already exists"):
        store.save_approval("run_1", "appr_1", payload)
