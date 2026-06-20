from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.broker import BrokerAccountConfig
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import SafetyState
from maestro.dashboard.actions import build_signal_freshness
from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerPosition,
    BrokerReadOnlySnapshot,
)
from maestro.integrations.telegram.handlers import (
    TelegramOperatorCommandRouter,
    telegram_bot_commands,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.safety.controls import SafetyControlService
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class _TelegramStaticStrategy(BaseStrategyPlugin):
    strategy_id = "base"

    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id=self.strategy_id,
            name=self.strategy_id,
            version="0.1.0",
            description="Telegram operator signal test strategy.",
            supported_modes=["paper", "live_approval"],
            supported_asset_types=["cash", "etf"],
            result_type="target_allocation",
            requires_data=["price"],
            can_run_live=True,
        )

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        return [
            DataRequest(
                symbol=symbol,
                asset_type="cash" if symbol == "CASH" else "etf",
                data_type="price",
            )
            for symbol in context.config.get("allocations", {"CASH": 1.0})
        ]

    def run(self, data_bundle: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        return TargetAllocationResult(
            strategy_id=self.strategy_id,
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            allocations=context.config.get("allocations", {"CASH": 1.0}),
            confidence=1.0,
            time_horizon="telegram-test",
            rationale="Telegram operator signal test allocation.",
        )


class BuyOnlyFundingTelegramStrategy(_TelegramStaticStrategy):
    strategy_id = "tranquillo"

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        del context
        return [
            DataRequest(symbol="MOCK_ETF_A", asset_type="etf", data_type="price"),
            DataRequest(symbol="MOCK_ETF_B", asset_type="etf", data_type="price"),
        ]

    def run(self, data_bundle: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        del data_bundle
        return TargetAllocationResult(
            strategy_id=context.strategy_id,
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            allocations={},
            allocation_sleeves={"KRW": {"MOCK_ETF_A": 0.6, "MOCK_ETF_B": 0.4}},
            confidence=1.0,
            time_horizon="telegram-test",
            rationale="Telegram funding retry target.",
        )


class TranquilloTelegramSignalStrategy(_TelegramStaticStrategy):
    strategy_id = "tranquillo"


class CrescendoTelegramSignalStrategy(_TelegramStaticStrategy):
    strategy_id = "crescendo_us"




def test_telegram_operator_signal_command_generates_strategy_signal_for_dashboard(tmp_path):
    readonly_config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_signal_config_path(tmp_path)
    config = load_config(readonly_config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=signal_config_path,
    )

    assert router.process_update(message_update("/signal_tranquillo"))

    text = client.sent_messages[-1]["text"]
    assert text.startswith("Signal generated")
    assert "strategy: Tranquillo" in text
    assert "strategy_id: tranquillo" not in text
    assert "signal_run_id:" in text
    assert "loaded_strategies: Tranquillo" in text
    assert "orders_preview_count:" in text
    signal_run_id = text.split("signal_run_id: ", 1)[1].splitlines()[0]
    signal = store.load_signal_package(signal_run_id)
    assert signal is not None
    assert signal["loaded_strategies"] == ["tranquillo"]
    counts = store.status()["counts"]
    assert counts["approvals"] == 0
    assert counts["orders"] == 0
    freshness = build_signal_freshness(
        store,
        max_age_seconds=config.approval.signal_max_age_seconds,
    )
    assert freshness["strategies"][0]["strategy_id"] == "tranquillo"
    assert freshness["strategies"][0]["latest_signal_run_id"] == signal_run_id


def test_telegram_operator_signal_command_rejects_internal_strategy_id_alias(tmp_path):
    readonly_config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_signal_config_path(tmp_path)
    config = load_config(readonly_config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=signal_config_path,
    )

    assert router.process_update(message_update("/signal_crescendo_us"))

    text = client.sent_messages[-1]["text"]
    assert "Signal generation failed" in text
    assert "Unknown signal command: /signal_crescendo_us" in text


def test_telegram_operator_signal_command_rejects_signal_disabled_strategy(tmp_path):
    readonly_config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_signal_config_path(tmp_path)
    raw = yaml.safe_load(signal_config_path.read_text())
    raw["strategies"].append(
        {
            **raw["strategies"][0],
            "id": "fugue",
            "signal_enabled": False,
            "entrypoint": "missing.strategy:MissingStrategy",
        }
    )
    signal_config_path.write_text(yaml.safe_dump(raw))
    config = load_config(readonly_config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=signal_config_path,
    )

    assert router.process_update(message_update("/signal_fugue"))

    assert "Signal generation failed" in client.sent_messages[-1]["text"]
    assert "Strategy is not signal-enabled: fugue" in client.sent_messages[-1]["text"]


def test_telegram_operator_funding_complete_regenerates_signal_and_creates_approval(tmp_path):
    readonly_config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_buy_only_config_path(
        tmp_path,
        filename="funding_signal.yaml",
        provider="telegram",
        require_approval=False,
    )
    approval_config_path = _telegram_buy_only_config_path(
        tmp_path,
        filename="funding_approval.yaml",
        provider="console",
        require_approval=True,
    )
    config = load_config(readonly_config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "signal_old",
        "contribution_funding_request",
        {
            "request_id": "fund_req_1",
            "source_signal_run_id": "signal_old",
            "strategy_ids": ["tranquillo"],
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "currency": "KRW",
            "available_cash": 1_000_000.0,
            "min_monthly_budget": 2_000_000.0,
            "required_shortfall": 1_000_000.0,
            "month_key": "2026-05",
            "status": "pending",
        },
    )
    store.save_portfolio_snapshot(
        "manual_deposit",
        PortfolioState(cash=3_000_000, cash_by_currency={"KRW": 3_000_000}, positions={}),
    )
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=signal_config_path,
        approval_config_path=approval_config_path,
    )

    assert router.process_update(callback_update("operator:funding:complete:fund_req_1"))

    assert client.answered_callbacks[-1]["text"] == "Funding request confirmed."
    assert "Funding confirmed" in client.edited_messages[-1]["text"]
    assert "new_signal_run_id:" in client.edited_messages[-1]["text"]
    assert "approval_status: approved" in client.edited_messages[-1]["text"]
    ack_events = store.list_system_events_by_type("contribution_funding_request_ack", limit=10)
    assert ack_events[0]["payload"]["request_id"] == "fund_req_1"
    assert ack_events[0]["payload"]["status"] == "confirmed"
    assert store.status()["counts"]["approvals"] == 1
    assert store.status()["counts"]["orders"] == 2
    cash_flow_events = store.list_system_events_by_type("strategy_cash_flow", limit=10)
    assert cash_flow_events[0]["payload"]["strategy_id"] == "tranquillo"
    assert cash_flow_events[0]["payload"]["amount"] == 1_000_000.0
    assert cash_flow_events[0]["payload"]["currency"] == "KRW"
    assert cash_flow_events[0]["payload"]["flow_type"] == "deposit"
    assert cash_flow_events[0]["payload"]["source"] == "telegram_funding_confirmation"


def test_telegram_operator_funding_complete_fails_when_readonly_refresh_fails(
    tmp_path,
    monkeypatch,
):
    readonly_config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_buy_only_config_path(
        tmp_path,
        filename="funding_signal_refresh_failure.yaml",
        provider="telegram",
        require_approval=False,
    )
    config = _multi_readonly_config(load_config(readonly_config_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "signal_old",
        "contribution_funding_request",
        {
            "request_id": "fund_req_refresh_failure",
            "source_signal_run_id": "signal_old",
            "strategy_ids": ["tranquillo"],
            "account_id": "toss_brokerage",
            "execution_sleeve": "krw_contribution",
            "currency": "KRW",
            "available_cash": 1_000_000.0,
            "min_monthly_budget": 2_000_000.0,
            "required_shortfall": 1_000_000.0,
            "month_key": "2026-05",
            "status": "pending",
        },
    )
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=signal_config_path,
    )

    def fail_refresh():
        raise ValueError("Toss OpenAPI request failed: HTTP 500")

    monkeypatch.setattr(router, "_refresh_portfolio_from_broker_snapshot", fail_refresh)

    assert router.process_update(
        callback_update("operator:funding:complete:fund_req_refresh_failure")
    )

    assert client.answered_callbacks[-1]["text"] == "Funding request confirmed."
    assert "Funding confirmation failed" in client.edited_messages[-1]["text"]
    assert "Toss OpenAPI request failed: HTTP 500" in client.edited_messages[-1]["text"]
    ack_events = store.list_system_events_by_type("contribution_funding_request_ack", limit=10)
    assert ack_events == []


def test_telegram_operator_account_detects_voluntary_deposit_and_approves_target_split(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "run_old_cash",
        "paper_cash",
        {
            "account": {
                "account_id": "paper_cash",
                "currency": "KRW",
                "cash": 1_000_000.0,
                "total_value": 1_000_000.0,
                "positions": [],
                "source": "fixture",
            }
        },
    )
    store.save_broker_account_snapshot(
        "run_new_cash",
        "paper_cash",
        {
            "account": {
                "account_id": "paper_cash",
                "currency": "KRW",
                "cash": 2_000_000.0,
                "total_value": 2_000_000.0,
                "positions": [],
                "source": "fixture",
            }
        },
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(message_update("/account"))

    proposal_text = client.sent_messages[-1]["text"]
    assert proposal_text.startswith("Unattributed deposit detected")
    assert "Tranquillo: 600,000.00 KRW" in proposal_text
    assert "Crescendo: 400,000.00 KRW" in proposal_text
    proposal_events = store.list_system_events_by_type("strategy_cash_flow_proposal", limit=10)
    proposal_id = proposal_events[0]["payload"]["proposal_id"]

    assert router.process_update(callback_update(f"operator:cash-flow:approve:{proposal_id}"))

    assert client.answered_callbacks[-1]["text"] == "Cash-flow allocation approved."
    assert "Strategy cash-flow allocation recorded" in client.edited_messages[-1]["text"]
    cash_flow_events = store.list_system_events_by_type("strategy_cash_flow", limit=10)
    amounts_by_strategy = {
        event["payload"]["strategy_id"]: event["payload"]["amount"]
        for event in cash_flow_events
    }
    assert amounts_by_strategy == {"tranquillo": 600_000.0, "crescendo_us": 400_000.0}
    ack_events = store.list_system_events_by_type("strategy_cash_flow_proposal_ack", limit=10)
    assert ack_events[0]["payload"]["status"] == "approved"


def test_telegram_operator_account_assigns_voluntary_deposit_to_one_strategy(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "run_old_cash",
        "paper_cash",
        {
            "account": {
                "account_id": "paper_cash",
                "currency": "KRW",
                "cash": 1_000_000.0,
                "total_value": 1_000_000.0,
                "positions": [],
                "source": "fixture",
            }
        },
    )
    store.save_broker_account_snapshot(
        "run_new_cash",
        "paper_cash",
        {
            "account": {
                "account_id": "paper_cash",
                "currency": "KRW",
                "cash": 2_000_000.0,
                "total_value": 2_000_000.0,
                "positions": [],
                "source": "fixture",
            }
        },
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(message_update("/account"))

    proposal_events = store.list_system_events_by_type("strategy_cash_flow_proposal", limit=10)
    proposal_id = proposal_events[0]["payload"]["proposal_id"]
    markup = client.sent_messages[-1]["reply_markup"]
    callback_buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert any(button["text"] == "Assign Crescendo" for button in callback_buttons)

    callback_data = f"operator:cash-flow:assign:{proposal_id}:crescendo_us"
    assert router.process_update(callback_update(callback_data))

    assert client.answered_callbacks[-1]["text"] == "Cash-flow allocation assigned."
    cash_flow_events = store.list_system_events_by_type("strategy_cash_flow", limit=10)
    assert len(cash_flow_events) == 1
    payload = cash_flow_events[0]["payload"]
    assert payload["strategy_id"] == "crescendo_us"
    assert payload["amount"] == 1_000_000.0
    assert payload["execution_sleeve"] == "satellite"
    ack_events = store.list_system_events_by_type("strategy_cash_flow_proposal_ack", limit=10)
    assert ack_events[0]["payload"]["status"] == "assigned"
    assert ack_events[0]["payload"]["assigned_strategy_id"] == "crescendo_us"


def test_telegram_operator_funding_callback_rejects_unauthorized_user(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "signal_old",
        "contribution_funding_request",
        {"request_id": "fund_req_1", "status": "pending", "strategy_ids": ["tranquillo"]},
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(
        callback_update("operator:funding:complete:fund_req_1", user_id=999)
    )

    assert client.answered_callbacks[-1]["text"] == "Unauthorized Telegram user."
    assert store.list_system_events_by_type("contribution_funding_request_ack", limit=10) == []


def test_telegram_operator_budget_callback_records_selected_budget(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "signal_old",
        "contribution_budget_request",
        {
            "request_id": "budget_req_1",
            "source_signal_run_id": "signal_old",
            "strategy_ids": ["tranquillo"],
            "account_id": "kis_isa",
            "execution_sleeve": "tranquillo_isa",
            "currency": "KRW",
            "available_cash": 8_000_000.0,
            "min_monthly_budget": 1_660_000.0,
            "recommended_budget": 4_000_000.0,
            "selectable_max_budget": 8_000_000.0,
            "month_key": "2026-06",
            "status": "pending",
        },
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(callback_update("operator:budget:select:budget_req_1:full"))

    assert client.answered_callbacks[-1]["text"] == "Budget selected."
    events = store.list_system_events_by_type("contribution_budget_request_decision", limit=10)
    assert events[0]["payload"]["request_id"] == "budget_req_1"
    assert events[0]["payload"]["selected_budget"] == 8_000_000.0
    assert events[0]["payload"]["status"] == "selected"


def test_telegram_operator_budget_direct_amount_rejects_out_of_range(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "signal_old",
        "contribution_budget_request",
        {
            "request_id": "budget_req_2",
            "source_signal_run_id": "signal_old",
            "strategy_ids": ["tranquillo"],
            "account_id": "kis_isa",
            "execution_sleeve": "tranquillo_isa",
            "currency": "KRW",
            "available_cash": 8_000_000.0,
            "min_monthly_budget": 1_660_000.0,
            "recommended_budget": 4_000_000.0,
            "selectable_max_budget": 8_000_000.0,
            "month_key": "2026-06",
            "status": "pending",
        },
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(message_update("/budget budget_req_2 9000000"))

    assert "Budget amount out of range" in client.sent_messages[-1]["text"]
    assert (
        store.list_system_events_by_type("contribution_budget_request_decision", limit=10) == []
    )


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
    store.save_signal_package(
        "signal_abc",
        {
            "status": "action_required",
            "action_required": True,
            "orders_preview_count": 2,
            "loaded_strategies": ["tranquillo"],
            "datahub_evidence": {"issue_count": 0},
        },
    )

    for command in (
        "/help",
        "/status",
        "/health",
        "/signal",
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
    assert "Latest signal" in sent_text
    assert "signal_run_id: signal_abc" in sent_text
    assert "Maestro portfolio" in sent_text
    assert "Maestro apps" in sent_text
    assert "Recent orders" in sent_text
    assert "Recent approvals" in sent_text
    assert len(store.list_system_events_by_type("telegram_command", limit=20)) == 8


def test_telegram_operator_apps_uses_current_virtuoso_strategy_names(tmp_path):
    config_path = _telegram_signal_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )

    assert router.process_update(message_update("/apps"))

    text = client.sent_messages[-1]["text"]
    assert "Tranquillo: on signal:on" in text
    assert "Crescendo: on signal:on" in text
    assert "tranquillo: on" not in text
    assert "crescendo_us: on" not in text


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
    assert "created_at: " in text
    assert "KST" in text
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


def test_telegram_operator_account_displays_multi_account_currency_breakdowns(tmp_path):
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
        "run_kis",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "broker_account_id": "MOCK-KIS",
            "account": {
                "account_id": "MOCK-KIS",
                "cash": 1_000_000.0,
                "cash_by_currency": {"KRW": 1_000_000.0},
                "positions": [
                    {
                        "symbol": "MOCK_ETF_A",
                        "quantity": 10,
                        "current_price": 100.0,
                        "currency": "KRW",
                    }
                ],
                "source": "kis_mock",
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_toss",
        "toss_brokerage",
        {
            "account_id": "toss_brokerage",
            "broker_account_id": "12345678901",
            "account": {
                "account_id": "12345678901",
                "cash": 20.0,
                "cash_by_currency": {"USD": 20.0},
                "positions": [
                    {
                        "symbol": "MOCK_ETF_B",
                        "quantity": 2,
                        "current_price": 50.0,
                        "currency": "USD",
                    }
                ],
                "source": "toss_openapi_readonly",
            },
        },
    )

    assert router.process_update(message_update("/account"))

    text = client.sent_messages[-1]["text"]
    assert "account_id: multiple" in text
    assert "cash: 1,000,000.00 KRW, 20.00 USD" in text
    assert "positions_market_value: 1,000.00 KRW, 100.00 USD" in text
    assert "positions: 2" in text
    assert "source: broker_account_aggregate" in text


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

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.build_broker_readonly_services",
        lambda config, store, audit: [
            ("kis_mock", FailingReadOnlyService("KIS request failed with HTTP 500"))
        ],
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


def test_telegram_operator_portfolio_refreshes_all_readonly_accounts(
    tmp_path,
    monkeypatch,
):
    config = _multi_readonly_config(load_config(_telegram_config_path(tmp_path)))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    services = [
        (
            "kis_mock",
            StaticReadOnlyService(
                BrokerReadOnlySnapshot(
                    account=BrokerAccountSnapshot(
                        account_id="MOCK-KIS",
                        cash=1_000_000.0,
                        cash_by_currency={"KRW": 1_000_000.0},
                        buying_power=1_000_000.0,
                        positions=[
                            BrokerPosition(
                                symbol="MOCK_ETF_A",
                                quantity=10.0,
                                average_price=100.0,
                                current_price=100.0,
                                currency="KRW",
                            )
                        ],
                        fetched_at=utc_now(),
                        source="kis_mock",
                    ),
                    current_prices={"MOCK_ETF_A": 100.0},
                ),
            ),
        ),
        (
            "toss_brokerage",
            StaticReadOnlyService(
                BrokerReadOnlySnapshot(
                    account=BrokerAccountSnapshot(
                        account_id="12345678901",
                        cash=20.0,
                        cash_by_currency={"USD": 20.0},
                        buying_power=20.0,
                        positions=[
                            BrokerPosition(
                                symbol="MOCK_ETF_B",
                                quantity=2.0,
                                average_price=50.0,
                                current_price=50.0,
                                currency="USD",
                            )
                        ],
                        fetched_at=utc_now(),
                        source="toss_openapi_readonly",
                    ),
                    current_prices={"MOCK_ETF_B": 50.0},
                ),
            ),
        ),
    ]
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.build_broker_readonly_services",
        lambda config, store, audit: services,
    )

    assert router.process_update(message_update("/portfolio"))

    assert client.sent_messages[-2]["text"] == (
        "Maestro portfolio: refreshing from broker snapshot"
    )
    state = store.load_latest_portfolio_state()
    assert state.cash_by_currency == {"KRW": 1_000_000.0, "USD": 20.0}
    assert state.positions == {"MOCK_ETF_A": 10.0, "MOCK_ETF_B": 2.0}
    account_states = {
        row["account_id"]: row["payload"]
        for row in store.list_portfolio_snapshots(limit=10)
        if row.get("account_id")
    }
    assert set(account_states) == {"kis_mock", "toss_brokerage"}
    assert account_states["kis_mock"]["positions"] == {"MOCK_ETF_A": 10.0}
    assert account_states["toss_brokerage"]["positions"] == {"MOCK_ETF_B": 2.0}


def test_telegram_operator_portfolio_can_include_unknown_readonly_positions(
    tmp_path,
    monkeypatch,
):
    config = _multi_readonly_config(load_config(_telegram_config_path(tmp_path)))
    config = config.model_copy(
        update={
            "portfolio": config.portfolio.model_copy(
                update={"unknown_broker_position_policy": "include_readonly"}
            )
        }
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
    )
    services = [
        (
            "toss_brokerage",
            StaticReadOnlyService(
                BrokerReadOnlySnapshot(
                    account=BrokerAccountSnapshot(
                        account_id="12345678901",
                        cash=20.0,
                        cash_by_currency={"USD": 20.0},
                        buying_power=20.0,
                        positions=[
                            BrokerPosition(
                                symbol="UNKNOWN_TOSS",
                                quantity=2.0,
                                average_price=50.0,
                                current_price=55.0,
                                currency="USD",
                            )
                        ],
                        fetched_at=utc_now(),
                        source="toss_openapi_readonly",
                    ),
                    current_prices={"UNKNOWN_TOSS": 55.0},
                ),
            ),
        )
    ]
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.build_broker_readonly_services",
        lambda config, store, audit: services,
    )

    assert router.process_update(message_update("/portfolio"))

    state = store.load_latest_portfolio_state()
    assert state.cash_by_currency == {"USD": 20.0}
    assert state.positions == {"UNKNOWN_TOSS": 2.0}
    text = client.sent_messages[-1]["text"]
    assert "UNKNOWN_TOSS: 2" in text


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


def test_telegram_operator_portfolio_does_not_let_zero_price_override_account_position_price(
    tmp_path,
):
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
        "run_portfolio",
        PortfolioState(
            cash=0.0,
            cash_by_currency={"USD": 0.0},
            positions={"GOOG": 2.0},
        ),
    )
    store.save_broker_account_snapshot(
        "run_kis",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "account": {
                "account_id": "MOCK-KIS",
                "cash": 0.0,
                "positions": [],
                "source": "kis_mock",
            },
            "current_prices": {"GOOG": 0.0},
        },
    )
    store.save_broker_account_snapshot(
        "run_toss",
        "toss_brokerage",
        {
            "account_id": "toss_brokerage",
            "account": {
                "account_id": "12345678901",
                "cash": 0.0,
                "positions": [
                    {
                        "symbol": "GOOG",
                        "quantity": 2.0,
                        "current_price": 360.0,
                        "currency": "USD",
                    }
                ],
                "source": "toss_openapi_readonly",
            },
            "current_prices": {"GOOG": 361.0},
        },
    )

    assert router.process_update(message_update("/portfolio"))

    text = client.sent_messages[-1]["text"]
    assert "GOOG: 2 @ 361 USD = 722 USD" in text
    assert "GOOG: 2 @ 0 USD = 0 USD" not in text


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


def test_telegram_operator_adopts_account_attribution_once(tmp_path):
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
    AccountAttributionReconciliationService(store, audit).reconcile_broker_snapshot(
        run_id="run_sync",
        account_id="toss_brokerage",
        broker_snapshot_id=10,
        broker_positions={"QQQ": 2.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )

    assert router.process_update(message_update("/attribution toss_brokerage"))
    callback_data = client.sent_messages[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    assert callback_data == "operator:attribution:approve:toss_brokerage"
    assert router.process_update(callback_update(callback_data))
    assert (
        store.load_latest_system_event("account_attribution_adopted")["payload"]["approved"]
        is True
    )

    assert router.process_update(callback_update(callback_data))
    assert "already adopted" in client.answered_callbacks[-1]["text"]


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
    signal_config_path = _telegram_signal_config_path(tmp_path)
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.delenv("MAESTRO_SIGNAL_CONFIG", raising=False)
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    result = CliRunner().invoke(
        app,
        [
            "telegram-set-commands",
            "--config",
            str(config_path),
            "--signal-config",
            str(signal_config_path),
        ],
    )

    assert result.exit_code == 0
    assert "telegram_set_commands status=ok commands=" in result.output
    assert fake_clients[0].registered_commands == telegram_bot_commands(
        load_config(signal_config_path)
    )


def test_telegram_operator_cli_passes_signal_config_to_router(tmp_path, monkeypatch):
    config_path = _telegram_config_path(tmp_path)
    signal_config_path = _telegram_signal_config_path(tmp_path)
    captured: dict[str, Any] = {}

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        return FakeTelegramClient()

    class FakeRouter:
        def __init__(self, *, signal_config_path=None, **kwargs):
            captured["signal_config_path"] = signal_config_path

        def poll_once(self, *, offset=None, timeout_seconds=0):
            captured["timeout_seconds"] = timeout_seconds
            return offset

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)
    monkeypatch.setattr("maestro.cli.TelegramOperatorCommandRouter", FakeRouter)

    result = CliRunner().invoke(
        app,
        [
            "telegram-operator",
            "--config",
            str(config_path),
            "--signal-config",
            str(signal_config_path),
            "--once",
            "--timeout-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0
    assert captured["signal_config_path"] == signal_config_path
    assert captured["timeout_seconds"] == 0


def test_telegram_bot_commands_include_signal_generation_commands(tmp_path):
    signal_config = load_config(_telegram_signal_config_path(tmp_path))

    commands = telegram_bot_commands(signal_config)

    assert {"command": "signal_tranquillo", "description": "Generate Tranquillo signal"} in commands
    assert {"command": "signal_crescendo", "description": "Generate Crescendo signal"} in commands
    assert {
        "command": "signal_crescendo_us",
        "description": "Generate Crescendo signal",
    } not in commands


def test_telegram_bot_commands_do_not_expose_legacy_signal_aliases(tmp_path):
    config_path = _telegram_signal_config_path(tmp_path)
    raw = yaml.safe_load(config_path.read_text())
    raw["strategies"][0]["id"] = "ataraxia"
    raw["strategies"][1]["id"] = "snowball_us"
    config_path.write_text(yaml.safe_dump(raw))
    signal_config = load_config(config_path)

    commands = telegram_bot_commands(signal_config)

    assert {"command": "signal_tranquillo", "description": "Generate Tranquillo signal"} in commands
    assert {"command": "signal_crescendo", "description": "Generate Crescendo signal"} in commands
    assert {
        "command": "signal_ataraxia",
        "description": "Generate Tranquillo signal",
    } not in commands
    assert {
        "command": "signal_snowball_us",
        "description": "Generate Crescendo signal",
    } not in commands


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


class StaticReadOnlyService:
    def __init__(self, snapshot: BrokerReadOnlySnapshot) -> None:
        self.snapshot = snapshot

    def fetch_and_store_snapshot(
        self,
        symbols: list[str],
        run_id: str | None = None,
    ) -> BrokerReadOnlySnapshot:
        del symbols, run_id
        return self.snapshot


class FailingReadOnlyService:
    def __init__(self, message: str) -> None:
        self.message = message

    def fetch_and_store_snapshot(self, symbols: list[str], run_id: str | None = None):
        del symbols, run_id
        raise ValueError(self.message)


def _multi_readonly_config(config):
    return config.model_copy(
        update={
            "accounts": [
                BrokerAccountConfig(
                    id="kis_mock",
                    broker="kis",
                    environment="paper_trading",
                    enabled=True,
                    provider="mock",
                    account_id="MOCK-KIS",
                    broker_products=["kis_domestic_stock"],
                ),
                BrokerAccountConfig(
                    id="toss_brokerage",
                    broker="toss",
                    environment="real",
                    enabled=True,
                    account_seq=1,
                    client_id_env="TOSS_CLIENT_ID",
                    client_secret_env="TOSS_CLIENT_SECRET",
                ),
            ]
        }
    )


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


def _telegram_signal_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": False,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    raw["strategies"][0]["id"] = "tranquillo"
    raw["strategies"][0]["entrypoint"] = f"{__name__}:TranquilloTelegramSignalStrategy"
    raw["strategies"].append(
        {
            **raw["strategies"][0],
            "id": "crescendo_us",
            "entrypoint": f"{__name__}:CrescendoTelegramSignalStrategy",
            "config": {"allocations": {"CASH": 0.4, "MOCK_ETF_A": 0.1, "MOCK_ETF_B": 0.5}},
        }
    )
    config_path = tmp_path / "telegram_signal.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _telegram_buy_only_config_path(
    tmp_path,
    *,
    filename: str,
    provider: str,
    require_approval: bool,
) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": provider,
        "require_approval": require_approval,
        "default_decision": "approved",
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["strategies"] = [
        {
            "id": "tranquillo",
            "enabled": True,
            "signal_enabled": True,
            "weight": 1.0,
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "order_posture": "dry_run",
            "entrypoint": f"{__name__}:BuyOnlyFundingTelegramStrategy",
            "config": {},
        }
    ]
    raw["execution"]["order_posture"] = "dry_run"
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "holidays": [],
    }
    raw["execution"]["live_order_limits"] = {"fee_buffer_pct": 0.0}
    raw["accounts"] = [
        {
            "id": "paper_cash",
            "broker": "sandbox",
            "environment": "paper_trading",
            "enabled": True,
        }
    ]
    raw["execution_sleeves"] = {
        "accounts": {
            "paper_cash": {
                "krw_contribution": {
                    "currency_sleeve": "KRW",
                    "target_weight": 1.0,
                    "order_generation_mode": "buy_only_contribution",
                    "contribution": {
                        "enabled": True,
                        "currency": "KRW",
                        "sleeve": "KRW",
                        "monthly_budget": 3_000_000,
                        "min_monthly_budget": 2_000_000,
                        "max_monthly_budget": 4_000_000,
                        "buy_day": 1,
                        "non_trading_day_policy": "next_trading_day",
                        "target_policy": "buy_only_toward_target",
                        "funding_request": {"enabled": True},
                    },
                }
            }
        }
    }
    config_path = tmp_path / filename
    config_path.write_text(yaml.safe_dump(raw))
    return config_path



def _telegram_voluntary_deposit_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    raw["state"]["sqlite_path"] = str(tmp_path / "voluntary_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "voluntary_audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    raw["accounts"] = [
        {
            "id": "paper_cash",
            "broker": "sandbox",
            "environment": "paper_trading",
            "enabled": True,
        }
    ]
    raw["strategies"] = [
        {
            "id": "tranquillo",
            "enabled": True,
            "signal_enabled": True,
            "weight": 0.6,
            "account_id": "paper_cash",
            "execution_sleeve": "core",
            "entrypoint": f"{__name__}:TranquilloTelegramSignalStrategy",
            "config": {},
        },
        {
            "id": "crescendo_us",
            "enabled": True,
            "signal_enabled": True,
            "weight": 0.4,
            "account_id": "paper_cash",
            "execution_sleeve": "satellite",
            "entrypoint": f"{__name__}:CrescendoTelegramSignalStrategy",
            "config": {},
        },
    ]
    raw["execution_sleeves"] = {
        "accounts": {
            "paper_cash": {
                "core": {
                    "currency_sleeve": "KRW",
                    "target_weight": 0.6,
                    "order_generation_mode": "target_rebalance",
                },
                "satellite": {
                    "currency_sleeve": "KRW",
                    "target_weight": 0.4,
                    "order_generation_mode": "target_rebalance",
                },
            }
        }
    }
    config_path = tmp_path / "telegram_voluntary_deposit.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path

def _telegram_live_readonly_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_readonly_mock.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    config_path = tmp_path / "telegram_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path
