from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.enums import SafetyState
from maestro.integrations.telegram.handlers import (
    TelegramOperatorCommandRouter,
    telegram_bot_commands,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.safety.controls import SafetyControlService
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_telegram_operator_read_commands_send_state_responses(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_order(
        "run_order",
        "ord_1",
        {
            "symbol": "MOCK_ETF_A",
            "side": "buy",
            "quantity": 1,
            "approval_status": "approved",
        },
    )
    store.save_approval(
        "run_approval",
        "appr_1",
        {
            "request": {"order_count": 1, "estimated_notional": 100.0},
            "decision": {"status": "approved", "decided_by": "telegram:operator"},
        },
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "12345678-01",
        {
            "account": {
                "account_id": "12345678-01",
                "cash": 950.0,
                "buying_power": 900.0,
                "positions": [{"symbol": "AAPL", "quantity": 1, "current_price": 50.0}],
                "source": "fixture",
            }
        },
    )

    for command in (
        "/help",
        "/status",
        "/health",
        "/portfolio",
        "/apps",
        "/orders",
        "/approvals",
    ):
        assert router.process_update(message_update(command))

    sent_text = "\n\n".join(message["text"] for message in client.sent_messages)
    assert "Maestro Telegram commands" in sent_text
    assert "Maestro status" in sent_text
    assert "maestro_cash:" not in sent_text
    assert "maestro_positions:" not in sent_text
    assert (
        "Broker\n- total_value: 1,000.00 unknown\n- cash: 950.00 unknown\n- positions: 1"
        in sent_text
    )
    assert "\ncash:" not in sent_text
    assert f"- state: {Path(config.state.sqlite_path).resolve()}" in sent_text
    assert f"- audit: {Path(config.audit.jsonl_path).resolve()}" in sent_text
    assert "Maestro health" in sent_text
    assert "Maestro portfolio" in sent_text
    assert "Maestro apps" in sent_text
    assert "Recent orders" in sent_text
    assert "Recent approvals" in sent_text
    assert len(store.list_system_events_by_type("telegram_command", limit=20)) == 7


def test_telegram_operator_status_groups_fields_for_readability(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "12345678-01",
        {
            "account": {
                "account_id": "12345678-01",
                "cash": 950.0,
                "positions": [{"symbol": "AAPL", "quantity": 1, "current_price": 50.0}],
                "source": "fixture",
            }
        },
    )

    assert router.process_update(message_update("/status"))

    text = client.sent_messages[-1]["text"]
    assert text.startswith("Maestro status\n\nRuntime\n")
    assert (
        "\nBroker\n- total_value: 1,000.00 unknown\n- cash: 950.00 unknown\n- positions: 1\n"
        in text
    )
    assert "\nActivity\n- orders: 0\n- approvals: 0\n" in text
    assert "\nConfig\n- path:" in text
    assert "broker_total_value:" not in text
    assert "broker_cash:" not in text


def test_telegram_operator_account_masks_account_id(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "12345678-01",
        {
            "account": {
                "account_id": "12345678-01",
                "cash": 1000.0,
                "buying_power": 900.0,
                "positions": [{"symbol": "AAPL", "quantity": 1, "current_price": 50.0}],
                "source": "fixture",
            }
        },
    )

    assert router.process_update(message_update("/account"))

    text = client.sent_messages[-1]["text"]
    assert "Broker account snapshot" in text
    assert "12345678-01" not in text
    assert "total_value: 1,050.00 unknown" in text
    assert "positions_market_value: 50.00 unknown" in text
    assert "orderable_cash:" not in text
    assert "buying_power:" not in text
    assert "positions: 1" in text


def test_telegram_operator_account_displays_currency_breakdowns(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "12345678-01",
        {
            "account": {
                "account_id": "12345678-01",
                "cash": 1020.0,
                "cash_by_currency": {"KRW": 1000.0, "USD": 20.0},
                "buying_power": 900.0,
                "positions": [
                    {
                        "symbol": "AAPL",
                        "quantity": 1,
                        "current_price": 50.0,
                        "currency": "USD",
                    },
                    {
                        "symbol": "005930",
                        "quantity": 1,
                        "current_price": 1000.0,
                        "currency": "KRW",
                    },
                ],
                "source": "fixture",
            }
        },
    )

    assert router.process_update(message_update("/account"))

    text = client.sent_messages[-1]["text"]
    assert "total_value: 2,000.00 KRW, 70.00 USD" in text
    assert "cash: 1,000.00 KRW, 20.00 USD" in text
    assert "positions_market_value: 1,000.00 KRW, 50.00 USD" in text
    assert "1,070.00" not in text


def test_telegram_operator_account_refreshes_broker_snapshot_before_response(tmp_path):
    config = load_config(_telegram_live_readonly_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_broker_account_snapshot(
        "run_stale_broker",
        "MOCK-ACCOUNT",
        {
            "account": {
                "account_id": "MOCK-ACCOUNT",
                "cash": 12_345_678.0,
                "buying_power": 12_345_678.0,
                "positions": [],
                "source": "stale_fixture",
            }
        },
    )

    assert router.process_update(message_update("/account"))

    assert client.sent_messages[-2]["text"] == "Broker account snapshot: refreshing"
    text = client.sent_messages[-1]["text"]
    assert "total_value: 10,000,000.00 KRW" in text
    assert "cash: 5,000,000.00 KRW" in text
    assert "positions_market_value: 5,000,000.00 KRW" in text
    assert "orderable_cash:" not in text
    assert "source: kis_mock" in text
    assert "12,345,678.00" not in text
    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    assert latest["payload"]["account"]["source"] == "kis_mock"


def test_telegram_operator_account_shows_latest_snapshot_when_refresh_fails(
    tmp_path,
    monkeypatch,
):
    config = load_config(_telegram_live_readonly_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_broker_account_snapshot(
        "run_latest_broker",
        "MOCK-ACCOUNT",
        {
            "account": {
                "account_id": "MOCK-ACCOUNT",
                "cash": 12_345_678.0,
                "buying_power": 12_345_678.0,
                "positions": [],
                "source": "stored_fixture",
            }
        },
    )

    def fail_refresh(*args, **kwargs):
        raise ValueError("KIS request failed with HTTP 500")

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.KISReadOnlyService.fetch_and_store_snapshot",
        fail_refresh,
    )

    assert router.process_update(message_update("/account"))

    assert client.sent_messages[-2]["text"] == "Broker account snapshot: refreshing"
    text = client.sent_messages[-1]["text"]
    assert "Broker account snapshot refresh failed: KIS request failed with HTTP 500" in text
    assert "Showing latest stored broker snapshot." in text
    assert "total_value: 12,345,678.00 unknown" in text
    assert "source: stored_fixture" in text


def test_telegram_operator_portfolio_refreshes_from_broker_snapshot_before_response(tmp_path):
    config = load_config(_telegram_live_readonly_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_portfolio_snapshot(
        "run_stale_portfolio",
        PortfolioState(cash=12_345_678.0, positions={"MOCK_ETF_A": 1.0}),
    )

    assert router.process_update(message_update("/portfolio"))

    assert client.sent_messages[-2]["text"] == (
        "Maestro portfolio: refreshing from broker snapshot"
    )
    text = client.sent_messages[-1]["text"]
    assert "CASH" in text
    assert "- KRW: 5,000,000" in text
    assert "MOCK_ETF_A: 30,000 @ 100 KRW = 3,000,000 KRW" in text
    assert "MOCK_ETF_B: 40,000 @ 50 KRW = 2,000,000 KRW" in text
    assert "12,345,678" not in text
    state = store.load_latest_portfolio_state()
    assert state.cash == 5_000_000.0
    assert state.positions == {"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0}


def test_telegram_operator_portfolio_displays_cash_by_currency(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_portfolio_snapshot(
        "run_multi_currency",
        PortfolioState(
            cash=1020.0,
            cash_by_currency={"KRW": 1000.0, "USD": 20.0},
            positions={"005930": 1.0, "AAPL": 2.0},
        ),
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "12345678-01",
        {
            "account": {
                "account_id": "12345678-01",
                "cash": 1020.0,
                "cash_by_currency": {"KRW": 1000.0, "USD": 20.0},
                "positions": [
                    {
                        "symbol": "005930",
                        "name": "삼성전자",
                        "quantity": 1,
                        "current_price": 1000.0,
                        "currency": "KRW",
                    },
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "quantity": 2,
                        "current_price": 50.0,
                        "currency": "USD",
                    },
                ],
            },
            "current_prices": {"005930": 1000.0, "AAPL": 50.0},
        },
    )

    assert router.process_update(message_update("/portfolio"))

    text = client.sent_messages[-1]["text"]
    assert "CASH" in text
    assert "- KRW: 1,000" in text
    assert "- USD: 20" in text
    assert "005930 삼성전자: 1 @ 1,000 KRW = 1,000 KRW" in text
    assert "AAPL: 2 @ 50 USD = 100 USD" in text
    assert "AAPL Apple Inc." not in text


def test_telegram_operator_approvals_displays_notional_currency_breakdown(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    store.save_approval(
        "run_approval",
        "appr_1",
        {
            "request": {
                "order_count": 2,
                "estimated_notional": 1900180.5,
                "proposed_orders": [
                    {"symbol": "133690", "notional": 1_900_000.0, "currency": "KRW"},
                    {"symbol": "QLD", "notional": 180.5, "currency": "USD"},
                ],
            },
            "decision": {"status": "approved", "decided_by": "telegram:operator"},
        },
    )

    assert router.process_update(message_update("/approvals"))

    text = client.sent_messages[-1]["text"]
    assert "notional=1,900,000.00 KRW, 180.50 USD" in text
    assert "1,900,180.50" not in text


def test_telegram_operator_enforces_chat_and_user_whitelist(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )

    assert router.process_update(message_update("/status", chat_id=999, user_id=100))
    assert router.process_update(message_update("/status", chat_id=100, user_id=999))

    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "Unauthorized Telegram user."
    events = store.list_system_events_by_type("telegram_command", limit=2)
    assert {event["payload"]["status"] for event in events} == {"denied_chat", "denied_user"}


def test_telegram_operator_pause_and_kill_switch_require_confirmation(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )

    assert router.process_update(message_update("/pause"))
    assert client.sent_messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        "operator:confirm:pause"
    )
    assert SafetyControlService(store, audit).current_state().state == SafetyState.ACTIVE

    assert router.process_update(callback_update("operator:confirm:pause"))
    assert SafetyControlService(store, audit).current_state().state == SafetyState.PAUSED
    assert client.edited_messages[-1]["text"].startswith("Safety state changed: paused")

    assert router.process_update(message_update("/kill_switch"))
    assert client.sent_messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        "operator:confirm:kill-switch"
    )
    assert router.process_update(callback_update("operator:confirm:kill-switch"))
    assert SafetyControlService(store, audit).current_state().state == SafetyState.KILLED

    statuses = [
        event["payload"]["status"]
        for event in store.list_system_events_by_type("telegram_command", limit=10)
    ]
    assert "handled" in statuses
    assert "confirmed" in statuses


def test_telegram_operator_poll_once_routes_updates(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient(updates=[message_update("/status", update_id=5)])
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )

    next_offset = router.poll_once(offset=None, timeout_seconds=0)

    assert next_offset == 6
    assert client.update_requests[0]["allowed_updates"] == ["message", "callback_query"]
    assert client.sent_messages[-1]["text"].startswith("Maestro status")


def test_telegram_bot_commands_cover_operator_commands():
    commands = telegram_bot_commands()

    assert {"command": "status", "description": "Show Maestro status summary"} in commands
    assert {"command": "health", "description": "Show health checks"} in commands
    assert {
        "command": "kill_switch",
        "description": "Confirm emergency live execution stop",
    } in commands
    assert all(not item["command"].startswith("/") for item in commands)


def test_telegram_set_commands_cli_registers_bot_commands(
    tmp_path,
    monkeypatch,
):
    config_path = _telegram_config_path(tmp_path)
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    result = CliRunner().invoke(
        app,
        ["telegram-set-commands", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert "telegram_set_commands status=ok commands=" in result.output
    assert fake_clients[0].registered_commands == telegram_bot_commands()


def test_telegram_operator_cli_rejects_placeholder_chat_ids(tmp_path):
    config_path = _telegram_config_path(tmp_path)
    raw = yaml.safe_load(config_path.read_text())
    raw["approval"]["telegram_allowed_chat_ids"] = [123456789]
    raw["approval"]["whitelisted_user_ids"] = [123456789]
    config_path.write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(
        app,
        ["telegram-operator", "--config", str(config_path), "--once"],
    )

    assert result.exit_code != 0
    assert "replace placeholder 123456789" in result.output


class FakeTelegramClient:
    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self.updates = list(updates or [])
        self.update_requests: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.registered_commands: list[dict[str, str]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        self.update_requests.append(
            {
                "offset": offset,
                "timeout_seconds": timeout_seconds,
                "allowed_updates": allowed_updates,
            }
        )
        return {"ok": True, "result": self.updates}

    def answer_callback_query(self, callback_query_id: str, text: str) -> dict[str, Any]:
        self.answered_callbacks.append({"callback_query_id": callback_query_id, "text": text})
        return {"ok": True}

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
        return {"ok": True}

    def set_my_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        self.registered_commands = list(commands)
        return {"ok": True, "result": True}


def message_update(
    text: str,
    *,
    update_id: int = 1,
    chat_id: int = 100,
    user_id: int = 100,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": "operator"},
            "text": text,
        },
    }


def callback_update(
    data: str,
    *,
    update_id: int = 2,
    chat_id: int = 100,
    user_id: int = 100,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "data": data,
            "message": {
                "chat": {"id": chat_id},
                "message_id": 10,
                "text": "Confirm operator command",
            },
            "from": {"id": user_id, "username": "operator"},
        },
    }


def _telegram_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
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


def _telegram_live_readonly_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/examples/live_readonly_mock.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    config_path = tmp_path / "telegram_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path
