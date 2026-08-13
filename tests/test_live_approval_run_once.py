import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISBuyingPower,
    KISCashBalance,
    KISReadOnlySnapshot,
)
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderLifecycleNotification,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class FakeTelegramClient:
    def __init__(self, approval_id: str, command: str = "approve") -> None:
        self.approval_id = approval_id
        self.command = command
        self.sent_messages: list[dict[str, Any]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        if self.command == "none":
            return {"ok": True, "result": []}
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": f"{self.command}:{self.approval_id}",
                        "message": {
                            "chat": {"id": 100},
                            "message_id": 1,
                            "text": "Maestro approval request",
                        },
                        "from": {"id": 100, "username": "approver"},
                    },
                }
            ],
        }


def _kis_snapshot(
    *,
    account_id: str,
    cash: float,
    symbols: list[str],
) -> KISReadOnlySnapshot:
    account = KISAccountSnapshot(
        account_id=account_id,
        cash=cash,
        cash_by_currency={"USD": cash},
        buying_power=cash,
        positions=[],
        cash_balance=KISCashBalance(cash=cash, withdrawable_cash=cash),
        buying_power_detail=KISBuyingPower(
            cash_buying_power=cash,
            source="kis_mock",
        ),
        fetched_at=utc_now(),
        source="kis_mock",
    )
    return KISReadOnlySnapshot(
        account=account,
        current_prices={symbol: 100.0 for symbol in symbols if symbol != "CASH"},
        order_fills=[],
        unfilled_orders=[],
    )


@pytest.fixture(autouse=True)
def _mock_kis_broker_snapshot(monkeypatch) -> None:
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
        self.client = client
        self.logical_account_id = logical_account_id

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        return _kis_snapshot(
            account_id=self.config.account_id or "MOCK",
            cash=1000.0,
            symbols=symbols,
        )

    monkeypatch.setattr(KISReadOnlyService, "__init__", init_service)
    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)


def _seed_broker_baseline(
    store: StateStore,
    *,
    cash: float,
    positions: dict[str, float] | None = None,
) -> None:
    state = PortfolioState(cash=cash, positions=positions or {})
    store.save_portfolio_snapshot("run_adopted_broker_baseline", state)
    store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        state,
        account_id="default_kis",
    )


def _save_kis_snapshot_as_broker_snapshot(
    service: KISReadOnlyService,
    snapshot: KISReadOnlySnapshot,
) -> None:
    payload = {
        "account": snapshot.account.model_dump(mode="json"),
        "current_prices": snapshot.current_prices,
        "order_fills": [],
        "unfilled_orders": [],
    }
    logical_account_id = getattr(service, "logical_account_id", None)
    if logical_account_id:
        payload["account_id"] = logical_account_id
        payload["broker_account_id"] = snapshot.account.account_id
    service.state_store.save_broker_account_snapshot(
        "run_kis_snapshot",
        snapshot.account.account_id,
        payload,
    )


def test_run_once_live_approval_uses_lifecycle_with_fake_clients(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_live_run_once"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    del raw["portfolio"]["initial_cash"]
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": True,
        "require_reconciliation_pass": True,
        "live_order_limits": {
            "max_order_notional": 5_000_000.0,
            "max_daily_notional": 10_000_000.0,
        },
        "order_status_max_polls": 1,
        "order_status_poll_interval_seconds": 0.0,
    }
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "timeout_seconds": 1,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["kis"] = {"enabled": True, "provider": "mock", "account_id": "MOCK"}
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    submit_client = FakeLiveOrderClient()
    status_client = FakeStatusClient()
    notification_client = FakeNotificationClient()
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        live_order_client=submit_client,
        live_order_status_client=status_client,
        live_order_notification_client=notification_client,
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=10_000_000.0)

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["order_prices"]["MOCK_ETF_A"] == 100.0
    assert proposal_snapshot["order_prices"]["MOCK_ETF_B"] == 50.0
    assert len(proposal_snapshot["proposed_orders"]) == 2
    assert len(submit_client.requests) == 2
    assert len(status_client.requests) == 2
    assert {request.order_type.value for request in submit_client.requests} == {"limit"}
    assert notification_client.events[-1].status == OrderStatus.FILLED
    assert orchestrator.state_store.list_system_events_by_type("live_order_lifecycle")


def test_live_approval_order_generation_uses_broker_quote_snapshot(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_live_quote_run_once"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    del raw["portfolio"]["initial_cash"]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": False,
        "live_order_dry_run": True,
        "require_reconciliation_pass": False,
        "broker_validation": {"require_quote_validation": True},
        "live_order_limits": {
            "max_order_notional": 5_000_000.0,
            "max_daily_notional": 10_000_000.0,
            "max_daily_order_count": 3,
        },
    }
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "timeout_seconds": 1,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["kis"] = {"enabled": True, "provider": "mock", "account_id": "MOCK"}
    config_path = tmp_path / "live_approval_broker_quote.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_broker_account_snapshot(
        "run_broker_snapshot",
        "MOCK",
        {
            "account": {
                "account_id": "MOCK",
                "cash": 10_000_000.0,
                "buying_power": 10_000_000.0,
                "positions": [],
            },
            "current_prices": {
                "MOCK_ETF_A": 123.0,
                "MOCK_ETF_B": 47.5,
            },
        },
    )
    _seed_broker_baseline(orchestrator.state_store, cash=10_000_000.0)

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["order_prices"] == {
        "MOCK_ETF_A": 123.0,
        "MOCK_ETF_B": 47.5,
    }
    dry_run_requests = [
        row["payload"]["request"]
        for row in orchestrator.state_store.list_system_events_by_type("live_order_dry_run")
    ]
    assert {request["limit_price"] for request in dry_run_requests} == {123.0, 47.5}


def test_live_approval_order_generation_fills_position_prices_from_broker_snapshot(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_live_position_price_run_once"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    del raw["portfolio"]["initial_cash"]
    raw["portfolio"]["allowed_symbols"] = [
        "CASH",
        "MOCK_ETF_A",
        "MOCK_ETF_B",
        "MOCK_LEGACY",
    ]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": False,
        "live_order_dry_run": True,
        "require_reconciliation_pass": False,
        "broker_validation": {"require_quote_validation": False},
        "live_order_limits": {
            "max_order_notional": 5_000_000.0,
            "max_daily_notional": 10_000_000.0,
            "max_daily_order_count": 3,
        },
    }
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "timeout_seconds": 1,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["kis"] = {"enabled": True, "provider": "mock", "account_id": "MOCK"}
    config_path = tmp_path / "live_approval_position_price.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_broker_account_snapshot(
        "run_broker_snapshot",
        "MOCK",
        {
            "account": {
                "account_id": "MOCK",
                "cash": 10_000_000.0,
                "buying_power": 10_000_000.0,
                "positions": [
                    {
                        "symbol": "MOCK_LEGACY",
                        "quantity": 1.0,
                        "average_price": 700.0,
                        "current_price": 777.0,
                    }
                ],
            },
            "current_prices": {},
        },
    )
    _seed_broker_baseline(
        orchestrator.state_store,
        cash=10_000_000.0,
        positions={"MOCK_LEGACY": 1.0},
    )

    summary = orchestrator.run_once()

    # Two target legs plus the exit of MOCK_LEGACY, which the account holds and
    # today's target dropped. Pricing it from the broker snapshot is what makes
    # both its valuation and its exit possible.
    assert summary.orders_created == 3
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["prices"]["MOCK_LEGACY"] == 777.0


def test_live_approval_refresh_does_not_persist_signal_portfolio_state(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_refresh_cash"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        return _kis_snapshot(
            account_id=self.config.account_id or "MOCK",
            cash=900.0,
            symbols=symbols,
        )

    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)
    config_path = _overseas_live_approval_config(tmp_path, dry_run=True)
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=1000.0)

    summary = orchestrator.run_once()

    dry_run = orchestrator.state_store.list_system_events_by_type("live_order_dry_run")[0][
        "payload"
    ]
    latest_state = orchestrator.state_store.load_latest_portfolio_state()
    adopted = orchestrator.state_store.list_system_events_by_type("broker_snapshot_adopted")[0][
        "payload"
    ]
    assert summary.cash == 1000.0
    assert latest_state.cash == 1000.0
    assert dry_run["request"]["quantity"] == 2.0
    assert adopted["cash"] == 1000.0


def test_live_approval_run_once_auto_reconciles_refreshed_broker_snapshot(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_auto_reconcile"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        snapshot = _kis_snapshot(
            account_id=self.config.account_id or "MOCK",
            cash=900.0,
            symbols=symbols,
        )
        _save_kis_snapshot_as_broker_snapshot(self, snapshot)
        return snapshot

    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)
    config_path = _overseas_live_approval_config(tmp_path, dry_run=True)
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_system_event(
        "run_old_reconcile",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=900.0)
    with sqlite3.connect(orchestrator.state_store.path) as conn:
        conn.execute(
            "UPDATE system_events SET created_at = ? WHERE event_type = ?",
            ("2026-05-20 00:00:00", "broker_reconciliation"),
        )

    summary = orchestrator.run_once()

    latest_reconciliation = orchestrator.state_store.load_latest_system_event(
        "broker_reconciliation"
    )
    assert latest_reconciliation is not None
    assert summary.orders_created == 1
    assert latest_reconciliation["run_id"] == summary.run_id
    assert latest_reconciliation["payload"]["passed"] is True
    assert orchestrator.state_store.list_system_events_by_type("broker_reconciliation_halt") == []


def test_live_approval_run_once_auto_reconciliation_failure_blocks_approval(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_auto_reconcile_fail"
    telegram_client = FakeTelegramClient(approval_id)
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        snapshot = _kis_snapshot(
            account_id=self.config.account_id or "MOCK",
            cash=900.0,
            symbols=symbols,
        )
        _save_kis_snapshot_as_broker_snapshot(self, snapshot)
        return snapshot

    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)
    config_path = _overseas_live_approval_config(tmp_path, dry_run=True)
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        telegram_client=telegram_client,
    )
    _seed_broker_baseline(orchestrator.state_store, cash=800.0)

    summary = orchestrator.run_once()

    reconciliation = orchestrator.state_store.load_latest_system_event("broker_reconciliation")
    halt = orchestrator.state_store.list_system_events_by_type("broker_reconciliation_halt")[0]
    assert reconciliation is not None
    assert summary.orders_created == 0
    assert reconciliation["run_id"] == summary.run_id
    assert reconciliation["payload"]["passed"] is False
    assert reconciliation["payload"]["issues"][0]["issue_type"] == "cash_mismatch"
    assert halt["payload"]["reason"] == "failed_reconciliation"
    assert telegram_client.sent_messages == []


def test_live_approval_run_once_fails_closed_when_broker_refresh_fails(
    tmp_path,
    monkeypatch,
):
    def fail_refresh(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        raise ValueError("KIS snapshot unavailable")

    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fail_refresh)
    config_path = _overseas_live_approval_config(tmp_path, dry_run=True)
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        telegram_client=FakeTelegramClient("appr_missing_baseline"),
    )

    with pytest.raises(ValueError, match="could not refresh broker snapshot"):
        orchestrator.run_once()

    events = orchestrator.state_store.list_system_events_by_type("broker_baseline_required")
    assert events[0]["payload"]["mode"] == "live_approval"
    assert events[0]["payload"]["error"] == "KIS snapshot unavailable"


@pytest.mark.parametrize(
    ("statuses", "expected_final_status", "expected_applied_fills"),
    [
        ([OrderStatus.FILLED], OrderStatus.FILLED, 1),
        ([OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED], OrderStatus.FILLED, 2),
        ([OrderStatus.REJECTED], OrderStatus.REJECTED, 0),
        ([OrderStatus.UNKNOWN], OrderStatus.HALTED, 0),
    ],
)
def test_run_once_live_approval_kis_overseas_e2e_status_paths(
    tmp_path,
    monkeypatch,
    statuses,
    expected_final_status,
    expected_applied_fills,
):
    approval_id = "appr_kis_overseas"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    config_path = _overseas_live_approval_config(tmp_path, max_polls=len(statuses))
    submit_client = FakeLiveOrderClient()
    status_client = FakeStatusClient(statuses=statuses, symbol="AAPL", side=OrderSide.BUY)
    notification_client = FakeNotificationClient()
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        live_order_client=submit_client,
        live_order_status_client=status_client,
        live_order_notification_client=notification_client,
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=1000.0)

    summary = orchestrator.run_once()

    lifecycle = orchestrator.state_store.list_system_events_by_type("live_order_lifecycle")[0][
        "payload"
    ]
    assert summary.orders_created == 1
    assert submit_client.requests[0].symbol == "AAPL"
    assert submit_client.requests[0].quantity == 2.0
    assert len(status_client.requests) == len(statuses)
    assert lifecycle["final_status"] == expected_final_status.value
    assert len(lifecycle["applied_fills"]) == expected_applied_fills


def test_run_once_live_approval_rejected_telegram_decision_skips_kis_submit(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_reject"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    submit_client = FakeLiveOrderClient()
    orchestrator = MaestroOrchestrator(
        load_config(_overseas_live_approval_config(tmp_path)),
        live_order_client=submit_client,
        live_order_status_client=FakeStatusClient(statuses=[OrderStatus.FILLED]),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id, command="reject"),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=1000.0)

    summary = orchestrator.run_once()

    assert summary.orders_created == 1
    assert submit_client.requests == []
    assert (
        orchestrator.state_store.list_system_events_by_type("execution_skipped")[0]["payload"][
            "approval_status"
        ]
        == "rejected"
    )


def test_run_once_live_approval_expired_telegram_decision_skips_kis_submit(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_timeout"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    monkeypatch.setattr(
        "maestro.integrations.telegram.bot.utc_now",
        lambda: utc_now().replace(year=2099),
    )
    submit_client = FakeLiveOrderClient()
    orchestrator = MaestroOrchestrator(
        load_config(_overseas_live_approval_config(tmp_path)),
        live_order_client=submit_client,
        live_order_status_client=FakeStatusClient(statuses=[OrderStatus.FILLED]),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id, command="none"),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=1000.0)

    summary = orchestrator.run_once()

    assert summary.orders_created == 1
    assert submit_client.requests == []
    assert (
        orchestrator.state_store.list_system_events_by_type("execution_skipped")[0]["payload"][
            "approval_status"
        ]
        == "expired"
    )


def test_run_once_live_approval_dry_run_records_payload_without_kis_submit(
    tmp_path,
    monkeypatch,
):
    approval_id = "appr_kis_dry_run"
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: approval_id)
    submit_client = FakeLiveOrderClient()
    orchestrator = MaestroOrchestrator(
        load_config(_overseas_live_approval_config(tmp_path, dry_run=True)),
        live_order_client=submit_client,
        live_order_status_client=FakeStatusClient(statuses=[OrderStatus.FILLED]),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(orchestrator.state_store, cash=1000.0)

    summary = orchestrator.run_once()

    dry_run = orchestrator.state_store.list_system_events_by_type("live_order_dry_run")[0][
        "payload"
    ]
    assert summary.orders_created == 1
    assert submit_client.requests == []
    assert dry_run["broker_submit_skipped"] is True
    assert dry_run["request"]["symbol"] == "AAPL"
    assert dry_run["approval_decision"]["status"] == "approved"
    assert orchestrator.state_store.list_orders() == []
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["order_prices"] == {"AAPL": 100.0}
    assert proposal_snapshot["proposed_orders"][0]["symbol"] == "AAPL"
    assert orchestrator.state_store.list_system_events_by_type("live_order_lifecycle") == []


def test_live_smoke_live_dry_run_records_payload_without_broker_submit(tmp_path):
    config_path = _overseas_live_approval_config(tmp_path, dry_run=True)
    raw = yaml.safe_load(config_path.read_text())
    raw["approval"]["provider"] = "console"
    raw["approval"]["default_decision"] = "approved"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )
    _seed_broker_baseline(store, cash=1000.0)

    result = CliRunner().invoke(
        app,
        [
            "live-smoke",
            "--config",
            str(config_path),
            "--check",
            "live-dry-run",
            "--allow-mock",
        ],
    )

    assert result.exit_code == 0
    assert "check=live_dry_run status=ok" in result.output
    assert "symbol=AAPL" in result.output
    assert "notional=200.00" in result.output


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=_broker_order(request),
        )

    def get_buying_power(self, symbol, order_price, currency=None):
        return BrokerBuyingPower(
            symbol=symbol,
            order_price=order_price,
            cash_buying_power=1_000_000_000,
            currency=currency,
            max_buy_quantity=1_000_000,
            source="test",
        )


class FakeStatusClient(LiveOrderStatusClient):
    def __init__(
        self,
        statuses: list[OrderStatus] | None = None,
        *,
        symbol: str = "MOCK_ETF_A",
        side: OrderSide = OrderSide.BUY,
    ) -> None:
        self.requests: list[str] = []
        self.statuses = list(statuses or [OrderStatus.FILLED])
        self.symbol = symbol
        self.side = side

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        self.requests.append(broker_order_id.broker_order_id)
        status = self.statuses[min(len(self.requests) - 1, len(self.statuses) - 1)]
        filled_quantity = 0.0
        remaining_quantity = 1.0
        average_fill_price = None
        fill_count = 0
        if status == OrderStatus.PARTIALLY_FILLED:
            filled_quantity = 1.0
            remaining_quantity = 1.0
            average_fill_price = 100.0
            fill_count = 1
        elif status == OrderStatus.FILLED:
            filled_quantity = 2.0 if self.symbol == "AAPL" else 1.0
            remaining_quantity = 0.0
            average_fill_price = 100.0 if self.symbol == "AAPL" else 1.0
            fill_count = 1
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=status,
            checked_at=utc_now().isoformat(),
            symbol=self.symbol,
            side=self.side,
            partial_fill=PartialFillSummary(
                ordered_quantity=filled_quantity + remaining_quantity,
                filled_quantity=filled_quantity,
                remaining_quantity=remaining_quantity,
                average_fill_price=average_fill_price,
                fill_count=fill_count,
            ),
        )


class FakeNotificationClient(LiveOrderNotificationClient):
    def __init__(self) -> None:
        self.events: list[LiveOrderLifecycleNotification] = []

    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        self.events.append(event)


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id="run_broker_reconcile",
            passed=True,
            checked_at=utc_now().isoformat(),
            issues=[],
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )


def _broker_order(request: LiveOrderRequest) -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id=f"KIS-{request.order_id}",
        order_id=request.order_id,
        submitted_at=utc_now().isoformat(),
    )


def _overseas_live_approval_config(
    tmp_path: Path,
    max_polls: int = 1,
    *,
    dry_run: bool = False,
) -> Path:
    csv_path = tmp_path / "prices.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,symbol,open,high,low,close,volume",
                "2026-05-08T00:00:00+00:00,AAPL,100,100,100,100,1000",
            ]
        )
        + "\n"
    )
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_approval_us_etf.yaml").read_text())
    raw["portfolio"]["allowed_symbols"] = ["CASH", "AAPL"]
    raw["universe"]["instruments"] = [
        {
            "symbol": "CASH",
            "asset_type": "cash",
            "region": "US",
            "currency": "USD",
            "broker": "kis",
            "broker_product": "kis_overseas_stock",
            "broker_symbol": "USD",
            "quantity_step": 0.01,
            "price_tick": 0.01,
            "min_order_quantity": 0.01,
            "min_order_notional": 0,
        },
        {
            "symbol": "AAPL",
            "asset_type": "stock",
            "region": "US",
            "currency": "USD",
            "broker": "kis",
            "broker_product": "kis_overseas_stock",
            "broker_symbol": "AAPL",
            "exchange_code": "NASD",
            "quantity_step": 1,
            "price_tick": 0.01,
            "min_order_quantity": 1,
            "min_order_notional": 1,
        },
    ]
    raw["strategies"] = [
        {
            "id": "sample_static_allocation",
            "enabled": True,
            "weight": 1.0,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            "config": {"allocations": {"CASH": 0.8, "AAPL": 0.2}},
        }
    ]
    raw["datahub"] = {"provider": "csv", "csv_path": str(csv_path)}
    raw["execution"]["order_posture"] = "dry_run" if dry_run else "armed"
    raw["execution"]["live_order_limits"]["max_order_notional"] = 500
    raw["execution"]["live_order_limits"]["max_daily_notional"] = 1000
    raw["execution"]["live_order_limits"]["max_daily_order_count"] = 3
    raw["execution"]["order_status_max_polls"] = max_polls
    raw["execution"]["order_status_poll_interval_seconds"] = 0.0
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["timeout_seconds"] = 1
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    raw["approval"]["telegram_poll_interval_seconds"] = 0.0
    raw["kis"]["account_id"] = "12345678-01"
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    config_path = tmp_path / "live_approval_kis_overseas.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def test_live_execution_entry_points_take_the_live_order_lock_outermost(tmp_path, monkeypatch):
    """approve_signal and run_once both descend into submit_approved_order.

    That takes live_order_lock, so these entry points must take it before
    writer_lock. Holding writer first is the inversion that deadlocks against a
    concurrent reconcile_latest, which takes live_order_lock then writer_lock.
    StateStore raises on the violation, but this pins the acquisition order at
    the entry points themselves so a refactor cannot quietly rely on that.
    """
    orchestrator = MaestroOrchestrator(load_config(_overseas_live_approval_config(tmp_path)))
    lock_order: list[tuple[str, str]] = []
    original_writer_lock = orchestrator.state_store.writer_lock
    original_live_order_lock = orchestrator.state_store.live_order_lock

    @contextmanager
    def recording_writer_lock(owner: str, **kwargs):
        lock_order.append(("writer", owner))
        with original_writer_lock(owner, **kwargs):
            yield

    @contextmanager
    def recording_live_order_lock(owner: str, **kwargs):
        lock_order.append(("live_order", owner))
        with original_live_order_lock(owner, **kwargs):
            yield

    orchestrator.state_store.writer_lock = recording_writer_lock
    orchestrator.state_store.live_order_lock = recording_live_order_lock
    monkeypatch.setattr(MaestroOrchestrator, "_run_once_locked", lambda self: None)
    monkeypatch.setattr(
        MaestroOrchestrator, "_approve_signal_locked", lambda self, signal_run_id: None
    )

    orchestrator.run_once()
    orchestrator.approve_signal("signal_lock_order")

    assert lock_order == [
        ("live_order", "run_once"),
        ("writer", "run_once"),
        ("live_order", "approve_signal"),
        ("writer", "approve_signal"),
    ]
