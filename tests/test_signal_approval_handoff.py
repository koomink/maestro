import subprocess
from pathlib import Path

import pytest
import yaml
from sample_static_allocation.strategy import SampleStaticAllocationStrategy
from typer.testing import CliRunner

from maestro.approval.models import ApprovalDecision, ApprovalRequest
from maestro.cli import _run_daily_signal_approval, _send_signal_funding_request_notifications, app
from maestro.config.loader import load_config, load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.core.ids import new_run_id
from maestro.execution.brokers.kis.models import KISAccountSnapshot, KISReadOnlySnapshot
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_orders import (
    BrokerOrderId,
    LiveOrderClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationIssue, ReconciliationResult
from maestro.orchestration.orchestrator import (
    MaestroOrchestrator,
    signal_contract_fingerprint_diff,
)
from maestro.safety.controls import SafetyControlService
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
from maestro.state.store import StateStore


class SecondStaticAllocationStrategy(SampleStaticAllocationStrategy):
    def manifest(self):
        return (
            super()
            .manifest()
            .model_copy(update={"strategy_id": "second_static", "name": "Second Static Allocation"})
        )

    def run(self, data_bundle, context):
        return (
            super()
            .run(data_bundle, context)
            .model_copy(update={"strategy_id": context.strategy_id})
        )


class BuyOnlyFundingStrategy(BaseStrategyPlugin):
    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id="buy_only_funding",
            name="Buy Only Funding",
            version="0.1.0",
            description="Funding request test strategy.",
            supported_modes=["paper", "live_approval"],
            supported_asset_types=["cash", "etf"],
            result_type="target_allocation",
            requires_data=["price"],
            can_run_live=True,
        )

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
            strategy_version="0.1.0",
            timestamp=context.timestamp,
            allocations={},
            allocation_sleeves={"KRW": {"MOCK_ETF_A": 0.6, "MOCK_ETF_B": 0.4}},
            confidence=1.0,
            time_horizon="monthly",
            rationale="Buy-only contribution funding request test target.",
        )


def test_run_signal_persists_immutable_signal_package_without_approval(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    orchestrator = MaestroOrchestrator(config)

    summary = orchestrator.run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    assert summary.action_required is True
    assert summary.orders_preview_count == 2
    assert signal["signal_run_id"] == summary.signal_run_id
    assert signal["status"] == "action_required"
    assert signal["orders_preview_count"] == 2
    assert signal["approval_consumed"] is False
    assert store.list_approvals() == []
    assert store.list_orders() == []


def test_run_signal_persists_funding_request_when_buy_only_cash_is_below_minimum(tmp_path):
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=True)

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["buy_only_funding"])

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    assert summary.action_required is False
    assert summary.orders_preview_count == 0
    assert signal["status"] == "funding_required"
    assert signal["funding_requests_count"] == 1
    request = signal["funding_requests"][0]
    assert request["source_signal_run_id"] == summary.signal_run_id
    assert request["strategy_ids"] == ["buy_only_funding"]
    assert request["account_id"] == "paper_cash"
    assert request["execution_sleeve"] == "krw_contribution"
    assert request["currency"] == "KRW"
    assert request["available_cash"] == 1_000_000
    assert request["min_monthly_budget"] == 2_000_000
    assert request["required_shortfall"] == 1_000_000

    events = store.list_system_events_by_type("contribution_funding_request", limit=10)
    assert len(events) == 1
    assert events[0]["payload"]["request_id"] == request["request_id"]
    assert events[0]["payload"]["status"] == "pending"


def test_signal_funding_request_notification_sends_telegram_message(
    monkeypatch,
    tmp_path,
):
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=True)
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["buy_only_funding"])
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    sent = _send_signal_funding_request_notifications(config, summary.signal_run_id)

    assert sent == 1
    assert fake_clients[0].sent_messages[0]["chat_id"] == 100
    assert "Maestro funding request" in fake_clients[0].sent_messages[0]["text"]
    assert "shortfall: 1,000,000 KRW" in fake_clients[0].sent_messages[0]["text"]
    keyboard = fake_clients[0].sent_messages[0]["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["text"] == "입금 완료"


def test_run_signal_keeps_no_action_when_funding_request_opt_in_is_disabled(tmp_path):
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=False)

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["buy_only_funding"])

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    assert summary.action_required is False
    assert signal["status"] == "no_action"
    assert signal["funding_requests_count"] == 0
    assert store.list_system_events_by_type("contribution_funding_request", limit=10) == []


def test_approve_signal_uses_saved_package_without_rerunning_strategies(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    strategy_runs_before = store.status()["counts"]["strategy_runs"]

    approval_summary = MaestroOrchestrator(config).approve_signal(
        signal_summary.signal_run_id,
    )

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    assert approval_summary.signal_run_id == signal_summary.signal_run_id
    assert approval_summary.orders_created == 2
    assert store.status()["counts"]["strategy_runs"] == strategy_runs_before
    assert store.status()["counts"]["approvals"] == 1
    assert store.status()["counts"]["orders"] == 2
    assert signal["approval_consumed"] is True
    assert signal["approval_run_id"] == approval_summary.run_id


def test_approve_signal_creates_strategy_grouped_approval_requests(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    config.strategies[0].account_id = "account_a"
    second_strategy = config.strategies[0].model_copy(
        update={
            "id": "second_static",
            "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
            "account_id": "account_b",
            "weight": 1.0,
            "config": {"allocations": {"MOCK_ETF_B": 1.0}},
        }
    )
    config.strategies.append(second_strategy)
    signal_summary = MaestroOrchestrator(config).run_signal()

    approval_summary = MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    approvals = store.list_approvals(limit=10)
    source_groups = sorted(
        tuple(row["payload"]["request"]["source_strategy_ids"]) for row in approvals
    )
    assert approval_summary.orders_created == 3
    assert approval_summary.approval_status == "approved"
    assert len(approvals) == 2
    assert source_groups == [
        ("sample_static_allocation",),
        ("second_static",),
    ]


def test_approve_signal_propagates_signal_run_id_to_live_order_events(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()

    approval_summary = MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    events = store.list_system_events_by_type("live_order_dry_run", limit=10)
    assert events
    assert {event["payload"]["signal_run_id"] for event in events} == {signal_summary.signal_run_id}
    assert {event["payload"]["request"]["signal_run_id"] for event in events} == {
        signal_summary.signal_run_id
    }
    assert approval_summary.orders_created == 2


def test_approve_signal_retry_uses_stable_duplicate_key(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    config.execution.order_posture = "armed"
    config.execution.live_order_enabled = True
    config.execution.live_order_dry_run = False
    config.strategies[0].order_posture = "armed"
    config.strategies[0].config["allocations"] = {"CASH": 0.7, "MOCK_ETF_A": 0.3}
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event("run_reconcile", "broker_reconciliation", {"passed": True})
    live_client = CountingLiveOrderClient()
    first = MaestroOrchestrator(
        config,
        live_order_client=live_client,
        live_order_status_client=FilledStatusClient(),
        broker_reconciliation_service=PassingBrokerReconciliation(),
    )
    first.approval_manager = ApprovingTelegramApprovalManager()
    first.state_store.mark_signal_package_consumed = lambda signal_run_id, run_id: None

    first.approve_signal(signal_summary.signal_run_id)

    assert live_client.submit_count == 1
    second = MaestroOrchestrator(
        config,
        live_order_client=live_client,
        live_order_status_client=FilledStatusClient(),
        broker_reconciliation_service=PassingBrokerReconciliation(),
    )
    second.approval_manager = ApprovingTelegramApprovalManager()
    second.approve_signal(signal_summary.signal_run_id)

    assert live_client.submit_count == 1
    lifecycle_events = store.list_system_events_by_type("live_order_lifecycle", limit=10)
    assert lifecycle_events[0]["payload"]["final_status"] == "failed"
    assert (
        lifecycle_events[0]["payload"]["failed_reason"] == "Duplicate live order request rejected"
    )


def test_approve_signal_consumes_package_before_approval_request_failure(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    orchestrator = MaestroOrchestrator(config)
    orchestrator.approval_manager = FailingApprovalManager()

    with pytest.raises(RuntimeError, match="approval manager failed"):
        orchestrator.approve_signal(signal_summary.signal_run_id)

    with pytest.raises(ValueError, match="Signal package already consumed"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_excludes_disabled_posture_orders_from_approval(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    config.strategies[0].order_posture = "disabled"
    signal_summary = MaestroOrchestrator(config).run_signal()

    approval_summary = MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert approval_summary.orders_created == 0
    assert store.status()["counts"]["approvals"] == 0
    signal = store.load_signal_package(signal_summary.signal_run_id)
    assert signal["orders_preview_count"] == 2
    assert signal["approval_consumed"] is True


def test_signal_false_strategy_is_not_loaded(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    disabled_signal = config.strategies[0].model_copy(
        update={
            "id": "unimportable_dev_strategy",
            "entrypoint": "missing.strategy:MissingStrategy",
            "signal_enabled": False,
            "order_posture": "disabled",
        }
    )
    config.strategies.append(disabled_signal)

    summary = MaestroOrchestrator(config).run_signal()

    assert summary.loaded_strategies == ["sample_static_allocation"]


def test_run_signal_can_filter_to_one_strategy(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    second_strategy = config.strategies[0].model_copy(
        update={
            "id": "second_static",
            "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
            "weight": 1.0,
            "config": {"allocations": {"MOCK_ETF_B": 1.0}},
        }
    )
    config.strategies.append(second_strategy)

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["second_static"])

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    strategy_runs = store.list_strategy_runs(limit=10)
    assert summary.loaded_strategies == ["second_static"]
    assert signal["loaded_strategies"] == ["second_static"]
    assert [row["strategy_id"] for row in strategy_runs] == ["second_static"]
    assert signal["portfolio_target"]["source_strategy_ids"] == ["second_static"]


def test_approve_signal_rejects_unknown_signal_run_id(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")

    with pytest.raises(ValueError, match="Unknown signal_run_id"):
        MaestroOrchestrator(config).approve_signal("signal_missing")


def test_signal_contract_fingerprint_diff_reports_market_session(tmp_path):
    left_path = _paper_approval_config_path(tmp_path, "approved", filename="left.yaml")
    right_path = _paper_approval_config_path(tmp_path, "approved", filename="right.yaml")
    right_raw = yaml.safe_load(right_path.read_text())
    right_raw["execution"]["market_session"] = {
        "required": True,
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    right_path.write_text(yaml.safe_dump(right_raw))

    diff_keys = signal_contract_fingerprint_diff(load_config(left_path), load_config(right_path))

    assert "execution.market_session" in diff_keys


def test_signal_contract_fingerprint_diff_is_empty_for_identical_config(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")

    assert signal_contract_fingerprint_diff(config, config) == []


def test_profile_diff_cli_reports_signal_contract_fingerprint_changed(tmp_path):
    left_path = _paper_approval_config_path(tmp_path, "approved", filename="left_profile.yaml")
    right_path = _paper_approval_config_path(tmp_path, "approved", filename="right_profile.yaml")

    result = CliRunner().invoke(
        app,
        ["profile-diff", "--left", str(left_path), "--right", str(right_path)],
    )

    assert result.exit_code == 0
    assert "signal_contract_fingerprint_changed=false" in result.output


def test_profile_diff_cli_reports_signal_contract_diff_keys(tmp_path):
    left_path = _paper_approval_config_path(tmp_path, "approved", filename="left_profile.yaml")
    right_path = _paper_approval_config_path(tmp_path, "approved", filename="right_profile.yaml")
    right_raw = yaml.safe_load(right_path.read_text())
    right_raw["execution"]["market_session"] = {
        "required": True,
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    right_path.write_text(yaml.safe_dump(right_raw))

    result = CliRunner().invoke(
        app,
        ["profile-diff", "--left", str(left_path), "--right", str(right_path)],
    )

    assert result.exit_code == 0
    assert "signal_contract_fingerprint_changed=true" in result.output
    assert "signal_contract_diff_keys=execution.market_session" in result.output


def test_daily_signal_approval_preflights_signal_contract_before_signal_run(
    tmp_path,
    monkeypatch,
):
    readonly = _paper_approval_config(tmp_path, "approved")
    signal_path = _paper_approval_config_path(tmp_path, "approved", filename="daily_signal.yaml")
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="daily_approval.yaml",
    )
    approval_raw = yaml.safe_load(approval_path.read_text())
    approval_raw["execution"]["market_session"] = {
        "required": True,
        "timezone": "America/New_York",
        "open": "09:30",
        "close": "16:00",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    approval_path.write_text(yaml.safe_dump(approval_raw))
    signal = load_config(signal_path)
    approval = load_config(approval_path)
    created_orchestrators = []

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            created_orchestrators.append((args, kwargs))

        def run_signal(self):
            raise AssertionError("run_signal should not execute on contract mismatch")

    configs = [readonly, signal, approval]

    def fake_load_operator_config(path):
        del path
        config = configs.pop(0)
        return config, None

    monkeypatch.setattr("maestro.cli._load_operator_config", fake_load_operator_config)
    monkeypatch.setattr("maestro.cli._refresh_daily_readonly", lambda config, identity: None)
    monkeypatch.setattr("maestro.cli.MaestroOrchestrator", FakeOrchestrator)

    with pytest.raises(ValueError, match="signal/approval config contract mismatch"):
        _run_daily_signal_approval(
            readonly_config=Path("readonly.yaml"),
            signal_config=Path("signal.yaml"),
            approval_config=Path("approval.yaml"),
            stop_telegram_operator=False,
            telegram_operator_service="maestro-telegram-operator.service",
        )

    assert created_orchestrators == []


def test_run_signal_and_approve_signal_cli(tmp_path):
    config_path = _paper_approval_config_path(tmp_path, "approved")

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])

    assert signal_result.exit_code == 0
    assert "signal_run_id=" in signal_result.output
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]

    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
            "--keep-telegram-operator",
        ],
    )

    assert approval_result.exit_code == 0
    assert f"signal_run_id={signal_run_id}" in approval_result.output
    assert "orders=2" in approval_result.output


def test_approve_signal_cli_stops_telegram_operator_by_default(tmp_path, monkeypatch):
    config_path = _paper_approval_config_path(tmp_path, "approved")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "maestro.cli._systemctl",
        lambda action, service: calls.append((action, service)),
    )

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]
    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
            "--telegram-operator-service",
            "maestro-test-telegram.service",
        ],
    )

    assert approval_result.exit_code == 0
    assert "orders=2" in approval_result.output
    assert calls == [
        ("stop", "maestro-test-telegram.service"),
        ("start", "maestro-test-telegram.service"),
    ]


def test_approve_signal_cli_keep_telegram_operator_does_not_call_systemctl(
    tmp_path,
    monkeypatch,
):
    config_path = _paper_approval_config_path(tmp_path, "approved")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "maestro.cli._systemctl",
        lambda action, service: calls.append((action, service)),
    )

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]
    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
            "--keep-telegram-operator",
        ],
    )

    assert approval_result.exit_code == 0
    assert "orders=2" in approval_result.output
    assert calls == []


def test_approve_signal_cli_continues_when_telegram_operator_stop_fails(
    tmp_path, monkeypatch
):
    config_path = _paper_approval_config_path(tmp_path, "approved")
    calls: list[tuple[str, str]] = []

    def fail_stop(action: str, service: str) -> None:
        calls.append((action, service))
        if action == "stop":
            raise subprocess.CalledProcessError(returncode=1, cmd=["systemctl", action, service])

    monkeypatch.setattr("maestro.cli._systemctl", fail_stop)

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]
    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
        ],
    )

    assert approval_result.exit_code == 0
    assert "symphony_approve status=warn reason=telegram_operator_stop_failed" in (
        approval_result.output
    )
    assert "orders=2" in approval_result.output
    assert calls == [("stop", "maestro-telegram-operator.service")]


def test_approve_signal_cli_continues_when_systemctl_is_missing(tmp_path, monkeypatch):
    config_path = _paper_approval_config_path(tmp_path, "approved")
    calls: list[tuple[str, str]] = []

    def missing_systemctl(action: str, service: str) -> None:
        calls.append((action, service))
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr("maestro.cli._systemctl", missing_systemctl)

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]
    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
        ],
    )

    assert approval_result.exit_code == 0
    assert "symphony_approve status=warn reason=telegram_operator_stop_failed" in (
        approval_result.output
    )
    assert "orders=2" in approval_result.output
    assert calls == [("stop", "maestro-telegram-operator.service")]


def test_daily_signal_approval_cli_sends_summary_and_approves_actionable_signal(
    tmp_path,
    monkeypatch,
):
    readonly_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="readonly.yaml",
    )
    signal_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="signal.yaml",
        provider="telegram",
        identity_group="daily_signal_approval",
    )
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="approval.yaml",
        provider="console",
        identity_group="daily_signal_approval",
    )
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> "FakeTelegramClient":
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    result = CliRunner().invoke(
        app,
        [
            "daily-signal-approval",
            "--readonly-config",
            str(readonly_path),
            "--signal-config",
            str(signal_path),
            "--approval-config",
            str(approval_path),
            "--keep-telegram-operator",
        ],
    )

    assert result.exit_code == 0
    assert "symphony_daily status=signal_completed" in result.output
    assert "action_required=true" in result.output
    assert "telegram_signal_summary=sent chats=1" in result.output
    assert "symphony_daily status=approval_completed" in result.output
    assert fake_clients
    text = fake_clients[0].sent_messages[0]["text"]
    assert "Maestro daily signal summary" in text
    assert "action_required: true" in text


def test_daily_signal_approval_cli_scopes_signal_run_to_strategy_ids(tmp_path, monkeypatch):
    readonly_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="readonly_scoped.yaml",
    )
    signal_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="signal_scoped.yaml",
        identity_group="daily_signal_scoped",
    )
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="approval_scoped.yaml",
        identity_group="daily_signal_scoped",
    )
    captured: dict[str, object] = {}
    original_run_signal = MaestroOrchestrator.run_signal

    def capturing_run_signal(self, strategy_ids=None):
        captured["strategy_ids"] = strategy_ids
        return original_run_signal(self, strategy_ids=strategy_ids)

    monkeypatch.setattr(MaestroOrchestrator, "run_signal", capturing_run_signal)

    result = CliRunner().invoke(
        app,
        [
            "daily-signal-approval",
            "--readonly-config",
            str(readonly_path),
            "--signal-config",
            str(signal_path),
            "--approval-config",
            str(approval_path),
            "--strategy-ids",
            "sample_static_allocation",
            "--keep-telegram-operator",
        ],
    )

    assert result.exit_code == 0
    assert captured["strategy_ids"] == ["sample_static_allocation"]
    assert "symphony_daily status=signal_completed" in result.output


def test_daily_signal_approval_cli_rejects_unknown_strategy_ids(tmp_path):
    readonly_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="readonly_unknown.yaml",
    )
    signal_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="signal_unknown.yaml",
        identity_group="daily_signal_unknown",
    )
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="approval_unknown.yaml",
        identity_group="daily_signal_unknown",
    )

    result = CliRunner().invoke(
        app,
        [
            "daily-signal-approval",
            "--readonly-config",
            str(readonly_path),
            "--signal-config",
            str(signal_path),
            "--approval-config",
            str(approval_path),
            "--strategy-ids",
            "does_not_exist",
            "--keep-telegram-operator",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "Unknown or disabled signal strategy id" in str(result.exception)


def test_daily_signal_approval_cli_sends_failure_briefing_when_readonly_refresh_fails(
    tmp_path,
    monkeypatch,
):
    readonly_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="readonly_failure.yaml",
        account_id="kis_mock",
    )
    signal_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="signal_failure.yaml",
        provider="telegram",
        identity_group="daily_failure_briefing",
    )
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="approval_failure.yaml",
        provider="console",
        identity_group="daily_failure_briefing",
    )
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    def init_service(
        self: KISReadOnlyService,
        config,
        state_store,
        audit_logger,
        client=None,
        instruments=None,
        logical_account_id=None,
    ) -> None:
        self.logical_account_id = logical_account_id

    def fail_snapshot(self: KISReadOnlyService, symbols: list[str]):
        del symbols
        raise ValueError("KIS request failed with HTTP 500")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)
    monkeypatch.setattr(KISReadOnlyService, "__init__", init_service)
    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fail_snapshot)

    result = CliRunner().invoke(
        app,
        [
            "daily-signal-approval",
            "--readonly-config",
            str(readonly_path),
            "--signal-config",
            str(signal_path),
            "--approval-config",
            str(approval_path),
            "--keep-telegram-operator",
        ],
    )

    assert result.exit_code != 0
    assert "readonly refresh failed for account kis_mock" in result.output
    assert "telegram_daily_failure=sent chats=1" in result.output
    assert fake_clients
    text = fake_clients[0].sent_messages[0]["text"]
    assert "Maestro daily briefing failed" in text
    assert "stage: readonly_refresh" in text
    assert "readonly refresh failed for account kis_mock" in text


def test_daily_signal_approval_cli_sends_failure_briefing_when_reconciliation_fails(
    tmp_path,
    monkeypatch,
):
    readonly_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="reconciliation_failure.yaml",
        account_id="kis_mock",
    )
    signal_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="signal_reconciliation_failure.yaml",
        provider="telegram",
        identity_group="daily_reconciliation_failure_briefing",
    )
    approval_path = _paper_approval_config_path(
        tmp_path,
        "approved",
        filename="approval_reconciliation_failure.yaml",
        provider="console",
        identity_group="daily_reconciliation_failure_briefing",
    )
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(*, token_env: str, timeout_seconds: float) -> FakeTelegramClient:
        assert token_env == "TELEGRAM_BOT_TOKEN"
        assert timeout_seconds == 10.0
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    def fail_reconciliation(self):
        del self
        return ReconciliationResult(
            run_id="reconcile_fail",
            passed=False,
            checked_at=utc_now().isoformat(),
            cash_difference=-100.0,
            issues=[
                ReconciliationIssue(
                    issue_type="cash_mismatch",
                    symbol="CASH_KRW",
                    difference=-100.0,
                    tolerance=1.0,
                    message="Broker cash differs from Maestro cash.",
                )
            ],
            tolerances={"cash": 1.0, "position": 0.0},
        )

    _mock_kis_snapshot_refresh(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)
    monkeypatch.setattr(
        "maestro.cli.BrokerReconciliationService.reconcile_latest",
        fail_reconciliation,
    )

    result = CliRunner().invoke(
        app,
        [
            "daily-signal-approval",
            "--readonly-config",
            str(readonly_path),
            "--signal-config",
            str(signal_path),
            "--approval-config",
            str(approval_path),
            "--keep-telegram-operator",
        ],
    )

    assert result.exit_code != 0
    assert "reconciliation=failed issues=1" in result.output
    assert "telegram_daily_failure=sent chats=1" in result.output
    assert fake_clients
    text = fake_clients[0].sent_messages[0]["text"]
    assert "Maestro daily briefing failed" in text
    assert "stage: reconciliation" in text
    assert "cash_mismatch:CASH_KRW" in text
    assert "Broker cash differs from Maestro cash." in text


def test_run_signal_uses_strategy_posture_when_signal_config_global_posture_disabled(
    monkeypatch,
    tmp_path,
):
    _mock_kis_snapshot_refresh(monkeypatch)
    config_path = _live_signal_config_path(tmp_path, "approved")
    raw = yaml.safe_load(config_path.read_text())
    raw["execution"]["order_posture"] = "disabled"
    raw["strategies"][0]["order_posture"] = "dry_run"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)

    summary = MaestroOrchestrator(config).run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    orders_preview = signal["orders_preview"]
    assert summary.action_required is True
    assert signal["action_required"] is True
    assert {order["metadata"]["order_posture"] for order in orders_preview} == {"dry_run"}


def test_live_run_signal_refreshes_broker_truth_and_records_snapshot_refs(
    monkeypatch,
    tmp_path,
):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    orchestrator = MaestroOrchestrator(config)

    summary = orchestrator.run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    snapshots = store.list_broker_account_snapshots()
    assert len(snapshots) == 1
    assert signal["broker_snapshot_refs"] == [
        {
            "id": snapshots[0]["id"],
            "run_id": snapshots[0]["run_id"],
            "account_id": "kis_paper",
            "broker_account_id": "MOCK-LIVE",
            "created_at": snapshots[0]["created_at"],
            "fetched_at": snapshots[0]["payload"]["account"]["fetched_at"],
        }
    ]
    assert signal["datahub_evidence"]["issue_count"] == 0
    assert signal["datahub_evidence"]["price_symbols"] == ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    assert "sample_static_allocation" in signal["datahub_evidence"]["strategies"]


def test_approve_signal_rejects_stale_broker_snapshot_refs(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["broker_snapshot_refs"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="stale broker snapshot"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_material_broker_snapshot_change(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "run_new_broker_truth",
        "MOCK-LIVE",
        {
            "account_id": "kis_paper",
            "broker_account_id": "MOCK-LIVE",
            "account": {
                "account_id": "MOCK-LIVE",
                "cash": 9_000_000.0,
                "cash_by_currency": {"KRW": 9_000_000.0},
                "buying_power": 9_000_000.0,
                "positions": [],
                "fetched_at": utc_now().isoformat(),
                "source": "kis_mock",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

    with pytest.raises(ValueError, match="broker snapshot changed"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_expired_signal_package(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["generated_at"] = "2000-01-01T00:00:00+00:00"
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="expired signal package"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_allows_signal_and_approval_order_posture_difference(
    monkeypatch,
    tmp_path,
):
    _mock_kis_snapshot_refresh(monkeypatch)
    signal_config_path = _live_signal_config_path(tmp_path, "approved")
    approval_config_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="approval_order_posture.yaml",
    )
    signal_raw = yaml.safe_load(signal_config_path.read_text())
    signal_raw["execution"]["order_posture"] = "disabled"
    signal_raw["strategies"][0]["order_posture"] = "dry_run"
    signal_config_path.write_text(yaml.safe_dump(signal_raw))
    approval_raw = yaml.safe_load(approval_config_path.read_text())
    approval_raw["execution"]["order_posture"] = "dry_run"
    approval_raw["strategies"][0]["order_posture"] = "dry_run"
    approval_config_path.write_text(yaml.safe_dump(approval_raw))
    signal_config, signal_identity = load_config_with_identity(signal_config_path)
    approval_config, approval_identity = load_config_with_identity(approval_config_path)
    signal_summary = MaestroOrchestrator(
        signal_config,
        config_identity=signal_identity,
    ).run_signal()

    approval_summary = MaestroOrchestrator(
        approval_config,
        config_identity=approval_identity,
    ).approve_signal(signal_summary.signal_run_id)

    assert approval_summary.orders_created == 2
    assert approval_summary.approval_status == "approved"


def test_approve_signal_rejects_config_runtime_mismatch(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    signal_config_path = _live_signal_config_path(tmp_path, "approved")
    approval_config_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="changed_signal_approval.yaml",
        strategy_weight=0.5,
    )
    signal_config, signal_identity = load_config_with_identity(signal_config_path)
    approval_config, approval_identity = load_config_with_identity(approval_config_path)
    signal_summary = MaestroOrchestrator(
        signal_config,
        config_identity=signal_identity,
    ).run_signal()

    with pytest.raises(ValueError, match="config runtime mismatch") as exc_info:
        MaestroOrchestrator(
            approval_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)
    message = str(exc_info.value)
    assert "signal_fingerprint=" in message
    assert "current_fingerprint=" in message
    assert "maestro profile-diff --left <signal-config> --right <approval-config>" in message


def test_approve_signal_rejects_account_mapping_mismatch(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    signal_config_path = _live_signal_config_path(tmp_path, "approved")
    approval_config_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="remapped_signal_approval.yaml",
        account_id="kis_other",
    )
    signal_config, signal_identity = load_config_with_identity(signal_config_path)
    approval_config, approval_identity = load_config_with_identity(approval_config_path)
    signal_summary = MaestroOrchestrator(
        signal_config,
        config_identity=signal_identity,
    ).run_signal()
    store = StateStore(signal_config.state.sqlite_path, signal_config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal.pop("config_runtime_fingerprint")
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="account mapping mismatch"):
        MaestroOrchestrator(
            approval_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rechecks_data_quality_gate(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["data_quality_issues"] = [{"symbol": "MOCK_ETF_A", "reason": "stale_price"}]
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="live execution gate"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    stale_events = store.list_system_events_by_type("stale_data_halt")
    assert stale_events[0]["payload"]["issues"][0]["reason"] == "stale_price"


def test_approve_signal_rejects_missing_datahub_evidence(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal.pop("datahub_evidence")
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="missing DataHub evidence"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rechecks_safety_state(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    orchestrator = MaestroOrchestrator(config)
    SafetyControlService(orchestrator.state_store, orchestrator.audit).kill_switch(
        new_run_id(),
        "operator kill",
    )

    with pytest.raises(ValueError, match="safety state blocks"):
        orchestrator.approve_signal(signal_summary.signal_run_id)

    blocked = orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")
    assert blocked[0]["payload"]["state"] == "killed"
    assert blocked[0]["payload"]["phase"] == "approve_signal"


def _buy_only_funding_config(tmp_path, *, funding_request_enabled: bool):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    raw["strategies"] = [
        {
            "id": "buy_only_funding",
            "enabled": True,
            "signal_enabled": True,
            "weight": 1.0,
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "order_posture": "dry_run",
            "entrypoint": f"{__name__}:BuyOnlyFundingStrategy",
            "config": {},
        }
    ]
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
                        "funding_request": {"enabled": funding_request_enabled},
                    },
                }
            }
        }
    }
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
    raw["state"]["sqlite_path"] = str(tmp_path / "funding_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "funding_audit.jsonl")
    config_path = tmp_path / f"buy_only_funding_{funding_request_enabled}.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


def _paper_approval_config(tmp_path, decision):
    return load_config(_paper_approval_config_path(tmp_path, decision))


def _paper_approval_config_path(
    tmp_path,
    decision,
    *,
    filename: str = "signal_approval.yaml",
    provider: str = "console",
    identity_group: str | None = None,
) -> Path:
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    if identity_group is not None:
        raw["state"]["identity_group"] = identity_group
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["provider"] = provider
    raw["approval"]["default_decision"] = decision
    if provider == "telegram":
        raw["approval"]["telegram_allowed_chat_ids"] = [100]
        raw["approval"]["whitelisted_user_ids"] = [100]
        raw["approval"]["telegram_poll_interval_seconds"] = 0
    config_path = tmp_path / filename
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages = []

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}


class CountingLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.submit_count = 0
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.submit_count += 1
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=BrokerOrderId(
                broker="kis",
                broker_order_id=f"KIS-{self.submit_count}",
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
            ),
        )


class FilledStatusClient(LiveOrderStatusClient):
    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=OrderStatus.FILLED,
            checked_at=utc_now().isoformat(),
            symbol="MOCK_ETF_A",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=1.0,
                filled_quantity=1.0,
                remaining_quantity=0.0,
                average_fill_price=100.0,
                fill_count=1,
            ),
            raw_status=OrderStatus.FILLED.value,
        )


class PassingBrokerReconciliation:
    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id=new_run_id(),
            passed=True,
            checked_at=utc_now().isoformat(),
            issues=[],
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )


class ApprovingTelegramApprovalManager:
    def request_approval(
        self,
        run_id: str,
        orders,
        risk_violations,
        source_strategy_ids=None,
    ) -> tuple[ApprovalRequest, ApprovalDecision, str]:
        del risk_violations
        request = ApprovalRequest(
            approval_id=f"appr_{run_id}",
            run_id=run_id,
            created_at=utc_now(),
            expires_at=utc_now(),
            channel="telegram",
            source_strategy_ids=list(source_strategy_ids or []),
            order_count=len(orders),
            estimated_notional=sum(order.notional for order in orders),
            proposed_orders=[order.model_dump(mode="json") for order in orders],
        )
        decision = ApprovalDecision(
            approval_id=request.approval_id,
            run_id=run_id,
            status="approved",
            decided_at=utc_now(),
            decided_by="telegram:fake",
        )
        return request, decision, "approved"


class FailingApprovalManager:
    def request_approval(
        self,
        run_id: str,
        orders,
        risk_violations,
        source_strategy_ids=None,
    ) -> tuple[ApprovalRequest | None, ApprovalDecision | None, str | None]:
        del run_id, orders, risk_violations, source_strategy_ids
        raise RuntimeError("approval manager failed")


def _live_signal_config(tmp_path, decision):
    return load_config(_live_signal_config_path(tmp_path, decision))


def _live_signal_config_path(
    tmp_path,
    decision,
    *,
    filename: str = "live_signal_approval.yaml",
    account_id: str = "kis_paper",
    strategy_weight: float = 1.0,
) -> Path:
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["portfolio"].pop("initial_cash", None)
    raw["strategies"][0]["account_id"] = account_id
    raw["strategies"][0]["weight"] = strategy_weight
    raw["accounts"] = [
        {
            "id": account_id,
            "broker": "kis",
            "environment": "paper_trading",
            "enabled": True,
            "provider": "kis",
            "account_id": "MOCK-LIVE",
            "broker_products": ["kis_overseas_stock"],
        }
    ]
    raw["execution"]["order_posture"] = "dry_run"
    raw["execution"]["live_order_limits"] = {
        "max_order_notional": 10_000_000,
        "max_daily_notional": 20_000_000,
        "max_daily_order_count": 10,
        "daily_loss_limit": None,
        "fee_buffer_pct": 0.0,
    }
    raw["state"]["sqlite_path"] = str(tmp_path / "live_state.db")
    raw["state"]["identity_group"] = "test_signal_approval"
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_audit.jsonl")
    raw["approval"]["default_decision"] = decision
    config_path = tmp_path / filename
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _mock_kis_snapshot_refresh(monkeypatch) -> None:
    def init_service(
        self: KISReadOnlyService,
        config,
        state_store,
        audit_logger,
        client=None,
        instruments=None,
        logical_account_id=None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.instruments = instruments or []
        self.logical_account_id = logical_account_id
        self.client = client

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        account = KISAccountSnapshot(
            account_id=self.config.account_id or "MOCK-LIVE",
            cash=10_000_000.0,
            cash_by_currency={"KRW": 10_000_000.0},
            buying_power=10_000_000.0,
            positions=[],
            fetched_at=utc_now(),
            source="kis_mock",
        )
        snapshot = KISReadOnlySnapshot(
            account=account,
            current_prices={symbol: 100.0 for symbol in symbols if symbol != "CASH"},
            order_fills=[],
            unfilled_orders=[],
        )
        payload = snapshot.model_dump(mode="json")
        payload["account_id"] = self.logical_account_id
        payload["broker_account_id"] = snapshot.account.account_id
        self.state_store.save_broker_account_snapshot(
            "run_mock_broker_snapshot",
            snapshot.account.account_id,
            payload,
        )
        return snapshot

    monkeypatch.setattr(KISReadOnlyService, "__init__", init_service)
    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)
