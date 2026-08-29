import io
import urllib.error
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
from maestro.core.enums import OrderSide, OrderStatus, RunMode
from maestro.execution.base import OrderIntent
from maestro.execution.live_orders import LiveOrderLifecycleNotification
from maestro.integrations.telegram.bot import (
    TelegramApiRejected,
    TelegramApprovalNotifier,
    TelegramApprovalService,
    TelegramBotAPIClient,
    TelegramLiveOrderNotificationClient,
)
from maestro.integrations.telegram.formatter import format_approval_request
from maestro.integrations.telegram.ui.cards import TELEGRAM_TEXT_LIMIT, telegram_text_length
from maestro.state.store import StateStore


class FakeTelegramClient:
    def __init__(self, updates: list[dict[str, Any]] | object) -> None:
        self.updates = updates
        self.sent_messages: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, str]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.edited_reply_markups: list[dict[str, Any]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        self.get_updates_calls.append({"offset": offset, "timeout_seconds": timeout_seconds})
        if len(self.get_updates_calls) == 1:
            return {"ok": True, "result": self.updates}
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id: str, text: str) -> dict[str, Any]:
        self.answered_callbacks.append({"callback_query_id": callback_query_id, "text": text})
        return {"ok": True, "result": True}

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.edited_reply_markups.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}


class FailingEditTelegramClient(FakeTelegramClient):
    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("edit failed")


class FailingAllEditTelegramClient(FailingEditTelegramClient):
    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("reply markup edit failed")


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


def callback_update(
    data: str,
    *,
    update_id: int = 1,
    user_id: int = 10,
    chat_id: int = 100,
    message_id: int = 50,
    text: str = "Maestro approval request",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": message_id, "text": text},
            "from": {"id": user_id, "username": "approver"},
        },
    }


def test_telegram_service_sends_request_and_receives_approval():
    request = approval_request()
    client = FakeTelegramClient([callback_update(f"approve:{request.approval_id}")])
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
    assert client.sent_messages[0]["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": "approve:appr_test"},
                {"text": "Reject", "callback_data": "reject:appr_test"},
            ]
        ]
    }
    assert message.startswith("📩 투자 주문을 진행할까요?")
    assert "MOCK_ETF_A" in message
    assert "approve appr_test" not in message
    assert "reject appr_test" not in message


def test_telegram_approval_message_shows_order_details_and_strategy_source():
    request = approval_request().model_copy(
        update={
            "source_strategy_ids": ["sample_static_allocation"],
            "proposed_orders": [
                {
                    "symbol": "133690",
                    "name": "TIGER 미국나스닥100",
                    "broker_symbol": "133690",
                    "exchange_code": "KRX",
                    "currency": "KRW",
                    "quantity": 10,
                    "price": 190000,
                    "side": "buy",
                    "notional": 1900000.0,
                },
                {
                    "symbol": "QLD",
                    "name": "ProShares Ultra QQQ",
                    "broker_symbol": "QLD",
                    "exchange_code": "AMEX",
                    "broker_product": "kis_overseas_stock",
                    "currency": "USD",
                    "quantity": 2,
                    "price": 90.25,
                    "side": "buy",
                    "notional": 180.5,
                },
            ],
        }
    )

    message = format_approval_request(request)

    assert "🧠 Strategy: sample_static_allocation" in message
    assert "💰 Total: 1,900,000.00 KRW, 180.50 USD" in message
    assert "1. 🟢 BUY · 🇰🇷 국내 KRX" in message
    assert "종목: 133690 TIGER 미국나스닥100" in message
    assert "코드: 133690" in message
    assert "수량: 10" in message
    assert "지정가: 190,000.00 KRW" in message
    assert "금액: 1,900,000.00 KRW" in message
    assert "2. 🟢 BUY · 🌐 해외 AMEX" in message
    assert "종목: QLD ProShares Ultra QQQ" in message
    assert "지정가: 90.25 USD" in message
    assert "금액: 180.50 USD" in message


def test_telegram_approval_message_uses_current_virtuoso_strategy_names():
    request = approval_request().model_copy(
        update={"source_strategy_ids": ["ataraxia", "crescendo_us", "trading_agents"]}
    )

    message = format_approval_request(request)

    assert "🧠 Strategy: Tranquillo, Crescendo, Fugue" in message
    assert "ataraxia" not in message
    assert "crescendo_us" not in message
    assert "trading_agents" not in message


def test_approval_manager_records_source_strategy_ids():
    manager = ApprovalManager(
        ApprovalConfig(enabled=True, provider="console", require_approval=True),
        run_mode=RunMode.PAPER,
        profile_name="kis_brokerage_us",
    )

    request, decision, message = manager.request_approval(
        "run_test",
        [
            OrderIntent(
                order_id="ord_test",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                price=200,
                notional=200,
            )
        ],
        [],
        source_strategy_ids=["strategy_a", "strategy_b"],
    )

    assert request is not None
    assert decision is not None
    assert message is not None
    assert request.source_strategy_ids == ["strategy_a", "strategy_b"]
    assert request.profile_name == "kis_brokerage_us"
    assert message.startswith("📩 투자 주문을 진행할까요?")
    assert "strategy_a, strategy_b 전략" in message


def test_approval_formatter_shows_operator_profile_name():
    request = approval_request().model_copy(update={"profile_name": "kis_brokerage_us"})

    message = format_approval_request(request)

    assert "📁 Profile: kis_brokerage_us" in message


def test_telegram_service_sends_every_order_and_risk_reason_before_buttons():
    orders = [
        {"symbol": f"MOCK_{index:03d}", "side": "buy", "notional": 100.0, "quantity": 1}
        for index in range(40)
    ]
    request = approval_request().model_copy(
        update={
            "proposed_orders": orders,
            "order_count": len(orders),
            "risk_violations": [f"위반 사유 {index}" for index in range(10)],
        }
    )
    client = FakeTelegramClient([callback_update(f"approve:{request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    _, message = service.request_decision(request)

    sent_text = "\n".join(item["text"] for item in client.sent_messages)
    for index in range(40):
        assert f"MOCK_{index:03d}" in sent_text  # 승인 전에 모든 주문을 볼 수 있어야 한다
    for index in range(10):
        assert f"위반 사유 {index}" in sent_text  # 위험 사유 원문도 노출
    for item in client.sent_messages:
        assert telegram_text_length(item["text"]) <= TELEGRAM_TEXT_LIMIT
    assert client.sent_messages[-1]["reply_markup"] is not None  # 버튼은 마지막 메시지에
    assert all(item["reply_markup"] is None for item in client.sent_messages[:-1])
    assert "MOCK_039" in message


def test_telegram_service_receives_button_approval():
    request = approval_request()
    client = FakeTelegramClient([callback_update(f"approve:{request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"
    assert decision.decided_by == "telegram:approver"
    assert decision.reason == "Telegram button approved callback."
    assert client.answered_callbacks == [
        {"callback_query_id": "callback-1", "text": "Approval approved."}
    ]
    assert client.edited_messages == [
        {
            "chat_id": 100,
            "message_id": 50,
            "text": (
                "Maestro approval request\n\n"
                f"Decision: approved by telegram:approver at {decision.decided_at.isoformat()}"
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "✅ Approved", "callback_data": "decision:appr_test"}]
                ]
            },
        }
    ]


def test_telegram_service_receives_button_rejection():
    request = approval_request()
    client = FakeTelegramClient([callback_update(f"reject:{request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "rejected"
    assert client.answered_callbacks == [
        {"callback_query_id": "callback-1", "text": "Approval rejected."}
    ]
    assert client.edited_messages[0]["reply_markup"] == {
        "inline_keyboard": [[{"text": "⛔ Rejected", "callback_data": "decision:appr_test"}]]
    }


def test_telegram_service_updates_buttons_when_text_edit_fails():
    request = approval_request()
    client = FailingEditTelegramClient([callback_update(f"approve:{request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"
    assert client.edited_reply_markups == [
        {
            "chat_id": 100,
            "message_id": 50,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "✅ Approved", "callback_data": "decision:appr_test"}]
                ]
            },
        }
    ]


def test_telegram_service_sends_confirmation_when_message_edits_fail():
    request = approval_request()
    client = FailingAllEditTelegramClient([callback_update(f"approve:{request.approval_id}")])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision, _ = service.request_decision(request)

    assert decision.status == "approved"
    assert client.sent_messages[-1]["chat_id"] == 100
    assert "Maestro approval decision recorded" in client.sent_messages[-1]["text"]
    assert "status: approved" in client.sent_messages[-1]["text"]


def test_telegram_service_ignores_manual_text_commands():
    request = approval_request()
    client = FakeTelegramClient([])
    service = TelegramApprovalService(
        client=client,
        chat_ids=[100],
        allowed_user_ids=[10],
        poll_interval_seconds=0,
    )

    decision = service._decision_from_update(update(f"approve {request.approval_id}"), request)

    assert decision is None


def test_telegram_service_ignores_button_from_wrong_user_and_chat():
    request = approval_request()
    client = FakeTelegramClient(
        [
            callback_update(f"approve:{request.approval_id}", update_id=1, user_id=99),
            callback_update(f"approve:{request.approval_id}", update_id=2, chat_id=999),
            callback_update(f"approve:{request.approval_id}", update_id=3),
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
    assert client.answered_callbacks == [
        {"callback_query_id": "callback-3", "text": "Approval approved."}
    ]
    assert len(client.edited_messages) == 1


def test_telegram_service_answers_stale_button_callback():
    request = approval_request()
    client = FakeTelegramClient(
        [
            callback_update("approve:appr_stale", update_id=1),
            callback_update(f"approve:{request.approval_id}", update_id=2),
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
    assert client.answered_callbacks == [
        {"callback_query_id": "callback-1", "text": "This approval request is no longer active."},
        {"callback_query_id": "callback-2", "text": "Approval approved."},
    ]
    assert len(client.edited_messages) == 1


def test_telegram_service_sends_request_to_all_configured_chats():
    request = approval_request()
    client = FakeTelegramClient([callback_update(f"approve:{request.approval_id}", chat_id=200)])
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
    client = FakeTelegramClient([callback_update(f"reject:{request.approval_id}")])
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
            callback_update(f"approve:{request.approval_id}", update_id=1, user_id=99),
            callback_update(f"approve:{request.approval_id}", update_id=2, chat_id=999),
            callback_update(f"approve:{request.approval_id}", update_id=3, user_id=10, chat_id=100),
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


def test_telegram_service_returns_one_decision_for_duplicate_callbacks():
    request = approval_request()
    client = FakeTelegramClient(
        [
            callback_update(f"approve:{request.approval_id}", update_id=1),
            callback_update(f"reject:{request.approval_id}", update_id=2),
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


def test_telegram_service_ignores_wrong_callback_approval_id():
    request = approval_request()
    client = FakeTelegramClient(
        [
            callback_update("approve:appr_other", update_id=1),
            callback_update(f"approve:{request.approval_id}", update_id=2),
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
            whitelisted_user_ids=[10],
            timeout_seconds=60,
        ),
        run_mode=RunMode.LIVE_APPROVAL,
        telegram_client=FakeTelegramClient([callback_update(f"approve:{request.approval_id}")]),
    )

    _, decision, _ = manager.request_approval("run_test", [], [], [])

    assert decision is not None
    assert decision.status == "approved"


def test_telegram_approval_manager_requires_whitelist_in_live_approval():
    client = FakeTelegramClient([])
    manager = ApprovalManager(
        ApprovalConfig(
            enabled=True,
            provider="telegram",
            require_approval=True,
            telegram_allowed_chat_ids=[100],
            timeout_seconds=60,
        ),
        run_mode=RunMode.LIVE_APPROVAL,
        telegram_client=client,
    )

    with pytest.raises(ValueError, match="Telegram approval whitelist is required"):
        manager.request_approval("run_test", [], [], [])

    assert client.sent_messages == []


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
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["execution"]["order_posture"] = "dry_run"
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
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


def test_state_store_converts_approval_unique_conflict_to_value_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    payload = {"decision": {"status": "approved"}}

    store.save_approval("run_1", "appr_1", payload)
    monkeypatch.setattr(store, "approval_exists", lambda approval_id: False)

    with pytest.raises(ValueError, match="Approval decision already exists: appr_1"):
        store.save_approval("run_2", "appr_1", payload)


def test_non_telegram_notifier_keeps_every_order_risk_and_account():
    now = utc_now()
    request = ApprovalRequest(
        approval_id="appr_audit",
        run_id="run_audit",
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        channel="console",
        order_count=8,
        estimated_notional=8000.0,
        proposed_orders=[
            {
                "symbol": f"MOCK_ETF_{index}",
                "side": "buy",
                "notional": 1000.0,
                "quantity": 1,
                "account_id": f"kis_{index % 4}",
            }
            for index in range(8)
        ],
        risk_violations=["max_notional exceeded", "sector cap exceeded"],
    )

    message = TelegramApprovalNotifier().send_approval_request(request)

    for index in range(8):
        assert f"MOCK_ETF_{index}" in message
    for index in range(4):
        assert f"계좌: kis_{index}" in message
    assert "max_notional exceeded" in message
    assert "sector cap exceeded" in message


def test_telegram_bot_api_client_preserves_rejection_metadata_on_200_ok_false(
    monkeypatch: pytest.MonkeyPatch,
):
    class DummyResponse:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    body = (
        b'{"ok": false, "error_code": 400, "description": "Bad Request: message to edit not found"}'
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: DummyResponse(body))
    monkeypatch.setenv("TEST_TG_TOKEN", "123:test")
    client = TelegramBotAPIClient(token_env="TEST_TG_TOKEN")

    with pytest.raises(TelegramApiRejected) as exc_info:
        client.edit_message_text(chat_id=100, message_id=50, text="hello")

    exc = exc_info.value
    assert isinstance(exc, RuntimeError)
    assert exc.method == "editMessageText"
    assert exc.error_code == 400
    assert exc.description == "Bad Request: message to edit not found"


def test_telegram_bot_api_client_preserves_rejection_metadata_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
):
    body = (
        b'{"ok": false, "error_code": 400, "description": "Bad Request: message to edit not found"}'
    )
    http_error = urllib.error.HTTPError(
        url="https://api.telegram.org/bot123:test/editMessageText",
        code=400,
        msg="Bad Request",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )

    def fake_urlopen(req, timeout=None):
        raise http_error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("TEST_TG_TOKEN", "123:test")
    client = TelegramBotAPIClient(token_env="TEST_TG_TOKEN")

    with pytest.raises(TelegramApiRejected) as exc_info:
        client.edit_message_text(chat_id=100, message_id=50, text="hello")

    exc = exc_info.value
    assert isinstance(exc, RuntimeError)
    assert exc.method == "editMessageText"
    assert exc.error_code == 400
    assert exc.description == "Bad Request: message to edit not found"


def test_telegram_bot_api_client_http_error_with_non_json_body_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
):
    http_error = urllib.error.HTTPError(
        url="https://api.telegram.org/bot123:test/editMessageText",
        code=502,
        msg="Bad Gateway",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b"<html>502 Bad Gateway</html>"),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: (_ for _ in ()).throw(http_error)
    )
    monkeypatch.setenv("TEST_TG_TOKEN", "123:test")
    client = TelegramBotAPIClient(token_env="TEST_TG_TOKEN")

    with pytest.raises(
        RuntimeError, match="Telegram Bot API unavailable for method: editMessageText"
    ):
        client.edit_message_text(chat_id=100, message_id=50, text="hello")
