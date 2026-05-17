from datetime import UTC, date, datetime
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
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataBundle, DataRequest
from maestro.state.models import PortfolioState
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


def test_missing_latest_price_blocks_live_approval_before_order_generation(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    orchestrator.datahub = FakeDataHub(missing_latest_price=True)
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
    assert event["payload"]["issues"][0]["reason"] == "missing_latest_price"
    blocked = orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")[0]
    assert blocked["payload"]["phase"] == "before_order_generation"


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


def test_market_session_blocks_live_approval_outside_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "maestro.orchestration.orchestrator.utc_now",
        lambda: datetime(2026, 5, 11, 20, 0, tzinfo=UTC),
    )
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "market_session": {
                "required": True,
                "timezone": "UTC",
                "open": "09:30",
                "close": "16:00",
            },
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    assert orchestrator.live_order_client.requests == []
    event = orchestrator.state_store.list_system_events_by_type("market_session_halt")[0]
    assert event["payload"]["reason"] == "outside_market_session"


def test_market_session_blocks_live_approval_on_configured_holiday(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "maestro.orchestration.orchestrator.utc_now",
        lambda: datetime(2026, 5, 11, 14, 0, tzinfo=UTC),
    )
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "market_session": {
                "required": True,
                "timezone": "UTC",
                "open": "09:30",
                "close": "16:00",
                "holidays": ["2026-05-11"],
            },
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("market_session_halt")[0]
    assert event["payload"]["reason"] == "market_holiday_closed"


def test_broker_quote_validation_uses_broker_quote_for_live_order_generation(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {
                "require_quote_validation": True,
                "max_quote_deviation_pct": 0.05,
            },
            "live_order_dry_run": True,
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 80.0, "MOCK_ETF_B": 50.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("broker_quote_validation_halt") == []
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["order_prices"] == {"MOCK_ETF_A": 80.0, "MOCK_ETF_B": 50.0}


def test_broker_quote_validation_allows_live_approval_when_quotes_match(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {
                "require_quote_validation": True,
                "max_quote_deviation_pct": 0.05,
            },
            "live_order_dry_run": True,
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("broker_quote_validation_halt") == []
    assert orchestrator.state_store.list_system_events_by_type("safety_execution_blocked") == []


def test_broker_risk_validation_blocks_when_fee_buffer_exceeds_buying_power(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {"require_risk_validation": True},
            "live_order_limits": {"fee_buffer_pct": 0.01},
            "live_order_dry_run": True,
        },
    )
    snapshot_id = _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        cash=10_000_000.0,
        buying_power=5_000_000.0,
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=snapshot_id)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_risk_halt")[0]
    reasons = {issue["reason"] for issue in event["payload"]["issues"]}
    assert "buying_power_exceeded" in reasons


def test_broker_risk_validation_blocks_pending_broker_orders(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {"require_risk_validation": True},
            "live_order_dry_run": True,
        },
    )
    snapshot_id = _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        unfilled_orders=[{"order_id": "pending-1", "symbol": "MOCK_ETF_A"}],
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=snapshot_id)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_risk_halt")[0]
    reasons = {issue["reason"] for issue in event["payload"]["issues"]}
    assert "pending_broker_orders" in reasons


def test_broker_risk_validation_blocks_unreconciled_broker_snapshot(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {"require_risk_validation": True},
            "live_order_dry_run": True,
        },
    )
    snapshot_id = _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=snapshot_id - 1)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_risk_halt")[0]
    reasons = {issue["reason"] for issue in event["payload"]["issues"]}
    assert "broker_snapshot_not_reconciled" in reasons


def test_broker_risk_validation_blocks_symbol_exposure_from_broker_truth(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {"require_risk_validation": True},
            "live_order_dry_run": True,
        },
    )
    snapshot_id = _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        cash=10_000_000.0,
        buying_power=10_000_000.0,
        positions={"MOCK_ETF_A": 100_000.0},
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=snapshot_id)

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_risk_halt")[0]
    reasons = {issue["reason"] for issue in event["payload"]["issues"]}
    assert "symbol_exposure_exceeded" in reasons


def test_daily_loss_limit_blocks_from_normalized_broker_pnl(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "live_order_limits": {"daily_loss_limit": 100.0},
            "live_order_dry_run": True,
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        account_overrides={"daily_pnl": -150.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("broker_risk_halt")[0]
    reasons = {issue["reason"] for issue in event["payload"]["issues"]}
    assert "daily_loss_limit_exceeded" in reasons


def test_daily_loss_limit_allows_when_normalized_broker_pnl_is_above_limit(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "live_order_limits": {"daily_loss_limit": 100.0},
            "live_order_dry_run": True,
        },
    )
    _save_passed_reconciliation(orchestrator.state_store)
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        account_overrides={"daily_pnl": -50.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("broker_risk_halt") == []


def test_live_recovery_required_blocks_next_live_approval_order(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    _save_passed_reconciliation(orchestrator.state_store)
    request = _live_order_request("ord_recovery")
    result = LiveOrderResult(
        order_id=request.order_id,
        status=OrderStatus.HALTED,
        message="Live order submission outcome is ambiguous.",
        raw={"recovery_required": True},
    )
    orchestrator.state_store.save_system_event(
        "run_prior",
        "live_order_recovery_required",
        {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "duplicate_key": request.duplicate_key,
            "notional": request.notional,
        },
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("live_order_recovery_halt")[0]
    assert event["payload"]["reason"] == "live_order_recovery_required"


def test_incomplete_live_order_lifecycle_blocks_next_live_approval_order(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    _save_passed_reconciliation(orchestrator.state_store)
    request = _live_order_request("ord_incomplete")
    result = LiveOrderResult(
        order_id=request.order_id,
        status=OrderStatus.ACCEPTED_BY_BROKER,
        broker_order=BrokerOrderId(
            broker="fake",
            broker_order_id="broker:ord_incomplete",
            order_id=request.order_id,
            submitted_at=utc_now().isoformat(),
        ),
    )
    orchestrator.state_store.save_system_event(
        "run_prior",
        "live_order_result",
        {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "duplicate_key": request.duplicate_key,
            "notional": request.notional,
            "submitted_date": date.today().isoformat(),
        },
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    event = orchestrator.state_store.list_system_events_by_type("live_order_recovery_halt")[0]
    assert event["payload"]["reason"] == "live_order_lifecycle_incomplete"


def test_recovery_completion_allows_live_approval_after_prior_ambiguous_order(tmp_path):
    orchestrator = _live_orchestrator(tmp_path)
    _save_passed_reconciliation(orchestrator.state_store)
    request = _live_order_request("ord_recovered")
    result = LiveOrderResult(
        order_id=request.order_id,
        status=OrderStatus.HALTED,
        message="Live order submission outcome is ambiguous.",
        raw={"recovery_required": True},
    )
    orchestrator.state_store.save_system_event(
        "run_prior",
        "live_order_recovery_required",
        {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "duplicate_key": request.duplicate_key,
            "notional": request.notional,
        },
    )
    orchestrator.state_store.save_system_event(
        "run_recovery",
        "live_order_recovery_completed",
        {"reason": "broker truth reconciled"},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("live_order_recovery_halt") == []


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
    def __init__(self, stale: bool = False, missing_latest_price: bool = False) -> None:
        self.stale = stale
        self.missing_latest_price = missing_latest_price
        self.prices = {"CASH": 1.0, "MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0}

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        now = utc_now()
        data = {}
        for request in requests:
            payload = {
                "latest_price": {
                    "symbol": request.symbol,
                    "timestamp": now.isoformat(),
                    "price": self.prices[request.symbol],
                    "source": "fake",
                },
                "is_stale": self.stale and request.symbol != "CASH",
            }
            if self.missing_latest_price and request.symbol != "CASH":
                payload.pop("latest_price")
            data[request.symbol] = payload
        return DataBundle(
            requests=requests,
            data=data,
            generated_at=now,
            source="fake",
        )


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
        return {"ok": True, "result": {"message_id": 1}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        callback_data = "approve:appr_live"
        if self.sent_messages:
            reply_markup = self.sent_messages[-1].get("reply_markup") or {}
            keyboard = reply_markup.get("inline_keyboard") or []
            if keyboard and keyboard[0]:
                callback_data = keyboard[0][0].get("callback_data", callback_data)
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


class FakeStatusClient(LiveOrderStatusClient):
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
                average_fill_price=1.0,
                fill_count=1,
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
    execution_overrides: dict[str, Any] | None = None,
) -> MaestroOrchestrator:
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
            "max_order_notional": 10_000_000.0,
            "max_daily_notional": max_daily_live_notional,
            "max_daily_order_count": max_daily_live_order_count,
        },
        "order_status_max_polls": 1,
        "order_status_poll_interval_seconds": 0.0,
    }
    execution_overrides = dict(execution_overrides or {})
    live_order_limits = execution_overrides.pop("live_order_limits", None)
    if live_order_limits:
        raw["execution"]["live_order_limits"].update(live_order_limits)
    raw["execution"].update(execution_overrides)
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
    orchestrator = MaestroOrchestrator(
        load_config(config_path),
        live_order_client=FakeLiveOrderClient(),
        live_order_status_client=FakeStatusClient(),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(),
    )
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        PortfolioState(cash=10_000_000.0, positions={}),
    )
    return orchestrator


def _save_passed_reconciliation(
    store: StateStore,
    *,
    broker_snapshot_id: int | None = None,
) -> None:
    payload: dict[str, Any] = {"passed": True}
    if broker_snapshot_id is not None:
        payload["broker_snapshot_id"] = broker_snapshot_id
    store.save_system_event("run_reconcile", "broker_reconciliation", payload)


def _save_broker_snapshot_with_quotes(
    store: StateStore,
    current_prices: dict[str, float],
    *,
    cash: float = 1_000_000.0,
    buying_power: float = 1_000_000.0,
    positions: dict[str, float] | None = None,
    unfilled_orders: list[dict[str, Any]] | None = None,
    account_overrides: dict[str, Any] | None = None,
) -> int:
    account = {
        "account_id": "MOCK",
        "cash": cash,
        "buying_power": buying_power,
        "positions": [
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_price": current_prices.get(symbol, 1.0),
                "current_price": current_prices.get(symbol, 1.0),
            }
            for symbol, quantity in (positions or {}).items()
        ],
    }
    account.update(account_overrides or {})
    store.save_broker_account_snapshot(
        "run_broker_snapshot",
        "MOCK",
        {
            "account": account,
            "current_prices": current_prices,
            "order_fills": [],
            "unfilled_orders": unfilled_orders or [],
        },
    )
    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    return int(latest["id"])


def _live_order_request(order_id: str) -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id=order_id,
        symbol="MOCK_ETF_A",
        side=OrderSide.BUY,
        quantity=1.0,
        limit_price=100.0,
        approval_id=f"appr_{order_id}",
        run_id=f"run_{order_id}",
        duplicate_key=f"duplicate:{order_id}",
    )


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
