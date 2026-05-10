from pathlib import Path
from typing import Any

import yaml

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


class FakeTelegramClient:
    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        self.sent_messages: list[dict[str, Any]] = []

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "text": f"approve {self.approval_id}",
                        "chat": {"id": 100},
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
    assert len(submit_client.requests) == 2
    assert len(status_client.requests) == 2
    assert {request.order_type.value for request in submit_client.requests} == {"limit"}
    assert notification_client.events[-1].status == OrderStatus.FILLED
    assert orchestrator.state_store.list_system_events_by_type("live_order_lifecycle")


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
    def __init__(self) -> None:
        self.requests: list[str] = []

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        self.requests.append(broker_order_id.broker_order_id)
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=OrderStatus.FILLED,
            checked_at=utc_now().isoformat(),
            symbol=broker_order_id.order_id.split(":")[0]
            if ":" in broker_order_id.order_id
            else "MOCK_ETF_A",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=1.0,
                filled_quantity=1.0,
                remaining_quantity=0.0,
                average_fill_price=1.0,
                fill_count=1,
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
        broker_order_id=f"KIS-{len(request.order_id)}",
        order_id=request.order_id,
        submitted_at=utc_now().isoformat(),
    )
