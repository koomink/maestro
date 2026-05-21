from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("ataraxia.strategy")

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.core.ids import new_run_id
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataBundle, DataRequest
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_ataraxia_live_approval_reaches_telegram_approval_and_live_order_lifecycle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "maestro.orchestration.orchestrator.utc_now",
        lambda: datetime(2026, 5, 15, 1, 0, tzinfo=UTC),
    )
    config = load_config(_config_path(tmp_path))
    live_client = FakeLiveOrderClient()
    status_client = FakeStatusClient(live_client)
    telegram_client = FakeTelegramClient()
    orchestrator = MaestroOrchestrator(
        config,
        live_order_client=live_client,
        live_order_status_client=status_client,
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=telegram_client,
    )
    orchestrator.datahub = AtaraxiaFakeDataHub()
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        PortfolioState(cash=10_000_000.0, cash_by_currency={"KRW": 10_000_000.0}, positions={}),
    )
    broker_snapshot_id = _save_broker_snapshot(orchestrator.state_store)
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id)

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert any("Maestro Approval" in message["text"] for message in telegram_client.sent_messages)
    assert len(live_client.requests) == 2
    assert {request.symbol for request in live_client.requests} == {
        "TIGER_NASDAQ100_LEVERAGE",
        "KODEX_US_DIVIDEND_DOWJONES",
    }
    assert {request.side for request in live_client.requests} == {OrderSide.BUY}
    assert {request.sleeve for request in live_client.requests} == {"KRW"}
    approvals = orchestrator.state_store.list_approvals()
    assert approvals[0]["payload"]["decision"]["status"] == "approved"
    assert approvals[0]["payload"]["request"]["source_strategy_ids"] == ["ataraxia"]
    risk_decisions = orchestrator.state_store.list_risk_decisions()
    assert risk_decisions[0]["payload"]["approved"] is True
    assert "modifications" not in risk_decisions[0]["payload"]
    lifecycles = orchestrator.state_store.list_system_events_by_type("live_order_lifecycle")
    assert len(lifecycles) == 2
    assert {row["payload"]["final_status"] for row in lifecycles} == {"filled"}
    fills = orchestrator.state_store.list_system_events_by_type("fill_reconciliation")
    assert any(row["payload"]["applied_fills"] for row in fills)


class AtaraxiaFakeDataHub:
    prices = {
        "TIGER_NASDAQ100_LEVERAGE": 100_000.0,
        "KODEX_US_DIVIDEND_DOWJONES": 10_000.0,
    }

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        now = utc_now()
        data = {
            request.symbol: {
                "latest_price": {
                    "symbol": request.symbol,
                    "timestamp": now.isoformat(),
                    "price": self.prices[request.symbol],
                    "source": "fake",
                },
                "is_stale": False,
            }
            for request in requests
        }
        return DataBundle(requests=requests, data=data, generated_at=now, source="fake")


class FakeTelegramClient:
    def __init__(self) -> None:
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
        del offset, timeout_seconds
        callback_data = "approve:unknown"
        reply_markup = self.sent_messages[-1]["reply_markup"]
        keyboard = reply_markup.get("inline_keyboard") or []
        if keyboard and keyboard[0]:
            callback_data = keyboard[0][0]["callback_data"]
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": callback_data,
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


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []
        self.requests_by_broker_order_id: dict[str, LiveOrderRequest] = {}

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        broker_order = BrokerOrderId(
            broker="fake",
            broker_order_id=f"broker:{request.order_id}",
            order_id=request.order_id,
            broker_order_org_no="KRX",
            submitted_at=utc_now().isoformat(),
            broker_product=request.broker_product,
        )
        self.requests_by_broker_order_id[broker_order.broker_order_id] = request
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=broker_order,
        )


class FakeStatusClient(LiveOrderStatusClient):
    def __init__(self, live_client: FakeLiveOrderClient) -> None:
        self.live_client = live_client

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        request = self.live_client.requests_by_broker_order_id[
            broker_order_id.broker_order_id
        ]
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=OrderStatus.FILLED,
            checked_at=utc_now().isoformat(),
            symbol=request.symbol,
            side=request.side,
            partial_fill=PartialFillSummary(
                ordered_quantity=request.quantity,
                filled_quantity=request.quantity,
                remaining_quantity=0.0,
                average_fill_price=request.limit_price,
                fill_count=1,
            ),
        )


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id=new_run_id(),
            passed=True,
            checked_at=utc_now().isoformat(),
            issues=[],
            cash_difference=0.0,
            broker_account_id="MOCK",
            tolerances={
                "cash_tolerance": 0.0,
                "position_quantity_tolerance": 0.0,
                "value_tolerance": 0.0,
            },
        )


def _config_path(tmp_path) -> Path:
    raw = yaml.safe_load(
        Path("configs/examples/live_approval_ataraxia_kis_paper_trading.yaml").read_text()
    )
    raw["datahub"] = {"provider": "mock"}
    raw["execution"]["order_posture"] = "armed"
    raw["execution"]["market_session"]["required"] = False
    raw["execution"]["live_order_limits"]["max_order_notional"] = 2_000_000
    raw["execution"]["live_order_limits"]["max_daily_notional"] = 3_500_000
    raw["execution"]["live_order_limits"]["daily_loss_limit"] = None
    raw["execution"]["order_status_poll_interval_seconds"] = 0.0
    raw["execution"]["order_status_max_polls"] = 1
    raw["reconciliation"] = {"max_age_seconds": 999_999_999}
    raw["approval"]["timeout_seconds"] = 1
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    raw["approval"]["telegram_poll_interval_seconds"] = 0.0
    raw["kis"]["provider"] = "mock"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "ataraxia_live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _save_broker_snapshot(store: StateStore) -> int:
    current_prices = AtaraxiaFakeDataHub.prices
    store.save_broker_account_snapshot(
        "run_broker_snapshot",
        "MOCK",
        {
            "account": {
                "account_id": "MOCK",
                "cash": 10_000_000.0,
                "cash_by_currency": {"KRW": 10_000_000.0},
                "buying_power": 10_000_000.0,
                "positions": [],
                "daily_pnl": 0.0,
            },
            "current_prices": current_prices,
            "order_fills": [],
            "unfilled_orders": [],
        },
    )
    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    return int(latest["id"])


def _save_passed_reconciliation(store: StateStore, broker_snapshot_id: int) -> None:
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {
            "passed": True,
            "issues": [],
            "broker_snapshot_id": broker_snapshot_id,
        },
    )
