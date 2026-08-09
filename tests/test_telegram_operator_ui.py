from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from typer.testing import CliRunner

from maestro.approval.models import ApprovalRequest, PendingApprovalEnvelope
from maestro.cli import app
from maestro.config.broker import BrokerAccountConfig
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode, SafetyState
from maestro.core.ids import new_run_id
from maestro.dashboard.actions import build_signal_freshness
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerBuyingPower,
    BrokerPosition,
    BrokerReadOnlySnapshot,
)
from maestro.execution.order_capacity import OrderCapacityService
from maestro.integrations.telegram.handlers import (
    TelegramOperatorCommandRouter,
    _quantity_step,
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


def _rebalance_router(tmp_path):
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
    return router, client, store


def test_telegram_rebalance_command_requests_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MAESTRO_REBALANCE_UNITS",
        "tranquillo=maestro-symphony-signal-kr.service,"
        "crescendo_us=maestro-symphony-signal-us.service",
    )
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(message_update("/rebalance_tranquillo"))

    message = client.sent_messages[-1]
    assert "Confirm manual rebalance for Tranquillo." in message["text"]
    assert "unit: maestro-symphony-signal-kr.service" in message["text"]
    callbacks = [
        button["callback_data"]
        for row in message["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "operator:rebalance:approve:tranquillo" in callbacks
    assert "operator:cancel" in callbacks


def test_telegram_rebalance_command_fails_without_unit_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("MAESTRO_REBALANCE_UNITS", raising=False)
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(message_update("/rebalance_tranquillo"))

    text = client.sent_messages[-1]["text"]
    assert "Manual rebalance failed" in text
    assert "MAESTRO_REBALANCE_UNITS" in text


def test_telegram_rebalance_command_rejects_unknown_strategy(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_REBALANCE_UNITS", "tranquillo=unit-a.service")
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(message_update("/rebalance_unknown"))

    text = client.sent_messages[-1]["text"]
    assert "Manual rebalance failed" in text
    assert "Unknown rebalance command: /rebalance_unknown" in text


def test_telegram_rebalance_callback_starts_mapped_systemd_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_REBALANCE_UNITS", "crescendo_us=maestro-symphony-signal-us.service")
    started_units = []
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers._start_systemd_unit",
        started_units.append,
    )
    router, client, store = _rebalance_router(tmp_path)

    assert router.process_update(callback_update("operator:rebalance:approve:crescendo_us"))

    assert started_units == ["maestro-symphony-signal-us.service"]
    assert client.answered_callbacks[-1]["text"] == "Manual rebalance triggered."
    edited = client.edited_messages[-1]["text"]
    assert "Manual rebalance triggered: Crescendo" in edited
    assert "unit: maestro-symphony-signal-us.service" in edited
    recorded = store.list_system_events_by_type("telegram_command")[0]["payload"]
    assert recorded["command"] == "/rebalance"
    assert recorded["status"] == "confirmed"


def test_telegram_rebalance_callback_reports_systemd_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_REBALANCE_UNITS", "tranquillo=maestro-symphony-signal-kr.service")

    def failing_start(unit):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers._start_systemd_unit",
        failing_start,
    )
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(callback_update("operator:rebalance:approve:tranquillo"))

    assert "Manual rebalance failed: Tranquillo" in client.edited_messages[-1]["text"]


def test_telegram_rebalance_callback_without_unit_mapping_fails_safely(tmp_path, monkeypatch):
    monkeypatch.delenv("MAESTRO_REBALANCE_UNITS", raising=False)
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(callback_update("operator:rebalance:approve:tranquillo"))

    assert "no systemd unit mapped for tranquillo" in client.edited_messages[-1]["text"]


def test_telegram_rebalance_usage_lists_available_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_REBALANCE_UNITS", "tranquillo=unit-a.service")
    router, client, _ = _rebalance_router(tmp_path)

    assert router.process_update(message_update("/rebalance"))

    text = client.sent_messages[-1]["text"]
    assert "Manual rebalance commands" in text
    assert "/rebalance_tranquillo - Tranquillo" in text
    assert "/rebalance_crescendo - Crescendo" in text


def test_telegram_bot_commands_include_rebalance_commands(tmp_path):
    signal_config = load_config(_telegram_signal_config_path(tmp_path))

    commands = {item["command"] for item in telegram_bot_commands(signal_config)}

    assert "rebalance_tranquillo" in commands
    assert "rebalance_crescendo" in commands
    assert "signal_tranquillo" in commands


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


def _save_broker_reported_cash_window(store, account_id, baseline, changed):
    """Baseline plus three snapshots holding the new level.

    Candidate detection now requires the same evidence from every broker: the
    new level has to persist rather than appear once, so a fixture that flips
    the balance in a single step no longer describes an offerable candidate.
    """
    for run_id, cash in [
        ("run_old_cash", baseline),
        ("run_new_cash_1", changed),
        ("run_new_cash_2", changed),
        ("run_new_cash_3", changed),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            account_id,
            {
                "account_id": account_id,
                "account": {
                    "account_id": account_id,
                    "currency": "KRW",
                    "cash": cash,
                    "cash_by_currency": {"KRW": cash},
                    "total_value": cash,
                    "positions": [],
                    "source": "fixture",
                },
                "unfilled_orders": [],
                "order_fills": [],
            },
        )


def test_telegram_operator_account_detects_voluntary_deposit_and_approves_target_split(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_broker_reported_cash_window(store, "paper_cash", 1_000_000.0, 2_000_000.0)
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
        event["payload"]["strategy_id"]: event["payload"]["amount"] for event in cash_flow_events
    }
    assert amounts_by_strategy == {"tranquillo": 600_000.0, "crescendo_us": 400_000.0}
    account_flows = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert account_flows[0]["payload"]["account_id"] == "paper_cash"
    assert account_flows[0]["payload"]["amount"] == 1_000_000.0
    assert {event["payload"]["account_cash_flow_id"] for event in cash_flow_events} == {
        account_flows[0]["run_id"]
    }
    ack_events = store.list_system_events_by_type("strategy_cash_flow_proposal_ack", limit=10)
    assert ack_events[0]["payload"]["status"] == "approved"


def test_telegram_operator_confirms_stable_toss_cash_flow_candidate_once(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_portfolio_snapshot(
        "ledger_baseline",
        PortfolioState(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        account_id="paper_cash",
    )
    for run_id, buying_power in [
        ("baseline", 1_000_000.0),
        ("changed-1", 2_000_000.0),
        ("changed-2", 2_000_000.0),
        ("changed-3", 2_000_000.0),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            "paper_cash",
            {
                "account_id": "paper_cash",
                "account": {
                    "account_id": "paper_cash",
                    "source": "toss_openapi_readonly",
                    "cash": buying_power,
                    "cash_by_currency": {"KRW": buying_power},
                    "buying_power_by_currency": {"KRW": buying_power},
                    "ledger_cash_by_currency": None,
                    "positions": [],
                },
                "unfilled_orders": [],
                "order_fills": [],
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
    assert client.sent_messages[-1]["text"].startswith("Maestro cash-flow candidate")
    proposal = store.list_system_events_by_type("account_cash_flow_proposal", limit=1)[0]["payload"]

    assert router.process_update(
        callback_update(f"operator:cash-flow:confirm:{proposal['proposal_id']}")
    )
    flow = store.list_system_events_by_type("account_cash_flow", limit=1)[0]["payload"]
    assert flow["amount"] == 1_000_000.0
    assert flow["verification"] == "operator_verified"
    assert flow["evidence"]["kind"] == "stable_toss_buying_power_change"
    state = store.load_latest_account_portfolio_state("paper_cash")
    assert state is not None
    assert state.cash_by_currency["KRW"] == 2_000_000.0

    assert router.process_update(
        callback_update(f"operator:cash-flow:confirm:{proposal['proposal_id']}")
    )
    assert len(store.list_system_events_by_type("account_cash_flow", limit=10)) == 1


def test_telegram_operator_proactively_confirms_toss_fx_conversion_once(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_portfolio_snapshot(
        "ledger_baseline",
        PortfolioState(
            cash=1_350_090.0,
            cash_by_currency={"KRW": 1_350_000.0, "USD": 90.0},
        ),
        account_id="paper_cash",
    )
    for run_id, balances in [
        ("baseline", {"KRW": 1_400_000.0, "USD": 100.0}),
        ("changed-1", {"KRW": 100_000.0, "USD": 1_100.0}),
        ("changed-2", {"KRW": 100_000.0, "USD": 1_100.0}),
        ("changed-3", {"KRW": 100_000.0, "USD": 1_100.0}),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            "paper_cash",
            {
                "account_id": "paper_cash",
                "account": {
                    "account_id": "paper_cash",
                    "source": "toss_openapi_readonly",
                    "cash": balances["KRW"],
                    "cash_by_currency": balances,
                    "buying_power_by_currency": balances,
                    "ledger_cash_by_currency": None,
                    "positions": [],
                },
                "unfilled_orders": [],
                "order_fills": [],
            },
        )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    router.notify_pending_cash_flows()

    assert client.sent_messages[-1]["text"].startswith("Maestro currency-conversion candidate")
    proposal = store.list_system_events_by_type("account_cash_flow_proposal", limit=1)[0]["payload"]
    assert proposal["source"] == "toss_buying_power_fx_conversion_candidate"
    assert proposal["observed_from_amount"] == 1_300_000.0
    assert proposal["observed_to_amount"] == 1_000.0
    assert proposal["from_amount"] == 1_250_000.0
    assert proposal["to_amount"] == 1_010.0
    assert "Maestro ledger adjustment" in client.sent_messages[-1]["text"]

    assert router.process_update(
        callback_update(f"operator:cash-flow:confirm:{proposal['proposal_id']}")
    )

    flows = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert len(flows) == 2
    assert {row["payload"]["flow_class"] for row in flows} == {"fx_conversion"}
    state = store.load_latest_account_portfolio_state("paper_cash")
    assert state is not None
    assert state.cash_by_currency == {"KRW": 100_000.0, "USD": 1_100.0}
    assert client.answered_callbacks[-1]["text"] == "Currency conversion recorded."

    assert router.process_update(
        callback_update(f"operator:cash-flow:confirm:{proposal['proposal_id']}")
    )
    assert len(store.list_system_events_by_type("account_cash_flow", limit=10)) == 2


def test_telegram_operator_warns_once_for_an_unreconciled_live_fill(
    tmp_path,
    monkeypatch,
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
    candidate = {
        "broker_order_id": "toss-order-1234567890",
        "account_id": "toss_brokerage",
        "symbol": "QQQM",
        "side": "sell",
        "filled_quantity": 22.0,
        "applied_quantity": 0.0,
        "missing_quantity": 22.0,
        "missing_notional": 6316.20,
        "first_observed_at": "2026-08-04T00:00:00+00:00",
        "age_seconds": 901.0,
    }
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.list_unreconciled_live_order_fills",
        lambda store: [candidate],
    )

    router._notify_unreconciled_live_order_fills()
    router._notify_unreconciled_live_order_fills()

    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"].startswith("Maestro unreconciled fill warning")
    assert "QQQM" in client.sent_messages[0]["text"]
    notices = store.list_system_events_by_type("telegram_unreconciled_fill_notice", limit=10)
    assert len(notices) == 1


def test_telegram_operator_account_assigns_voluntary_deposit_to_one_strategy(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_broker_reported_cash_window(store, "paper_cash", 1_000_000.0, 2_000_000.0)
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
    assign_button = next(
        button for button in callback_buttons if button["text"] == "Assign Crescendo"
    )
    assert assign_button["callback_data"] == f"operator:cash-flow:asg:{proposal_id}:1"
    for button in callback_buttons:
        assert len(button["callback_data"].encode("utf-8")) <= 64

    assert router.process_update(callback_update(assign_button["callback_data"]))

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


def test_telegram_operator_confirms_unexplained_withdrawal_as_account_flow(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_broker_reported_cash_window(store, "paper_cash", 2_000_000.0, 1_000_000.0)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(message_update("/account"))
    proposal = store.list_system_events_by_type("account_cash_flow_proposal", limit=1)[0]["payload"]
    assert proposal["flow_type"] == "withdrawal"

    assert router.process_update(
        callback_update(f"operator:cash-flow:approve:{proposal['proposal_id']}")
    )

    account_flow = store.list_system_events_by_type("account_cash_flow", limit=1)[0]["payload"]
    assert account_flow["amount"] == -1_000_000.0
    assert account_flow["flow_type"] == "withdrawal"


def test_telegram_operator_accepts_legacy_strategy_id_assign_callback(tmp_path):
    config = load_config(_telegram_voluntary_deposit_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "run_pending_proposal",
        "strategy_cash_flow_proposal",
        {
            "proposal_id": "run_pending_proposal",
            "status": "pending",
            "account_id": "paper_cash",
            "amount": 1_000_000.0,
            "currency": "KRW",
            "allocations": [
                {"strategy_id": "tranquillo", "execution_sleeve": "core", "amount": 600_000.0},
                {
                    "strategy_id": "crescendo_us",
                    "execution_sleeve": "satellite",
                    "amount": 400_000.0,
                },
            ],
        },
    )
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    callback_data = "operator:cash-flow:assign:run_pending_proposal:crescendo_us"
    assert router.process_update(callback_update(callback_data))

    assert client.answered_callbacks[-1]["text"] == "Cash-flow allocation assigned."
    ack_events = store.list_system_events_by_type("strategy_cash_flow_proposal_ack", limit=10)
    assert ack_events[0]["payload"]["assigned_strategy_id"] == "crescendo_us"


def test_telegram_operator_processes_callback_when_answer_fails(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    class StaleAnswerTelegramClient(FakeTelegramClient):
        def answer_callback_query(self, callback_query_id: str, text: str) -> dict[str, Any]:
            raise RuntimeError("Bad Request: query is too old")

    client = StaleAnswerTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.process_update(callback_update("operator:confirm:pause"))

    assert "Safety state changed" in client.edited_messages[-1]["text"]


def test_telegram_operator_poll_once_advances_offset_past_failing_update(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    class FlakySendTelegramClient(FakeTelegramClient):
        def __init__(self, updates: list[dict[str, Any]]) -> None:
            super().__init__(updates)
            self.send_failures_remaining = 1

        def send_message(
            self,
            chat_id: int,
            text: str,
            reply_markup: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if self.send_failures_remaining > 0:
                self.send_failures_remaining -= 1
                raise RuntimeError("Telegram Bot API returned not ok for method: sendMessage")
            return super().send_message(chat_id, text, reply_markup)

    client = FlakySendTelegramClient(
        [
            message_update("/help", update_id=7),
            message_update("/help", update_id=8),
        ]
    )
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )

    assert router.poll_once(offset=None, timeout_seconds=0) == 9

    assert len(client.sent_messages) == 1
    error_events = [
        row
        for row in store.list_system_events_by_type("telegram_command", limit=10)
        if row["payload"].get("status") == "error"
    ]
    assert len(error_events) == 1
    assert error_events[0]["payload"]["update_id"] == 7


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

    # New short-form callback as generated by budget_request_reply_markup.
    assert router.process_update(callback_update("operator:budget:sel:budget_req_1:f"))

    assert client.answered_callbacks[-1]["text"] == "Budget selected."
    events = store.list_system_events_by_type("contribution_budget_request_decision", limit=10)
    assert events[0]["payload"]["request_id"] == "budget_req_1"
    assert events[0]["payload"]["selected_budget"] == 8_000_000.0
    assert events[0]["payload"]["status"] == "selected"


def test_telegram_operator_budget_callback_accepts_legacy_select_format(tmp_path):
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
    assert events[0]["payload"]["selected_budget"] == 8_000_000.0


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
    assert store.list_system_events_by_type("contribution_budget_request_decision", limit=10) == []


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


def test_telegram_operator_currency_breakdown_excludes_disabled_account(tmp_path):
    """Regression test: a disabled account's stale snapshot must not be
    folded into the /status command's broker cash/exposure totals.

    Broker snapshots are never deleted, so a disabled/retired account (e.g.
    a mock account left over from setup) keeps contributing its last
    snapshot forever unless explicitly filtered out. This mirrors the
    dashboard's kis_mock incident (a disabled mock account's stale snapshot
    silently inflating Total Asset), applied to the Telegram bot's own
    parallel currency-breakdown aggregate instead. Note: production tags
    a KIS account's snapshot with the raw broker account number as the
    primary `account_id` (see `_broker_snapshot_account_id`'s DB-column
    priority) and the config's logical id only in the nested payload — this
    fixture mirrors that shape rather than using the logical id directly.
    """
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
    raw["accounts"] = [
        {
            "id": "kis_ps",
            "broker": "kis",
            "enabled": True,
            "broker_products": ["kis_domestic_stock"],
        },
        {
            "id": "kis_mock",
            "broker": "kis",
            "enabled": False,
            "broker_products": ["kis_domestic_stock"],
        },
    ]
    config_path = tmp_path / "telegram_currency_breakdown.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(config=config, store=store, audit=audit, client=client)
    # Disabled account: a stale snapshot from a retired mock account.
    store.save_broker_account_snapshot(
        "run_stale",
        "50186608-01",
        {
            "account_id": "kis_mock",
            "account": {
                "account_id": "50186608-01",
                "currency": "KRW",
                "cash": 10_000_000.0,
                "positions": [],
            },
        },
    )
    # Enabled account: the real, currently-active trading account.
    store.save_broker_account_snapshot(
        "run_active",
        "44667023-22",
        {
            "account_id": "kis_ps",
            "account": {
                "account_id": "44667023-22",
                "currency": "KRW",
                "cash": 1_000_000.0,
                "positions": [],
            },
        },
    )

    assert router.process_update(message_update("/status"))

    text = client.sent_messages[-1]["text"]
    assert "- total_value: 1,000,000.00 KRW" in text
    assert "- cash: 1,000,000.00 KRW" in text
    assert "11,000,000.00" not in text


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


def test_telegram_operator_account_returns_stored_snapshot_without_refresh(tmp_path):
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

    text = client.sent_messages[-1]["text"]
    assert "Broker account snapshot (stored)" in text
    assert "total_value: 12,345,678.00 unknown" in text
    assert "orderable_cash:" not in text
    assert "source: stale_fixture" in text
    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    assert latest["payload"]["account"]["source"] == "stale_fixture"


def test_telegram_operator_account_does_not_call_broker_implicitly(
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

    text = client.sent_messages[-1]["text"]
    assert "Broker account snapshot (stored)" in text
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


def test_telegram_clear_halt_requires_confirmation_and_recovers(tmp_path):
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
    safety = SafetyControlService(store, audit)
    safety.halt(new_run_id(), "test halt for telegram recovery")

    assert router.process_update(message_update("/clear_halt"))
    confirm_message = client.sent_messages[-1]
    assert "Confirm clear-halt" in confirm_message["text"]
    assert "halt reason: test halt for telegram recovery" in confirm_message["text"]
    assert confirm_message["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        "operator:confirm:clear-halt"
    )
    assert safety.current_state().state == SafetyState.HALTED

    assert router.process_update(callback_update("operator:confirm:clear-halt"))

    assert safety.current_state().state == SafetyState.ACTIVE
    assert client.edited_messages[-1]["text"].startswith("Safety state changed: active")
    statuses = [
        event["payload"]["status"]
        for event in store.list_system_events_by_type("telegram_command", limit=10)
    ]
    assert "confirmed" in statuses


def test_telegram_recovery_center_shows_halt_and_live_order_actions(tmp_path):
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
    SafetyControlService(store, audit).halt(new_run_id(), "test halt")
    store.save_system_event(
        "run_ambiguous",
        "live_order_recovery_required",
        {"reason": "ambiguous_submit", "order_id": "ord_ambiguous"},
    )

    assert router.process_update(message_update("/recovery"))

    message = client.sent_messages[-1]
    assert "Maestro Recovery Center" in message["text"]
    assert "health:" in message["text"]
    assert "broker_snapshot:" in message["text"]
    assert "reconciliation:" in message["text"]
    assert "live_order_blockers: 1" in message["text"]
    buttons = [row[0] for row in message["reply_markup"]["inline_keyboard"]]
    assert [button["text"] for button in buttons] == [
        "주문 상태 확인 및 복구",
        "Safety halt 해제",
    ]


def test_telegram_recovery_center_links_retryable_orders(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    order = _pending_approval_envelope().orders[0]
    store.save_order("run_rejected", order["order_id"], order)
    store.save_system_event(
        "run_rejected",
        "live_order_recovery_candidate",
        {
            "source_order_id": order["order_id"],
            "order": order,
            "source_type": "definitive_rejection",
            "reason": "prerequisite-required",
            "created_at": utc_now().isoformat(),
        },
    )

    assert router.process_update(message_update("/recovery"))

    message = client.sent_messages[-1]
    assert "retryable_orders: 1" in message["text"]
    button = message["reply_markup"]["inline_keyboard"][0][0]
    assert button == {
        "text": "재주문 검토 보기",
        "callback_data": "operator:wfrec:orders",
    }

    assert router.process_update(callback_update("operator:wfrec:orders"))
    assert "Recoverable orders" in client.sent_messages[-1]["text"]


def test_telegram_recovery_callback_requests_broker_attestation(monkeypatch, tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.WorkflowRecoveryService",
        lambda *args, **kwargs: SimpleNamespace(
            recover_live_orders=lambda **kwargs: SimpleNamespace(
                status="attestation_required",
                fingerprint="0123456789abcdef",
                unmatched_orders=[
                    {
                        "order_id": "ord_ambiguous",
                        "account_id": "toss_brokerage",
                        "candidate_orders": [],
                    }
                ],
            )
        ),
    )

    assert router.process_update(callback_update("operator:wfrec:auto:0123456789abcdef"))

    edited = client.edited_messages[-1]
    assert "Automatic order matching was inconclusive" in edited["text"]
    button = edited["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "브로커에서 미접수·미체결 확인 후 해제"
    assert button["callback_data"] == "operator:wfrec:attest:0123456789abcdef"


def test_telegram_recovery_notification_is_idempotent(tmp_path):
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
    SafetyControlService(store, audit).halt(new_run_id(), "notification halt")

    router.poll_once()
    router.poll_once()

    notices = [
        message for message in client.sent_messages if "Maestro Recovery Center" in message["text"]
    ]
    assert len(notices) == 1
    assert len(store.list_system_events_by_type("telegram_recovery_notice")) == 1


def test_telegram_clear_halt_rejected_when_not_halted(tmp_path):
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

    assert router.process_update(message_update("/clear_halt"))

    message = client.sent_messages[-1]
    assert "Safety state is active; clear-halt only applies to halted." in message["text"]
    assert message["reply_markup"] is None


def test_telegram_clear_halt_callback_cannot_release_kill_switch(tmp_path):
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
    safety = SafetyControlService(store, audit)
    safety.kill_switch(new_run_id(), "emergency stop")

    assert router.process_update(callback_update("operator:confirm:clear-halt"))

    assert safety.current_state().state == SafetyState.KILLED
    assert "Clear-halt failed:" in client.edited_messages[-1]["text"]


def test_telegram_clear_halt_callback_blocked_by_failing_preflight(tmp_path, monkeypatch):
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
    safety = SafetyControlService(store, audit)
    safety.halt(new_run_id(), "test halt")
    store.save_system_event(new_run_id(), "broker_reconciliation", {"passed": False})

    assert router.process_update(callback_update("operator:confirm:clear-halt"))

    assert safety.current_state().state == SafetyState.HALTED
    edited = client.edited_messages[-1]["text"]
    assert "Clear-halt blocked by failing health checks:" in edited
    assert "reconciliation" in edited


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
        store.load_latest_system_event("account_attribution_adopted")["payload"]["approved"] is True
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


def test_retry_order_rejection_ack_prevents_duplicate_callback(tmp_path):
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
    proposal_id = "run_retry_1"
    store.save_system_event(
        proposal_id,
        "live_order_retry_proposal",
        {
            "proposal_id": proposal_id,
            "blocked_order_id": "ord_blocked_1",
            "request": {"run_id": proposal_id},
            "status": "pending",
        },
    )
    update = callback_update(f"operator:retry-order:reject:{proposal_id}")

    assert router.process_update(update)
    assert router.process_update(update)

    acknowledgements = store.list_system_events_by_type("live_order_retry_proposal_ack")
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["payload"]["status"] == "rejected"
    assert client.answered_callbacks[-1]["text"] == "This retry proposal is no longer active."


def test_async_approval_rejection_is_persisted_once(tmp_path):
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
    envelope = _pending_approval_envelope()
    store.save_signal_package(envelope.signal_run_id, {"orders_preview": envelope.orders})
    store.save_system_event(
        envelope.run_id,
        "telegram_approval_pending",
        envelope.model_dump(mode="json"),
    )
    update = callback_update(f"operator:appr:r:{envelope.approval_id}")

    assert router.process_update(update)
    assert router.process_update(update)

    assert len(store.list_approvals(limit=10)) == 1
    acknowledgements = store.list_system_events_by_type("telegram_approval_ack")
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["payload"]["status"] == "rejected"
    assert client.answered_callbacks[-1]["text"] == ("This approval request is no longer active.")


def test_async_approval_reminders_are_sent_once(monkeypatch, tmp_path):
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
    envelope = _pending_approval_envelope(reminder_seconds=[120, 300, 480])
    store.save_system_event(
        envelope.run_id,
        "telegram_approval_pending",
        envelope.model_dump(mode="json"),
    )

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.utc_now",
        lambda: envelope.created_at + timedelta(seconds=121),
    )
    router.poll_once(timeout_seconds=0)
    router.poll_once(timeout_seconds=0)
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.utc_now",
        lambda: envelope.created_at + timedelta(seconds=301),
    )
    router.poll_once(timeout_seconds=0)

    reminders = store.list_system_events_by_type("telegram_approval_reminder")
    assert sorted(row["payload"]["reminder_seconds"] for row in reminders) == [120, 300]
    assert len([item for item in client.sent_messages if "Approval reminder" in item["text"]]) == 2


def test_expired_contribution_order_is_recoverable_but_open_order_is_not(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
    )
    month = utc_now().astimezone().strftime("%Y-%m")
    expired_order = _pending_approval_envelope().orders[0]
    expired_order["metadata"] = {
        "order_generation_mode": "buy_only_contribution",
        "contribution_month": month,
    }
    store.save_order(
        "run_expired",
        expired_order["order_id"],
        {
            **expired_order,
            "signal_run_id": "signal_expired",
            "approval_status": "expired",
        },
    )
    open_order = {**expired_order, "order_id": "ord_open_1"}
    store.save_order(
        "run_open",
        open_order["order_id"],
        {**open_order, "approval_status": "approved"},
    )
    store.save_system_event(
        "run_open",
        "live_order_lifecycle",
        {
            "run_id": "run_open",
            "order_id": open_order["order_id"],
            "final_status": "open",
            "broker_order_id": "broker-open-1",
            "checked_at": utc_now().isoformat(),
        },
    )

    candidate = router._pending_recovery_candidate(expired_order["order_id"])

    assert candidate is not None
    assert candidate.source_type == "approval_expired"
    assert router._pending_recovery_candidate(open_order["order_id"]) is None


def test_orders_adds_recovery_review_button(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    order = _store_recoverable_order(store)

    router._orders(100)

    markup = client.sent_messages[-1]["reply_markup"]
    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "재주문 검토 · MOCK_ETF_A"
    assert button["callback_data"] == f"operator:recover:review:{order.order_id}"
    assert len(button["callback_data"]) <= 64


def test_recovery_review_shows_original_max_and_direct_input(monkeypatch, tmp_path):
    config = load_config(_telegram_config_path(tmp_path)).model_copy(
        update={"mode": RunMode.LIVE_APPROVAL}
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    order = _store_recoverable_order(store, quantity=38)
    monkeypatch.setattr(router, "_lookup_retry_price", lambda config, order: 13_140.0)
    monkeypatch.setattr(
        router,
        "_lookup_retry_capacity",
        lambda config, order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=1_000_000,
            max_buy_quantity=31,
            source="test",
        ),
    )

    assert router.process_update(callback_update(f"operator:recover:review:{order.order_id}"))

    message = client.sent_messages[-1]
    assert "original_quantity: 38" in message["text"]
    assert "current_max_quantity: 31" in message["text"]
    buttons = [row[0] for row in message["reply_markup"]["inline_keyboard"]]
    assert [button["text"] for button in buttons] == [
        "원 수량 38",
        "현재 최대 31",
        "직접 수량 입력",
    ]
    assert all(len(button["callback_data"]) <= 64 for button in buttons)


def test_retry_quote_is_normalized_to_instrument_tick(monkeypatch, tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
    )
    order = _store_recoverable_order(store).model_copy(update={"symbol": "PDBC"})
    retry_config = SimpleNamespace(
        universe=SimpleNamespace(
            get=lambda symbol: SimpleNamespace(price_tick=0.01) if symbol == "PDBC" else None
        )
    )
    readonly_service = SimpleNamespace(
        client=SimpleNamespace(get_current_prices=lambda symbols: {"PDBC": 17.465})
    )
    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.build_broker_readonly_service",
        lambda *args, **kwargs: readonly_service,
    )

    assert router._lookup_retry_price(retry_config, order) == 17.46


def test_direct_quantity_reply_creates_one_retry_proposal(monkeypatch, tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    order = _store_recoverable_order(store, quantity=38)
    candidate = router._pending_recovery_candidate(order.order_id)
    monkeypatch.setattr(
        router,
        "_retry_order_review",
        lambda order_id: (candidate, order, 13_140.0, 31.0),
    )
    proposed: list[tuple[str, float]] = []

    def propose(order_id, quantity, chat_id, *, price=None):
        del chat_id, price
        proposed.append((order_id, quantity))
        return "run_retry_direct"

    monkeypatch.setattr(router, "_propose_retry_order", propose)
    assert router.process_update(callback_update(f"operator:recover:input:{order.order_id}"))
    prompt_message_id = client.sent_messages[-1]["reply_markup"]
    assert prompt_message_id["force_reply"] is True
    reply = message_update("29")
    reply["message"]["reply_to_message"] = {"message_id": len(client.sent_messages)}

    assert router.process_update(reply)
    assert router.process_update(reply) is False

    assert proposed == [(order.order_id, 29.0)]
    acknowledgements = store.list_system_events_by_type("live_order_retry_quantity_prompt_ack")
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["payload"]["status"] == "consumed"


def test_rejected_retry_proposal_allows_another_recovery_review(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
    )
    order = _store_recoverable_order(store)
    proposal_id = "run_retry_rejected"
    store.save_system_event(
        proposal_id,
        "live_order_retry_proposal",
        {
            "proposal_id": proposal_id,
            "blocked_order_id": order.order_id,
            "expires_at": (utc_now() + timedelta(minutes=10)).isoformat(),
        },
    )
    assert router._pending_recovery_candidate(order.order_id) is None
    store.save_system_event(
        proposal_id,
        "live_order_retry_proposal_ack",
        {
            "proposal_id": proposal_id,
            "blocked_order_id": order.order_id,
            "status": "rejected",
        },
    )

    assert router._pending_recovery_candidate(order.order_id) is not None


def test_retry_review_offers_the_same_maximum_the_block_alert_quoted(monkeypatch, tmp_path):
    """A retry the operator is offered must be one the capacity gate accepts.

    Both sides round the affordable quantity down to the instrument's step, so
    a partial share is never quoted as a retryable amount.
    """
    config = load_config(_telegram_config_path(tmp_path)).model_copy(
        update={"mode": RunMode.LIVE_APPROVAL}
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
    )
    order = _store_recoverable_order(store, quantity=38)
    # 250 KRW at 100 KRW a share buys two whole shares, not 2.5.
    capacity = BrokerBuyingPower(
        symbol=order.symbol,
        order_price=100.0,
        cash_buying_power=250.0,
        currency="KRW",
        source="test",
    )
    monkeypatch.setattr(router, "_lookup_retry_price", lambda config, order: 100.0)
    monkeypatch.setattr(router, "_lookup_retry_capacity", lambda config, order: capacity)

    _, _, _, review_maximum = router._retry_order_review(order.order_id)
    _, blocked = OrderCapacityService(
        lambda candidate: capacity,
        quantity_step=lambda candidate: _quantity_step(config, candidate),
    ).partition([order.model_copy(update={"price": 100.0, "notional": 3_800.0})])

    assert review_maximum == 2.0
    assert blocked[0].max_buy_quantity == review_maximum


def _store_recoverable_order(
    store: StateStore,
    *,
    quantity: float = 10,
) -> OrderIntent:
    order = OrderIntent(
        order_id="ord_12345678901234567890123456789012",
        symbol="MOCK_ETF_A",
        side="buy",
        quantity=quantity,
        price=100,
        notional=quantity * 100,
        currency="KRW",
        account_id="paper_cash",
        metadata={
            "order_generation_mode": "buy_only_contribution",
            "contribution_month": utc_now().strftime("%Y-%m"),
        },
    )
    store.save_order(
        "run_recoverable",
        order.order_id,
        {
            **order.model_dump(mode="json"),
            "approval_status": "expired",
            "signal_run_id": "signal_recoverable",
        },
    )
    return order


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


def _pending_approval_envelope(
    *,
    reminder_seconds: list[int] | None = None,
) -> PendingApprovalEnvelope:
    now = utc_now()
    order = OrderIntent(
        order_id="ord_async_1",
        symbol="MOCK_ETF_A",
        side="buy",
        quantity=1,
        price=100,
        notional=100,
        account_id="paper",
    )
    request = ApprovalRequest(
        approval_id="appr_async_1",
        run_id="run_async_1",
        created_at=now,
        expires_at=now + timedelta(seconds=600),
        channel="telegram",
        source_strategy_ids=["tranquillo"],
        order_count=1,
        estimated_notional=100,
        proposed_orders=[order.model_dump(mode="json")],
    )
    return PendingApprovalEnvelope(
        approval_id=request.approval_id,
        run_id=request.run_id,
        signal_run_id="signal_async_1",
        request=request,
        orders=[order.model_dump(mode="json")],
        message="Async approval",
        source_strategy_ids=["tranquillo"],
        account_ids=["paper"],
        reminder_seconds=list(reminder_seconds or []),
        created_at=now,
        expires_at=request.expires_at,
        duplicate_key=f"telegram-approval-pending:{request.approval_id}",
    )


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


def test_dispatch_uses_korean_approval_card():
    from maestro.orchestration import orchestrator as orch

    assert orch.render_approval_card is not None
