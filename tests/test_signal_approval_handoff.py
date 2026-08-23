import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml
from sample_static_allocation.strategy import SampleStaticAllocationStrategy
from typer.testing import CliRunner

from maestro.approval.models import (
    ApprovalDecision,
    ApprovalRequest,
    PendingApprovalEnvelope,
)
from maestro.cli import (
    _run_daily_signal_approval,
    _send_signal_budget_request_notifications,
    _send_signal_funding_request_notifications,
    app,
)
from maestro.config.loader import load_config, load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.core.ids import new_run_id
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.kis.models import KISAccountSnapshot, KISReadOnlySnapshot
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.live_order_safety import build_live_order_idempotency_key
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
from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.orchestration.dispatch_group import dispatch_group_id
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
from maestro.state.models import PortfolioState
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


def test_a_funding_request_that_cannot_be_sent_is_not_reported_as_nothing(
    monkeypatch,
    tmp_path,
):
    """요청이 있었다는 사실은 채널 상태와 무관하게 보고돼야 한다.

    이 함수들이 실패에도 0을 돌려주던 시절에는 호출자가 그것을 "오늘은
    올라온 게 없다"로 읽었다. 채널 검사가 패키지 로드보다 앞에 있었기
    때문에, 토큰이 없는 날은 요청을 세어 보지도 못했다.
    """
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=True)
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["buy_only_funding"])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    sent = _send_signal_funding_request_notifications(config, summary.signal_run_id)

    assert sent.requested == 1
    assert sent.delivered == 0
    assert sent.failed is True
    assert bool(sent) is True


def test_a_partly_sent_funding_notification_counts_what_actually_went_out(
    monkeypatch,
    tmp_path,
):
    """delivered는 실제로 반환된 전송만 센다.

    예전에는 루프가 끝난 뒤 요청수 x 채팅수로 계산해서, 중간에 예외가 나면
    이미 성공한 전송까지 없던 일이 됐다.
    """
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=True)
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100, 200]
    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["buy_only_funding"])

    class HalfDeadClient(FakeTelegramClient):
        def send_message(self, chat_id, text, reply_markup=None):
            if chat_id == 200:
                raise RuntimeError("telegram unreachable")
            return super().send_message(chat_id, text, reply_markup=reply_markup)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr(
        "maestro.cli.TelegramBotAPIClient",
        lambda **kwargs: HalfDeadClient(),
    )

    sent = _send_signal_funding_request_notifications(config, summary.signal_run_id)

    assert (sent.requested, sent.delivered, sent.failed) == (1, 1, True)


def test_signal_budget_request_notification_sends_telegram_message(
    monkeypatch,
    tmp_path,
):
    """예산 요청 발송 경로에는 직접 테스트가 없었다.

    패키지를 직접 써 넣는다 -- 검증 대상은 예산 요청을 만들어내는 전략
    설정이 아니라 발송 함수 자체이고, 그쪽은 이미 별도로 덮여 있다.
    """
    config = _buy_only_funding_config(tmp_path, funding_request_enabled=True)
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
    )
    store.save_signal_package(
        "signal-budget",
        {
            "orders_preview": [],
            "budget_requests": [
                {
                    "request_id": "budget_req_1",
                    "source_signal_run_id": "signal-budget",
                    "strategy_ids": ["buy_only_funding"],
                    "account_id": "paper_cash",
                    "execution_sleeve": "krw_contribution",
                    "currency": "KRW",
                    "available_cash": 2_000_000.0,
                    "min_monthly_budget": 200_000.0,
                    "recommended_budget": 400_000.0,
                    "selectable_max_budget": 1_000_000.0,
                    "month_key": "2026-08",
                    "status": "pending",
                }
            ],
        },
    )
    fake_clients: list[FakeTelegramClient] = []

    def fake_client_factory(**kwargs):
        del kwargs
        client = FakeTelegramClient()
        fake_clients.append(client)
        return client

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr("maestro.cli.TelegramBotAPIClient", fake_client_factory)

    sent = _send_signal_budget_request_notifications(config, "signal-budget")

    assert (sent.requested, sent.delivered, sent.failed) == (1, 1, False)
    assert "Maestro budget request" in fake_clients[0].sent_messages[0]["text"]


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

    assert (sent.requested, sent.delivered, sent.failed) == (1, 1, False)
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


def test_dispatch_signal_approval_returns_pending_without_polling(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    config.approval.timeout_seconds = 600
    config.approval.telegram_reminder_seconds = [120, 300, 480]
    signal_summary = MaestroOrchestrator(config).run_signal()
    client = FakeTelegramClient()

    result = MaestroOrchestrator(
        config,
        telegram_client=client,
        order_capacity_lookup=lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000_000,
            max_buy_quantity=100_000,
            source="fake",
        ),
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert result.approval_status == "pending"
    assert result.approvals_pending == 1
    assert store.list_approvals(limit=10) == []
    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    assert pending[0]["payload"]["reminder_seconds"] == [120, 300, 480]
    assert client.sent_messages[0]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ].startswith("operator:appr:a:")


def test_dispatch_files_the_envelope_under_its_group_id(monkeypatch, tmp_path):
    # The old key was telegram-approval-pending:<approval_id>. Because the id
    # is random it could never collide, so it made the envelope impossible to
    # find again -- a resume had no way to recognize a group it had already
    # dispatched, and minted a second approval for the same orders.
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    signal_summary = MaestroOrchestrator(config).run_signal()

    MaestroOrchestrator(
        config,
        telegram_client=FakeTelegramClient(),
        order_capacity_lookup=lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000_000,
            max_buy_quantity=100_000,
            source="fake",
        ),
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    pending = store.list_system_events_by_type("telegram_approval_pending")
    expected_key = dispatch_group_id(
        signal_summary.signal_run_id,
        pending[0]["payload"]["source_strategy_ids"],
    )
    assert pending[0]["payload"]["duplicate_key"] == expected_key
    assert store.load_system_event_payload_by_duplicate_key(expected_key) is not None


def test_each_approval_group_gets_its_own_envelope_key(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    # A second account doubles the portfolio, so the per-order cap the single
    # account fixture sets would block the run before grouping matters.
    config.execution.live_order_limits.max_order_notional = 100_000_000
    config.execution.live_order_limits.max_daily_notional = 200_000_000
    # Two strategies on one account merge into a single order group, so the
    # second one needs its own account for two groups to exist at all.
    config.accounts.append(
        config.accounts[0].model_copy(update={"id": "kis_paper_b", "account_id": "MOCK-LIVE-B"})
    )
    config.strategies.append(
        config.strategies[0].model_copy(
            update={
                "id": "second_static",
                "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
                "account_id": "kis_paper_b",
                "weight": 1.0,
                "config": {"allocations": {"MOCK_ETF_B": 1.0}},
            }
        )
    )
    signal_summary = MaestroOrchestrator(config).run_signal()

    MaestroOrchestrator(
        config,
        telegram_client=FakeTelegramClient(),
        order_capacity_lookup=lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000_000,
            max_buy_quantity=100_000,
            source="fake",
        ),
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    pending = store.list_system_events_by_type("telegram_approval_pending")
    keys = {row["payload"]["duplicate_key"] for row in pending}
    assert len(pending) == 2
    assert len(keys) == 2


def _dispatch_orchestrator(config, client):
    return MaestroOrchestrator(
        config,
        telegram_client=client,
        order_capacity_lookup=lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000_000,
            max_buy_quantity=100_000,
            source="fake",
        ),
    )


def _telegram_dispatch_config(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    config.approval.timeout_seconds = 600
    return config


def test_a_dispatch_that_died_before_reporting_is_resumed_not_refused(monkeypatch, tmp_path):
    # The package is consumed before the group loop runs, so a crash inside
    # that loop used to strand the run: re-dispatching raised "already
    # consumed" and the approvals that were never created never would be.
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.mark_signal_package_consumed(signal_summary.signal_run_id, new_run_id())
    assert store.list_incomplete_signal_dispatches() == [signal_summary.signal_run_id]

    client = FakeTelegramClient()
    result = _dispatch_orchestrator(config, client).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    assert result.approval_status == "pending"
    assert client.sent_messages
    assert store.list_incomplete_signal_dispatches() == []


def test_a_settled_dispatch_is_still_refused(monkeypatch, tmp_path):
    # Reopening a run that finished would send a second card for orders the
    # operator has already been asked about.
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    with pytest.raises(ValueError, match="already consumed"):
        _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
            signal_summary.signal_run_id
        )


def test_resuming_reuses_the_approval_and_its_original_deadline(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    first = store.list_system_events_by_type("telegram_approval_pending")[0]["payload"]
    # Reopen the run the way a crash between the last group and the settled
    # event would have left it.
    with sqlite3.connect(config.state.sqlite_path) as conn:
        conn.execute("DELETE FROM system_events WHERE event_type = 'signal_approval_pending'")

    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    assert pending[0]["payload"]["approval_id"] == first["approval_id"]
    assert pending[0]["payload"]["expires_at"] == first["expires_at"]


def test_a_resume_survives_a_message_template_change(monkeypatch, tmp_path):
    # The stored envelope is authoritative. Comparing it against a fresh
    # render -- which is what save_system_events_atomic would do -- would turn
    # any copy change between releases into a permanently stuck run.
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    with sqlite3.connect(config.state.sqlite_path) as conn:
        conn.execute("DELETE FROM system_events WHERE event_type = 'signal_approval_pending'")
    config.approval.telegram_reminder_seconds = [1, 2, 3]

    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    # The record wins: the reminder schedule stays what it was sent with.
    assert pending[0]["payload"]["reminder_seconds"] != [1, 2, 3]


def test_a_resume_creates_only_the_group_that_was_missing(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    config.execution.live_order_limits.max_order_notional = 100_000_000
    config.execution.live_order_limits.max_daily_notional = 200_000_000
    config.accounts.append(
        config.accounts[0].model_copy(update={"id": "kis_paper_b", "account_id": "MOCK-LIVE-B"})
    )
    config.strategies.append(
        config.strategies[0].model_copy(
            update={
                "id": "second_static",
                "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
                "account_id": "kis_paper_b",
                "weight": 1.0,
                "config": {"allocations": {"MOCK_ETF_B": 1.0}},
            }
        )
    )
    signal_summary = MaestroOrchestrator(config).run_signal()
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    surviving = store.list_system_events_by_type("telegram_approval_pending")[0]["payload"]
    # Leave one group's envelope behind and drop the other, the shape a crash
    # partway through the loop produces.
    with sqlite3.connect(config.state.sqlite_path) as conn:
        conn.execute("DELETE FROM system_events WHERE event_type = 'signal_approval_pending'")
        conn.execute(
            "DELETE FROM system_events WHERE event_type = 'telegram_approval_pending' "
            "AND duplicate_key != ?",
            (surviving["duplicate_key"],),
        )

    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    pending = store.list_system_events_by_type("telegram_approval_pending")
    by_key = {row["payload"]["duplicate_key"]: row["payload"] for row in pending}
    assert len(pending) == 2
    assert by_key[surviving["duplicate_key"]]["approval_id"] == surviving["approval_id"]


def test_a_dispatch_telegram_refused_stays_resumable(monkeypatch, tmp_path):
    # The approval card reached nobody, so the run must not look finished.
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()

    with pytest.raises(RuntimeError, match="refused the approval card"):
        _dispatch_orchestrator(config, RefusingTelegramClient()).dispatch_signal_approval(
            signal_summary.signal_run_id
        )

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert store.list_incomplete_signal_dispatches() == [signal_summary.signal_run_id]
    assert store.signal_dispatch_settled(signal_summary.signal_run_id) is False

    # And the retry reuses the approval rather than minting a second one.
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )
    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    assert store.list_incomplete_signal_dispatches() == []


def test_a_dispatch_interrupted_between_groups_is_not_recorded_as_settled(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _telegram_dispatch_config(tmp_path)
    config.execution.live_order_limits.max_order_notional = 100_000_000
    config.execution.live_order_limits.max_daily_notional = 200_000_000
    config.accounts.append(
        config.accounts[0].model_copy(update={"id": "kis_paper_b", "account_id": "MOCK-LIVE-B"})
    )
    config.strategies.append(
        config.strategies[0].model_copy(
            update={
                "id": "second_static",
                "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
                "account_id": "kis_paper_b",
                "weight": 1.0,
                "config": {"allocations": {"MOCK_ETF_B": 1.0}},
            }
        )
    )
    signal_summary = MaestroOrchestrator(config).run_signal()

    # Fail while building the second group's card, after the first group is
    # fully persisted and delivered. A send-time exception would not do: the
    # lifecycle classifies those as delivery-unknown by design and carries on.
    import maestro.orchestration.orchestrator as orchestrator_module

    real_render = orchestrator_module.render_approval_stage_card
    calls = {"n": 0}

    def die_on_second(request, stage):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("process died between groups")
        return real_render(request, stage)

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_on_second)

    with pytest.raises(RuntimeError, match="died between groups"):
        _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
            signal_summary.signal_run_id
        )
    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", real_render)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    # One group made it. The settled event must not be written until both are
    # done, or the sweep would treat a half-dispatched run as finished.
    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 1
    assert store.list_incomplete_signal_dispatches() == [signal_summary.signal_run_id]

    first = store.list_system_events_by_type("telegram_approval_pending")[0]["payload"]
    _dispatch_orchestrator(config, FakeTelegramClient()).dispatch_signal_approval(
        signal_summary.signal_run_id
    )

    pending = store.list_system_events_by_type("telegram_approval_pending")
    by_key = {row["payload"]["duplicate_key"]: row["payload"] for row in pending}
    assert len(pending) == 2
    assert by_key[first["duplicate_key"]]["approval_id"] == first["approval_id"]
    assert store.list_incomplete_signal_dispatches() == []


def _capacity_lookup(*, blocked_symbols: frozenset[str] = frozenset(), cash: float = 10_000_000):
    def lookup(order):
        blocked = order.symbol in blocked_symbols
        return BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0 if blocked else cash,
            max_buy_quantity=0 if blocked else 1_000_000,
            source="fake",
        )

    return lookup


def _two_group_config(tmp_path, *, armed: bool = False):
    config = _telegram_dispatch_config(tmp_path)
    config.execution.live_order_limits.max_order_notional = 100_000_000
    config.execution.live_order_limits.max_daily_notional = 200_000_000
    config.accounts.append(
        config.accounts[0].model_copy(update={"id": "kis_paper_b", "account_id": "MOCK-LIVE-B"})
    )
    config.strategies.append(
        config.strategies[0].model_copy(
            update={
                "id": "second_static",
                "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
                "account_id": "kis_paper_b",
                "weight": 1.0,
                "config": {"allocations": {"MOCK_ETF_B": 1.0}},
            }
        )
    )
    if armed:
        # _partition_orders_by_capacity only ever looks at armed orders --
        # everything else (dry_run, the default this fixture otherwise
        # leaves every order in) bypasses the capacity lookup entirely.
        config.execution.order_posture = "armed"
        for strategy in config.strategies:
            strategy.order_posture = "armed"
    return config


def _dispatch_orchestrator_with_capacity(config, client, lookup):
    return MaestroOrchestrator(config, telegram_client=client, order_capacity_lookup=lookup)


def _armed_dispatch_config(tmp_path):
    """One strategy, one group, two orders (MOCK_ETF_A and MOCK_ETF_B) --
    for exercising a *partial* block within a single group, which
    _two_group_config's one-symbol-per-group strategies cannot."""
    config = _telegram_dispatch_config(tmp_path)
    config.execution.order_posture = "armed"
    config.strategies[0].order_posture = "armed"
    return config


def test_a_resume_marks_every_group_capacity_blocked_rather_than_dropping_them(
    monkeypatch, tmp_path
):
    """Priority 2, all-groups-blocked: a dispatch that never created a single
    envelope, resumed once capacity has tightened to zero, must not let the
    settled event fire over groups nobody ever recorded a disposition for.

    Recomputing groups from live, post-capacity orders (the old behavior)
    would see zero approval orders survive the partition and never even
    build the two groups the original dispatch was obligated to resolve --
    "capacity_blocked" would report a number, but nothing would say *which*
    groups that covered, and a resume after capacity recovers would have no
    way to tell "already resolved as blocked" from "never seen".
    """
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _two_group_config(tmp_path, armed=True)
    signal_summary = MaestroOrchestrator(config).run_signal()

    # First attempt dies before either group's card is even rendered -- the
    # manifest (if the dispatch got that far) is the only durable record of
    # what groups exist at all.
    import maestro.orchestration.orchestrator as orchestrator_module

    def die_immediately(request, stage):
        raise RuntimeError("process died before the first card")

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_immediately)
    with pytest.raises(RuntimeError, match="died before the first card"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)
    monkeypatch.undo()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    manifest = store.load_system_event_payload_by_duplicate_key(
        f"dispatch-manifest:{signal_summary.signal_run_id}"
    )
    assert manifest is not None
    assert len(manifest["groups"]) == 2

    # Resume with capacity now blocking everything.
    _dispatch_orchestrator_with_capacity(
        config,
        FakeTelegramClient(),
        _capacity_lookup(blocked_symbols=frozenset({"MOCK_ETF_A", "MOCK_ETF_B"})),
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    assert store.list_system_events_by_type("telegram_approval_pending") == []
    blocked = store.list_system_events_by_type("dispatch_group_capacity_blocked")
    assert {row["payload"]["group_id"] for row in blocked} == {
        group["group_id"] for group in manifest["groups"]
    }
    assert store.signal_dispatch_settled(signal_summary.signal_run_id) is True
    assert store.list_incomplete_signal_dispatches() == []


def test_a_resume_blocks_only_the_group_capacity_now_refuses(monkeypatch, tmp_path):
    """Priority 2, some-groups-blocked: one group already has a live card;
    resuming after capacity now refuses the other must adopt the first
    envelope unchanged and durably record the second as blocked, not drop it
    from the run's accounting."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _two_group_config(tmp_path, armed=True)
    signal_summary = MaestroOrchestrator(config).run_signal()

    import maestro.orchestration.orchestrator as orchestrator_module

    real_render = orchestrator_module.render_approval_stage_card
    calls = {"n": 0}

    def die_on_second(request, stage):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("process died between groups")
        return real_render(request, stage)

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_on_second)
    with pytest.raises(RuntimeError, match="died between groups"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)
    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", real_render)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    pending_before = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending_before) == 1
    surviving = pending_before[0]["payload"]

    # Resume with capacity now refusing MOCK_ETF_B -- whichever group that is
    # (the two accounts' strategies race in unspecified order), the surviving
    # envelope above already tells us which group made it through.
    blocked_symbol = "MOCK_ETF_A" if "MOCK_ETF_A" not in {
        order["symbol"] for order in surviving["orders"]
    } else "MOCK_ETF_B"
    result = _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup(blocked_symbols=frozenset({blocked_symbol}))
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    pending_after = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending_after) == 1
    assert pending_after[0]["payload"]["approval_id"] == surviving["approval_id"]
    blocked = store.list_system_events_by_type("dispatch_group_capacity_blocked")
    assert len(blocked) == 1
    assert result.approval_status == "pending"
    assert result.approvals_pending == 1
    # Priority 3: the blocked group's order was durably blocked, not merely
    # observed and forgotten -- the final summary must report it even
    # though it was recorded on this same call, not a fresh re-observation
    # after the fact.
    assert result.orders_capacity_blocked == 1
    pending_event = store.list_system_events_by_type("signal_approval_pending")[0]
    assert pending_event["payload"]["orders_capacity_blocked"] == 1
    assert store.signal_dispatch_settled(signal_summary.signal_run_id) is True
    assert store.list_incomplete_signal_dispatches() == []


def test_orders_capacity_blocked_reports_the_durable_total_from_an_earlier_attempt(
    monkeypatch, tmp_path
):
    """Priority 3: a group blocked on an *earlier* attempt (its own call to
    _partition_orders_by_capacity long since returned) must still count in
    a *later* attempt's final summary. The old implementation started
    capacity_blocks = [] fresh every call and skipped already-blocked
    groups outright, so their orders were counted nowhere once the call
    that actually observed them had returned.
    """
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _two_group_config(tmp_path, armed=True)
    signal_summary = MaestroOrchestrator(config).run_signal()

    # Attempt 1: block every group's orders, then die before the settled
    # event -- as if the process crashed right after recording the block.
    import maestro.orchestration.orchestrator as orchestrator_module

    real_render = orchestrator_module.render_approval_stage_card

    def die_after_blocking(request, stage):
        raise RuntimeError("process died after recording the block")

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_after_blocking)
    with pytest.raises(RuntimeError, match="died after recording the block"):
        _dispatch_orchestrator_with_capacity(
            config,
            FakeTelegramClient(),
            _capacity_lookup(blocked_symbols=frozenset({"MOCK_ETF_A"})),
        ).dispatch_signal_approval(signal_summary.signal_run_id)
    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", real_render)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    # One group (MOCK_ETF_A's) is durably blocked; the other was never
    # reached this call (render died before it could be).
    assert len(store.list_system_events_by_type("dispatch_group_capacity_blocked")) == 1

    # Attempt 2 (resume): capacity is wide open now (generous enough for
    # second_static's full-weight order too), so the group 1's accepted
    # remainder and group 2 both dispatch cleanly. Nothing re-checks -- let
    # alone un-blocks -- the group attempt 1 already resolved.
    result = _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup(cash=25_000_000)
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 2
    assert len(store.list_system_events_by_type("dispatch_group_capacity_blocked")) == 1
    assert result.approval_status == "pending"
    assert result.orders_capacity_blocked == 1
    pending_event = store.list_system_events_by_type("signal_approval_pending")[0]
    assert pending_event["payload"]["orders_capacity_blocked"] == 1


def test_a_partially_blocked_group_records_the_exact_blocked_order_ids(monkeypatch, tmp_path):
    """Priority 2: within one group, some orders may be capacity-blocked
    while others are approved. The blocked disposition must name exactly
    the blocked orders, and together with the envelope it must exactly
    partition the manifest's order ids for that group -- no gaps, no
    overlap."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _armed_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()

    _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup(blocked_symbols=frozenset({"MOCK_ETF_B"}))
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    manifest = store.load_system_event_payload_by_duplicate_key(
        f"dispatch-manifest:{signal_summary.signal_run_id}"
    )
    assert len(manifest["groups"]) == 1
    manifest_order_ids = set(manifest["groups"][0]["order_ids"])

    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    envelope_order_ids = {order["order_id"] for order in pending[0]["payload"]["orders"]}
    assert {order["symbol"] for order in pending[0]["payload"]["orders"]} == {"MOCK_ETF_A"}

    blocked = store.list_system_events_by_type("dispatch_group_capacity_blocked")
    assert len(blocked) == 1
    blocked_order_ids = set(blocked[0]["payload"]["blocked_order_ids"])
    assert blocked_order_ids
    assert blocked_order_ids.isdisjoint(envelope_order_ids)
    assert envelope_order_ids | blocked_order_ids == manifest_order_ids


def test_a_lost_race_for_the_blocked_disposition_uses_the_winning_content(
    monkeypatch, tmp_path
):
    """TOCTOU: this call's own capacity computation may lose the race to
    write the group's blocked disposition -- insert_or_load_system_event is
    atomic, so a concurrent writer's decision can land first under the same
    key. The accepted set used to build the envelope must then be
    re-derived from whichever disposition actually became durable, not from
    this call's own, now-stale, local computation -- otherwise an order the
    durable record calls blocked could still end up in an approval
    envelope, or one it calls accepted could be silently dropped.
    """
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _armed_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    package = store.load_signal_package(signal_summary.signal_run_id)
    orders_by_symbol = {o["symbol"]: o["order_id"] for o in package["orders_preview"]}
    order_a = orders_by_symbol["MOCK_ETF_A"]
    order_b = orders_by_symbol["MOCK_ETF_B"]

    orchestrator = _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup(blocked_symbols=frozenset({"MOCK_ETF_B"}))
    )
    real_insert_or_load = orchestrator.state_store.insert_or_load_system_event

    def racing_insert_or_load(run_id, event_type, payload, duplicate_key):
        if event_type == "dispatch_group_capacity_blocked":
            # A concurrent writer's decision -- blocking A instead of the B
            # this call's own capacity check just decided -- has already
            # landed under this exact key by the time this call's own
            # insert is attempted.
            winning_payload = {**payload, "blocked_order_ids": [order_a]}
            return real_insert_or_load(run_id, event_type, winning_payload, duplicate_key)
        return real_insert_or_load(run_id, event_type, payload, duplicate_key)

    monkeypatch.setattr(
        orchestrator.state_store, "insert_or_load_system_event", racing_insert_or_load
    )

    orchestrator.dispatch_signal_approval(signal_summary.signal_run_id)

    pending = store.list_system_events_by_type("telegram_approval_pending")
    assert len(pending) == 1
    envelope_order_ids = {order["order_id"] for order in pending[0]["payload"]["orders"]}
    # The winning disposition blocked A, not B -- the envelope must reflect
    # that, not this call's own (losing) computation that blocked B.
    assert envelope_order_ids == {order_b}
    blocked = store.list_system_events_by_type("dispatch_group_capacity_blocked")
    assert set(blocked[0]["payload"]["blocked_order_ids"]) == {order_a}


def test_a_partial_block_disposition_survives_capacity_recovering_on_a_later_resume(
    monkeypatch, tmp_path
):
    """Priority 2: once a group's disposition is durably split between an
    envelope and a blocked marker, capacity recovering later must not
    reopen the group -- the manifest's membership for that group is fixed
    forever once it has one envelope, exactly as an all-accepted group
    already is."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _armed_dispatch_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()

    _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup(blocked_symbols=frozenset({"MOCK_ETF_B"}))
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    blocked_before = store.list_system_events_by_type("dispatch_group_capacity_blocked")

    # Already fully settled (pending_count > 0) -- a further call is an
    # ordinary settled dispatch, refused outright, not a live re-evaluation.
    with pytest.raises(ValueError, match="already consumed"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)

    assert store.list_system_events_by_type("dispatch_group_capacity_blocked") == blocked_before
    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 1


def test_the_manifest_is_unchanged_by_a_resume_under_unchanged_capacity(monkeypatch, tmp_path):
    """Priority 2, unchanged capacity: the manifest a resume loads must be
    byte-identical to the one the first attempt wrote -- nothing about a
    routine resume may recompute the obligation."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _two_group_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    manifest_key = f"dispatch-manifest:{signal_summary.signal_run_id}"

    import maestro.orchestration.orchestrator as orchestrator_module

    real_render = orchestrator_module.render_approval_stage_card
    calls = {"n": 0}

    def die_on_second(request, stage):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("process died between groups")
        return real_render(request, stage)

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_on_second)
    with pytest.raises(RuntimeError, match="died between groups"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)
    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", real_render)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    first_manifest = store.load_system_event_payload_by_duplicate_key(manifest_key)
    assert first_manifest is not None
    assert len(first_manifest["groups"]) == 2

    _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup()
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    second_manifest = store.load_system_event_payload_by_duplicate_key(manifest_key)
    assert second_manifest == first_manifest
    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 2


def test_repeated_resumes_under_unchanged_capacity_are_idempotent(monkeypatch, tmp_path):
    """Priority 2, repeated resume/idempotency: a dispatch that takes three
    calls to fully land (each of the first two interrupted at a different
    point) must not multiply the manifest, envelopes, or the settled event
    -- and once it has landed, a further call is refused exactly as any
    other settled dispatch is."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _two_group_config(tmp_path)
    signal_summary = MaestroOrchestrator(config).run_signal()
    manifest_key = f"dispatch-manifest:{signal_summary.signal_run_id}"
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    import maestro.orchestration.orchestrator as orchestrator_module

    real_render = orchestrator_module.render_approval_stage_card

    def die_on_call(n: int):
        calls = {"i": 0}

        def render(request, stage):
            calls["i"] += 1
            if calls["i"] == n:
                raise RuntimeError(f"process died on card {n}")
            return real_render(request, stage)

        return render

    # Attempt 1: dies before the first card. Attempt 2 (resume): the first
    # card survives, dies before the second.
    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_on_call(1))
    with pytest.raises(RuntimeError, match="died on card 1"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", die_on_call(2))
    with pytest.raises(RuntimeError, match="died on card 2"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)
    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 1

    monkeypatch.setattr(orchestrator_module, "render_approval_stage_card", real_render)
    _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup()
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    manifests = store.list_system_events_by_type("signal_dispatch_manifest")
    assert len(manifests) == 1
    assert store.load_system_event_payload_by_duplicate_key(manifest_key) == manifests[0][
        "payload"
    ]
    assert len(store.list_system_events_by_type("telegram_approval_pending")) == 2
    assert len(store.list_system_events_by_type("signal_approval_pending")) == 1
    assert len(store.list_system_events_by_type("dispatch_group_capacity_blocked")) == 0

    # Now that it has fully landed, a further call is an ordinary settled
    # dispatch -- refused, not silently re-run.
    with pytest.raises(ValueError, match="already consumed"):
        _dispatch_orchestrator_with_capacity(
            config, FakeTelegramClient(), _capacity_lookup()
        ).dispatch_signal_approval(signal_summary.signal_run_id)


def _envelope(
    *,
    signal_run_id: str = "signal-1",
    source_strategy_ids: list[str],
    account_ids: list[str] | None = None,
    orders: list[dict] | None = None,
    duplicate_key: str = "k",
) -> PendingApprovalEnvelope:
    return PendingApprovalEnvelope(
        approval_id="a1",
        run_id="run-1",
        signal_run_id=signal_run_id,
        request=ApprovalRequest(
            approval_id="a1",
            run_id="run-1",
            profile_name="p",
            created_at=utc_now(),
            expires_at=utc_now(),
            channel="telegram",
            source_strategy_ids=source_strategy_ids,
            order_count=len(orders or []),
            estimated_notional=0.0,
            proposed_orders=[],
            risk_violations=[],
        ),
        orders=orders or [],
        message="m",
        source_strategy_ids=source_strategy_ids,
        account_ids=account_ids or [],
        reminder_seconds=[],
        created_at=utc_now(),
        expires_at=utc_now(),
        duplicate_key=duplicate_key,
    )


def test_an_envelope_from_another_group_is_refused_rather_than_adopted(tmp_path):
    # Adopting it would show the operator a card for orders they were never
    # sent, with the approval buttons bound to it.
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    envelope = _envelope(
        signal_run_id="signal-other",
        source_strategy_ids=["other"],
        duplicate_key='dispatch-group:signal-1:["mine"]',
    )

    with pytest.raises(ValueError, match="does not match the group"):
        orchestrator._verify_reused_envelope(envelope, "signal-1", ["mine"], [])


def test_a_matching_envelope_passes_verification_whatever_order_it_records(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    order = _order("o1", ["a", "b"], account_id="acct-1")
    envelope = _envelope(
        source_strategy_ids=["b", "a"],
        account_ids=["acct-1"],
        orders=[order.model_dump(mode="json")],
    )

    orchestrator._verify_reused_envelope(envelope, "signal-1", ["a", "b"], [order])


def test_an_envelope_with_a_different_account_id_is_refused(tmp_path):
    # A key written by a different code path (or altered by hand) could match
    # on strategies alone while binding the approval buttons to orders in a
    # different account than the ones this group actually built.
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    order = _order("o1", ["a"], account_id="acct-real")
    envelope = _envelope(
        source_strategy_ids=["a"],
        account_ids=["acct-other"],
        orders=[order.model_dump(mode="json")],
    )

    with pytest.raises(ValueError, match="does not match the group"):
        orchestrator._verify_reused_envelope(envelope, "signal-1", ["a"], [order])


def test_an_envelope_with_a_different_order_id_is_refused(tmp_path):
    # Same strategies and account, but the stored envelope's orders are not
    # the ones this group actually built -- adopting it would bind the
    # approval buttons to the wrong orders.
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    stored_order = _order("o-stored", ["a"], account_id="acct-1")
    real_order = _order("o-real", ["a"], account_id="acct-1")
    envelope = _envelope(
        source_strategy_ids=["a"],
        account_ids=["acct-1"],
        orders=[stored_order.model_dump(mode="json")],
    )

    with pytest.raises(ValueError, match="does not match the group"):
        orchestrator._verify_reused_envelope(envelope, "signal-1", ["a"], [real_order])


def test_an_envelope_with_an_extra_order_is_refused(tmp_path):
    # Same order ids as a prefix, but the stored envelope carries one more --
    # a subset/superset mismatch must not pass as "close enough".
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    order_1 = _order("o1", ["a"], account_id="acct-1")
    order_2 = _order("o2", ["a"], account_id="acct-1")
    envelope = _envelope(
        source_strategy_ids=["a"],
        account_ids=["acct-1"],
        orders=[order_1.model_dump(mode="json"), order_2.model_dump(mode="json")],
    )

    with pytest.raises(ValueError, match="does not match the group"):
        orchestrator._verify_reused_envelope(envelope, "signal-1", ["a"], [order_1])


def test_an_envelope_missing_a_capacity_blocked_order_still_passes(tmp_path):
    # Priority 2: a group's envelope can legitimately hold fewer orders than
    # the group's full (capacity-independent) manifest membership, when some
    # of the group's orders were capacity-blocked at the time the envelope
    # was created. Verification is called with the *manifest's* full order
    # set (capacity-independent), so a genuine subset must still pass -- only
    # an envelope naming an order outside that set is a mismatch.
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    order_1 = _order("o1", ["a"], account_id="acct-1")
    order_2 = _order("o2", ["a"], account_id="acct-1")
    envelope = _envelope(
        source_strategy_ids=["a"],
        account_ids=["acct-1"],
        orders=[order_1.model_dump(mode="json")],
    )

    orchestrator._verify_reused_envelope(envelope, "signal-1", ["a"], [order_1, order_2])


def test_disposition_is_valid_when_envelope_and_blocked_exactly_partition_the_manifest(
    tmp_path,
):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    orchestrator._verify_group_disposition(
        "group-1",
        manifest_order_ids={"o1", "o2"},
        envelope_order_ids={"o1"},
        blocked_order_ids={"o2"},
    )


def test_disposition_fails_loudly_when_a_manifest_order_has_no_disposition(tmp_path):
    # The envelope alone proves it has no unknown order -- it does not prove
    # the manifest order missing from it (o2) was ever durably accounted
    # for as blocked. That must fail loudly, not be silently accepted as
    # "capacity must have blocked it".
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    with pytest.raises(ValueError, match="no disposition"):
        orchestrator._verify_group_disposition(
            "group-1",
            manifest_order_ids={"o1", "o2"},
            envelope_order_ids={"o1"},
            blocked_order_ids=set(),
        )


def test_disposition_fails_loudly_when_the_envelope_names_an_order_outside_the_manifest(
    tmp_path,
):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    with pytest.raises(ValueError, match="not in the manifest"):
        orchestrator._verify_group_disposition(
            "group-1",
            manifest_order_ids={"o1"},
            envelope_order_ids={"o1", "o-foreign"},
            blocked_order_ids=set(),
        )


def test_disposition_fails_loudly_when_an_order_is_both_approved_and_blocked(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    with pytest.raises(ValueError, match="both"):
        orchestrator._verify_group_disposition(
            "group-1",
            manifest_order_ids={"o1", "o2"},
            envelope_order_ids={"o1", "o2"},
            blocked_order_ids={"o2"},
        )


def test_disposition_is_valid_for_a_fully_blocked_group(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    orchestrator._verify_group_disposition(
        "group-1",
        manifest_order_ids={"o1", "o2"},
        envelope_order_ids=set(),
        blocked_order_ids={"o1", "o2"},
    )


def test_disposition_fails_loudly_when_a_blocked_order_is_outside_the_manifest(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)

    with pytest.raises(ValueError, match="not in the manifest"):
        orchestrator._verify_group_disposition(
            "group-1",
            manifest_order_ids={"o1"},
            envelope_order_ids=set(),
            blocked_order_ids={"o1", "o-foreign"},
        )


def _order(order_id: str, source_strategy_ids: list[str], *, account_id: str = "acct-1"):
    return OrderIntent(
        order_id=order_id,
        symbol="MOCK_ETF_A",
        side=OrderSide.BUY,
        quantity=1.0,
        price=100.0,
        notional=100.0,
        account_id=account_id,
        metadata={"source_strategy_ids": source_strategy_ids},
    )


def test_approval_order_groups_merges_the_same_strategies_in_a_different_order(tmp_path):
    # dispatch_group_id canonicalizes source_strategy_ids (sorted, deduped).
    # If _approval_order_groups uses raw list identity as its grouping key
    # instead, two orders naming the same strategies in a different order
    # split into two in-memory groups that both durably collide on the same
    # dispatch_group_id -- the second group's write would be evaluated
    # against the first group's stored envelope.
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    forward = _order("o1", ["a", "b"])
    backward = _order("o2", ["b", "a"])

    groups = orchestrator._approval_order_groups([forward, backward], {})

    assert len(groups) == 1
    source_strategy_ids, group_orders = groups[0]
    assert {order.order_id for order in group_orders} == {"o1", "o2"}
    assert dispatch_group_id("signal-1", source_strategy_ids) == dispatch_group_id(
        "signal-1", ["a", "b"]
    )


def test_approval_order_groups_merges_duplicate_strategy_ids_within_one_order(tmp_path):
    config = _live_signal_config(tmp_path, "expired")
    orchestrator = MaestroOrchestrator(config)
    duped = _order("o1", ["a", "a", "b"])
    plain = _order("o2", ["a", "b"])

    groups = orchestrator._approval_order_groups([duped, plain], {})

    assert len(groups) == 1
    _, group_orders = groups[0]
    assert {order.order_id for order in group_orders} == {"o1", "o2"}


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


def test_approve_signal_retry_cannot_resubmit_after_fill_changes_baseline(
    monkeypatch,
    tmp_path,
):
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
    capacity_lookup = lambda order: BrokerBuyingPower(  # noqa: E731
        symbol=order.symbol,
        order_price=order.price,
        cash_buying_power=1_000_000_000,
        max_buy_quantity=1_000_000,
        source="test",
    )
    first = MaestroOrchestrator(
        config,
        live_order_client=live_client,
        live_order_status_client=FilledStatusClient(),
        broker_reconciliation_service=PassingBrokerReconciliation(),
        order_capacity_lookup=capacity_lookup,
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
        order_capacity_lookup=capacity_lookup,
    )
    second.approval_manager = ApprovingTelegramApprovalManager()

    with pytest.raises(ValueError, match="broker snapshot changed.*cash_changed"):
        second.approve_signal(signal_summary.signal_run_id)

    assert live_client.submit_count == 1
    intent_events = store.list_system_events_by_type("live_order_submit_intent", limit=10)
    submitted_request = intent_events[0]["payload"]["request"]
    assert submitted_request["duplicate_key"] == build_live_order_idempotency_key(
        signal_run_id=signal_summary.signal_run_id,
        account_id=submitted_request["account_id"],
        order_intent_id=submitted_request["order_id"],
        fallback_run_id=submitted_request["run_id"],
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

    def capturing_run_signal(self, strategy_ids=None, **kwargs):
        captured["strategy_ids"] = strategy_ids
        captured["contribution_override"] = kwargs.get("contribution_override")
        return original_run_signal(self, strategy_ids=strategy_ids, **kwargs)

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


def test_daily_signal_approval_does_not_refresh_unrelated_readonly_profile(
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

    assert result.exit_code == 0, result.output
    assert "readonly refresh failed" not in result.output
    assert "telegram_daily_failure" not in result.output


def test_daily_signal_approval_ignores_unrelated_readonly_reconciliation(
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

    assert result.exit_code == 0, result.output
    assert "reconciliation=failed" not in result.output
    assert "telegram_daily_failure" not in result.output


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
        if logical_account_id and state_store.load_latest_account_portfolio_state(
            logical_account_id
        ) is None:
            baseline = PortfolioState(
                cash=10_000_000.0,
                cash_by_currency={"KRW": 10_000_000.0},
                positions={},
            )
            state_store.save_portfolio_snapshot(
                "run_mock_ledger_baseline",
                baseline,
                account_id=logical_account_id,
            )
            state_store.save_portfolio_snapshot("run_mock_ledger_baseline", baseline)

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        portfolio = self.state_store.load_latest_account_portfolio_state(
            self.logical_account_id
        )
        cash = portfolio.cash if portfolio is not None else 10_000_000.0
        account = KISAccountSnapshot(
            account_id=self.config.account_id or "MOCK-LIVE",
            cash=cash,
            cash_by_currency={"KRW": cash},
            buying_power=cash,
            positions=[
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "average_price": 100.0,
                    "current_price": 100.0,
                    "currency": "KRW",
                }
                for symbol, quantity in (portfolio.positions if portfolio else {}).items()
            ],
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


class EnvelopeReturningTelegramClient(FakeTelegramClient):
    """Returns what the real Bot API returns: {"ok": ..., "result": {...}}."""

    def __init__(self) -> None:
        super().__init__()
        self.next_message_id = 7000

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        super().send_message(chat_id, text, reply_markup)
        self.next_message_id += 1
        return {"ok": True, "result": {"message_id": self.next_message_id}}


class RefusingTelegramClient(FakeTelegramClient):
    def send_message(self, chat_id: int, text: str, reply_markup=None):
        raise TelegramApiRejected("Telegram Bot API returned not ok for method: sendMessage")


def test_dispatch_marks_the_envelope_as_lifecycle_owned(monkeypatch, tmp_path):
    """The approval card is owned by the lifecycle from birth.

    Sending it here without recording a message_id is what forced the sweep to
    post its own second card: two button-bearing cards for one approval, only
    one of which ever updates.
    """
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    config.approval.timeout_seconds = 600
    signal_summary = MaestroOrchestrator(config).run_signal()
    client = EnvelopeReturningTelegramClient()

    MaestroOrchestrator(
        config,
        telegram_client=client,
        order_capacity_lookup=lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000_000,
            max_buy_quantity=100_000,
            source="fake",
        ),
    ).dispatch_signal_approval(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    approval_id = store.list_system_events_by_type("telegram_approval_pending")[0]["payload"][
        "approval_id"
    ]
    assert (
        store.list_system_events_by_type("telegram_approval_pending")[0]["payload"][
            "card_delivery_version"
        ]
        == 1
    ), "sweep이 '전송 전에 죽은 승인'을 알아보려면 이 표시가 있어야 한다"
    copies = store.load_card_delivery_state(f"approval:{approval_id}")
    assert [copy["chat_id"] for copy in copies] == [100]
    assert copies[0]["delivery"] == "confirmed"
    assert copies[0]["message_id"] == client.next_message_id
    assert copies[0]["stage"] == "pending"


def test_dispatch_still_fails_loudly_when_telegram_refuses_every_chat(monkeypatch, tmp_path):
    """An approval nobody can see must not be reported as dispatched.

    Only an explicit rejection counts: a timeout leaves the card ambiguous, and
    treating that as failure is what would resend it.
    """
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "expired")
    config.approval.provider = "telegram"
    config.approval.telegram_allowed_chat_ids = [100]
    config.approval.whitelisted_user_ids = [100]
    config.approval.timeout_seconds = 600
    signal_summary = MaestroOrchestrator(config).run_signal()

    with pytest.raises(RuntimeError, match="refused the approval card"):
        MaestroOrchestrator(
            config,
            telegram_client=RefusingTelegramClient(),
            order_capacity_lookup=lambda order: BrokerBuyingPower(
                symbol=order.symbol,
                order_price=order.price,
                cash_buying_power=10_000_000,
                max_buy_quantity=100_000,
                source="fake",
            ),
        ).dispatch_signal_approval(signal_summary.signal_run_id)
