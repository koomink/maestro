from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.approval.models import ApprovalDecision
from maestro.config.loader import load_config
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, Currency, OrderSide, OrderStatus
from maestro.core.ids import new_run_id
from maestro.core.instruments import TradableInstrument
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderSafetyService,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataBundle, DataRequest
from maestro.state.store import StateStore


def test_stale_data_blocks_live_approval(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    orchestrator.datahub = FakeDataHub(stale=True)
    orchestrator.state_store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {"passed": True, "issues": []},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    assert orchestrator.live_order_client.requests == []
    event = orchestrator.state_store.list_system_events_by_type("stale_data_halt")[0]
    assert event["payload"]["issues"][0]["symbol"] == "MOCK_ETF_A"
    assert event["payload"]["issues"][0]["reason"] == "stale"


def test_missing_reconciliation_blocks_live_approval(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    assert orchestrator.live_order_client.requests == []
    event = orchestrator.state_store.list_system_events_by_type("broker_reconciliation_halt")[0]
    assert event["payload"]["reason"] == "missing_reconciliation"


def test_failed_reconciliation_blocks_live_approval(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    orchestrator.state_store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {"passed": False, "issues": [{"issue_type": "cash_mismatch"}]},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_reconciliation_halt")[0]
    assert event["payload"]["reason"] == "failed_reconciliation"


def test_daily_notional_limit_blocks_live_approval(tmp_path):
    orchestrator = _live_orchestrator(tmp_path, max_daily_live_notional=100.0)
    _save_passed_reconciliation(orchestrator.state_store)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("live_order_limit_halt")[0]
    assert event["payload"]["reason"] == "daily_notional_exceeded"


def test_daily_order_count_limit_blocks_live_approval(tmp_path):
    orchestrator = _live_orchestrator(tmp_path, max_daily_live_order_count=1)
    _save_passed_reconciliation(orchestrator.state_store)
    orchestrator.state_store.save_system_event(
        "run_existing",
        "live_order_result",
        {"submitted_date": date.today().isoformat(), "notional": 10.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("live_order_limit_halt")[0]
    assert event["payload"]["reason"] == "daily_order_count_exceeded"


def test_instrument_price_tick_and_quantity_step_validation(tmp_path):
    service = LiveOrderSafetyService(
        ExecutionConfig(
            live_order_enabled=True,
            max_live_order_notional=10_000.0,
            max_daily_live_notional=20_000.0,
            max_daily_live_order_count=10,
            require_reconciliation_pass=False,
        ),
        StateStore(str(tmp_path / "state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "audit.jsonl")),
        FakeLiveOrderClient(),
        instruments=[
            TradableInstrument(
                symbol="AAPL",
                asset_type="stock",
                region="US",
                currency="USD",
                broker="kis",
                broker_product="kis_overseas_stock",
                broker_symbol="AAPL",
                exchange_code="NASD",
                quantity_step=1,
                price_tick=0.01,
                min_order_quantity=1,
                min_order_notional=1,
            )
        ],
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        base_currency=Currency.USD,
    )
    approval = _approval("run_live", "appr_live")

    with pytest.raises(ValueError, match="quantity_step"):
        service.submit_approved_order(
            _request(quantity=1.5, limit_price=100.00),
            approval,
        )
    with pytest.raises(ValueError, match="price_tick"):
        service.submit_approved_order(
            _request(quantity=1, limit_price=100.005),
            approval,
        )

    result = service.submit_approved_order(_request(quantity=1, limit_price=100.01), approval)
    assert result.status == OrderStatus.ACCEPTED_BY_BROKER


def test_paper_mode_warns_on_stale_data_and_continues(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    orchestrator = MaestroOrchestrator(load_config(config_path))
    orchestrator.datahub = FakeDataHub(stale=True)

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    events = orchestrator.state_store.list_system_events_by_type("stale_data_warning")
    assert events


class FakeDataHub:
    def __init__(self, stale: bool = False) -> None:
        self.stale = stale
        self.prices = {"CASH": 1.0, "MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0}

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        now = utc_now()
        return DataBundle(
            requests=requests,
            data={
                request.symbol: {
                    "latest_price": {
                        "symbol": request.symbol,
                        "timestamp": now.isoformat(),
                        "price": self.prices[request.symbol],
                        "source": "fake",
                    },
                    "is_stale": self.stale and request.symbol != "CASH",
                }
                for request in requests
            },
            generated_at=now,
            source="fake",
        )


class FakeTelegramClient:
    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        return {"ok": True, "result": {"message_id": 1}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "text": "approve appr_live",
                        "chat": {"id": 100},
                        "from": {"id": 100, "username": "approver"},
                    },
                }
            ],
        }


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=BrokerOrderId(
                broker="fake",
                broker_order_id=f"broker:{request.order_id}",
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
            ),
        )


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def reconcile_latest(self) -> ReconciliationResult:
        return ReconciliationResult(
            run_id=new_run_id(),
            passed=True,
            issues=[],
            cash_difference=0.0,
            value_difference=0.0,
            broker_account_id="MOCK",
        )


def _live_orchestrator(
    tmp_path,
    *,
    max_daily_live_notional: float = 10_000_000.0,
    max_daily_live_order_count: int = 10,
) -> MaestroOrchestrator:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": True,
        "require_reconciliation_pass": True,
        "max_live_order_notional": 10_000_000.0,
        "max_daily_live_notional": max_daily_live_notional,
        "max_daily_live_order_count": max_daily_live_order_count,
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
    return MaestroOrchestrator(
        load_config(config_path),
        live_order_client=FakeLiveOrderClient(),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(),
    )


def _save_passed_reconciliation(store: StateStore) -> None:
    store.save_system_event("run_reconcile", "broker_reconciliation", {"passed": True})


def _approval(run_id: str, approval_id: str) -> ApprovalDecision:
    return ApprovalDecision(
        run_id=run_id,
        approval_id=approval_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _request(quantity: float, limit_price: float) -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id="ord_live",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=quantity,
        limit_price=limit_price,
        approval_id="appr_live",
        run_id="run_live",
    )
