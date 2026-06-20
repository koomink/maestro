from pathlib import Path

import pytest
import yaml
from sample_static_allocation.strategy import SampleStaticAllocationStrategy
from typer.testing import CliRunner

from maestro.cli import _send_signal_funding_request_notifications, app
from maestro.config.loader import load_config, load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
from maestro.execution.brokers.kis.models import KISAccountSnapshot, KISReadOnlySnapshot
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.reconciliation import ReconciliationIssue, ReconciliationResult
from maestro.orchestration.orchestrator import MaestroOrchestrator
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
        return super().manifest().model_copy(
            update={"strategy_id": "second_static", "name": "Second Static Allocation"}
        )

    def run(self, data_bundle, context):
        return super().run(data_bundle, context).model_copy(
            update={"strategy_id": context.strategy_id}
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
    assert {event["payload"]["signal_run_id"] for event in events} == {
        signal_summary.signal_run_id
    }
    assert {
        event["payload"]["request"]["signal_run_id"] for event in events
    } == {signal_summary.signal_run_id}
    assert approval_summary.orders_created == 2


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
        ],
    )

    assert approval_result.exit_code == 0
    assert f"signal_run_id={signal_run_id}" in approval_result.output
    assert "orders=2" in approval_result.output


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

    with pytest.raises(ValueError, match="config runtime mismatch"):
        MaestroOrchestrator(
            approval_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)


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
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"ok": True}


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
