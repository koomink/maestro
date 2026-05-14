from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
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
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": True,
        "require_reconciliation_pass": True,
        "max_live_order_notional": 5_000_000.0,
        "max_daily_live_notional": 10_000_000.0,
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
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": False,
        "live_order_dry_run": True,
        "require_reconciliation_pass": False,
        "require_broker_quote_validation": True,
        "max_live_order_notional": 5_000_000.0,
        "max_daily_live_notional": 10_000_000.0,
        "max_daily_live_order_count": 3,
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

    summary = orchestrator.run_once()

    dry_run = orchestrator.state_store.list_system_events_by_type("live_order_dry_run")[0][
        "payload"
    ]
    assert summary.orders_created == 1
    assert submit_client.requests == []
    assert dry_run["broker_submit_skipped"] is True
    assert dry_run["request"]["symbol"] == "AAPL"
    assert dry_run["approval_decision"]["status"] == "approved"
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
    StateStore(config.state.sqlite_path, config.portfolio.initial_cash).save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )

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
    raw = yaml.safe_load(Path("configs/live_approval.example.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1000
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
            "mode": "live_approval",
            "weight": 1.0,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            "config": {"allocations": {"CASH": 0.8, "AAPL": 0.2}},
        }
    ]
    raw["datahub"] = {"provider": "csv", "csv_path": str(csv_path)}
    raw["execution"]["live_order_enabled"] = True
    raw["execution"]["live_order_dry_run"] = dry_run
    raw["execution"]["max_live_order_notional"] = 500
    raw["execution"]["max_daily_live_notional"] = 1000
    raw["execution"]["max_daily_live_order_count"] = 3
    raw["execution"]["order_status_max_polls"] = max_polls
    raw["execution"]["order_status_poll_interval_seconds"] = 0.0
    raw["risk"] = {"max_single_asset_weight": 1.0, "min_cash_weight": 0.0}
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
