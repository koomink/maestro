from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.approval.models import ApprovalDecision
from maestro.config.broker import BrokerAccountConfig
from maestro.config.loader import load_config
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import (
    AssetType,
    BrokerProduct,
    Currency,
    ExchangeCode,
    MarketRegion,
    OrderSide,
    OrderStatus,
    OrderType,
)
from maestro.core.ids import new_run_id
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
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
from maestro.orchestration.live_gates import LiveExecutionGateService
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



def test_currency_daily_notional_limit_blocks_only_exceeded_currency(tmp_path):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"KRW": 10_000_000, "USD": 10_000},
            "max_daily_notional_by_currency": {"KRW": 10_000_000, "USD": 1_000},
            "max_daily_order_count": 10,
        },
    )
    store = StateStore(str(tmp_path / "currency_daily_state.db"), initial_cash=10_000.0)
    store.save_system_event(
        "run_existing_usd",
        "live_order_result",
        {
            "submitted_date": date.today().isoformat(),
            "notional": 200.0,
            "request": {"currency": "USD"},
        },
    )
    service = LiveExecutionGateService(
        config,
        store,
        AuditLogger(str(tmp_path / "currency_daily_audit.jsonl")),
    )
    orders = [
        _order("krw_order", "KRW_ETF", Currency.KRW, 9_000_000.0),
        _order("usd_order", "USD_ETF", Currency.USD, 900.0),
    ]

    blocks = service.evaluate("run_currency_daily", orders, [])

    assert [block["reason"] for block in blocks] == ["daily_notional_exceeded"]
    assert blocks[0]["currency"] == "USD"
    assert blocks[0]["existing_notional"] == 200.0
    assert blocks[0]["proposed_notional"] == 900.0
    assert blocks[0]["max_daily_live_notional"] == 1_000.0


def test_currency_max_order_limit_blocks_exceeded_order_currency(tmp_path):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"KRW": 1_000_000, "USD": 10_000},
            "max_daily_notional_by_currency": {"KRW": 10_000_000, "USD": 10_000},
            "max_daily_order_count": 10,
        },
    )
    service = LiveExecutionGateService(
        config,
        StateStore(str(tmp_path / "currency_max_order_state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "currency_max_order_audit.jsonl")),
    )

    blocks = service.evaluate(
        "run_currency_max_order",
        [_order("krw_order", "KRW_ETF", Currency.KRW, 1_500_000.0)],
        [],
    )

    assert [block["reason"] for block in blocks] == ["max_order_notional_exceeded"]
    assert blocks[0]["currency"] == "KRW"
    assert blocks[0]["order_notional"] == 1_500_000.0
    assert blocks[0]["max_live_order_notional"] == 1_000_000.0


def test_toss_order_does_not_require_kis_broker_product_match(tmp_path):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"USD": 10_000},
            "max_daily_notional_by_currency": {"USD": 10_000},
            "max_daily_order_count": 10,
        },
    )
    config.accounts = [
        BrokerAccountConfig(
            id="toss_brokerage",
            broker="toss",
            environment="real",
            account_seq=1,
        )
    ]
    config.kis.broker_products = [BrokerProduct.KIS_DOMESTIC_STOCK]
    service = LiveExecutionGateService(
        config,
        StateStore(str(tmp_path / "toss_gate_state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "toss_gate_audit.jsonl")),
    )
    order = _order("toss_order", "USD_ETF", Currency.USD, 100.0).model_copy(
        update={"account_id": "toss_brokerage"}
    )

    blocks = service.evaluate("run_toss_gate", [order], [])

    assert not [
        block for block in blocks if block.get("reason") == "broker_product_mismatch"
    ]


def test_toss_live_gate_requires_integer_quantity_even_when_instrument_allows_fractional(
    tmp_path,
):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"USD": 10_000},
            "max_daily_notional_by_currency": {"USD": 10_000},
            "max_daily_order_count": 10,
        },
    )
    config.accounts = [
        BrokerAccountConfig(
            id="toss_brokerage",
            broker="toss",
            environment="real",
            account_seq=1,
        )
    ]
    usd_instrument = next(
        instrument for instrument in config.universe.instruments if instrument.symbol == "USD_ETF"
    )
    usd_instrument.quantity_step = 0.000001
    usd_instrument.min_order_quantity = 0.000001
    service = LiveExecutionGateService(
        config,
        StateStore(str(tmp_path / "toss_integer_gate_state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "toss_integer_gate_audit.jsonl")),
    )
    order = _order("toss_fractional_order", "USD_ETF", Currency.USD, 50.0).model_copy(
        update={
            "account_id": "toss_brokerage",
            "quantity": 0.5,
            "order_type": OrderType.LIMIT,
        }
    )

    blocks = service.evaluate("run_toss_integer_gate", [order], [])

    assert any(
        block.get("reason") == "integer_quantity_required"
        and block.get("broker") == "toss"
        and block.get("symbol") == "USD_ETF"
        for block in blocks
    )


def test_toss_live_gate_blocks_market_order_intents_before_approval(tmp_path):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"USD": 10_000},
            "max_daily_notional_by_currency": {"USD": 10_000},
            "max_daily_order_count": 10,
        },
    )
    config.accounts = [
        BrokerAccountConfig(
            id="toss_brokerage",
            broker="toss",
            environment="real",
            account_seq=1,
        )
    ]
    service = LiveExecutionGateService(
        config,
        StateStore(str(tmp_path / "toss_market_gate_state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "toss_market_gate_audit.jsonl")),
    )
    order = _order("toss_market_order", "USD_ETF", Currency.USD, 50.0).model_copy(
        update={
            "account_id": "toss_brokerage",
            "order_type": OrderType.MARKET,
        }
    )

    blocks = service.evaluate("run_toss_market_gate", [order], [])

    assert any(
        block.get("reason") == "unsupported_order_type"
        and block.get("broker") == "toss"
        and block.get("order_type") == "market"
        for block in blocks
    )


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


def test_market_session_uses_exchange_specific_session_for_krx_orders(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "maestro.orchestration.live_gates.utc_now",
        lambda: datetime(2026, 5, 11, 1, 0, tzinfo=UTC),
    )
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"KRW": 10_000_000},
            "max_daily_notional_by_currency": {"KRW": 10_000_000},
            "max_daily_order_count": 10,
        },
    )
    config.execution.market_session.required = True
    config.execution.market_session.timezone = "America/New_York"
    config.execution.market_session.open = "09:30"
    config.execution.market_session.close = "16:00"
    config.execution.market_sessions_by_exchange = {
        "KRX": type(config.execution.market_session)(
            required=True,
            timezone="Asia/Seoul",
            open="09:00",
            close="15:30",
            weekdays=[0, 1, 2, 3, 4],
            holidays=[],
        )
    }
    service = LiveExecutionGateService(
        config,
        StateStore(str(tmp_path / "krx_market_state.db"), initial_cash=10_000.0),
        AuditLogger(str(tmp_path / "krx_market_audit.jsonl")),
        now_fn=lambda: datetime(2026, 5, 11, 1, 0, tzinfo=UTC),
    )

    blocks = service.evaluate(
        "run_krx_market",
        [_order("krw_order", "KRW_ETF", Currency.KRW, 1_000_000.0)],
        [],
    )

    assert not [block for block in blocks if block.get("event_type") == "market_session_halt"]


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


def test_broker_quote_validation_keeps_usd_order_price_native_for_krw_portfolio(tmp_path):
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
    orchestrator.config.portfolio.base_currency = Currency.KRW
    orchestrator.config.portfolio.allocation_mode = "currency_sleeves"
    orchestrator.config.universe.instruments = [
        TradableInstrument(
            symbol="MOCK_ETF_A",
            asset_type=AssetType.ETF,
            region=MarketRegion.US,
            currency=Currency.USD,
            broker="kis",
            broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
            broker_symbol="MOCKA",
            exchange_code=ExchangeCode.NASD,
            quantity_step=1.0,
            price_tick=0.01,
            min_order_quantity=1.0,
            min_order_notional=1.0,
        ),
        TradableInstrument(
            symbol="MOCK_ETF_B",
            asset_type=AssetType.ETF,
            region=MarketRegion.KR,
            currency=Currency.KRW,
            broker="kis",
            broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
            broker_symbol="MOCKB",
            exchange_code=ExchangeCode.KRX,
            quantity_step=1.0,
            price_tick=1.0,
            min_order_quantity=1.0,
            min_order_notional=1.0,
        ),
    ]
    orchestrator.config.kis.broker_products = [
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    ]
    for account in orchestrator.config.accounts:
        if account.broker == "kis":
            account.broker_products = [
                BrokerProduct.KIS_DOMESTIC_STOCK,
                BrokerProduct.KIS_OVERSEAS_STOCK,
            ]
    orchestrator.fx_service = type(
        "FakeFXService",
        (),
        {"refresh_from_config": lambda self: type("FX", (), {"rates": {"USD/KRW": 1531.1778}})()},
    )()
    _save_passed_reconciliation(orchestrator.state_store)
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 70.11, "MOCK_ETF_B": 50.0},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("broker_quote_validation_halt") == []
    proposal_snapshot = orchestrator.state_store.list_system_events_by_type(
        "live_proposal_data_snapshot"
    )[0]["payload"]
    assert proposal_snapshot["order_prices"]["MOCK_ETF_A"] == 70.11
    proposed_orders = {
        order["symbol"]: order for order in proposal_snapshot["proposed_orders"]
    }
    assert proposed_orders["MOCK_ETF_A"]["price"] == 70.11


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


def test_broker_risk_validation_uses_order_account_snapshot(tmp_path):
    orchestrator = _live_orchestrator(
        tmp_path,
        execution_overrides={
            "broker_validation": {"require_risk_validation": True},
            "live_order_dry_run": True,
        },
    )
    kis_snapshot_id = _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        cash=10_000_000.0,
        buying_power=10_000_000.0,
        account_overrides={"account_id": "kis_isa"},
    )
    _save_broker_snapshot_with_quotes(
        orchestrator.state_store,
        {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        cash=0.0,
        buying_power=0.0,
        account_overrides={"account_id": "toss_brokerage"},
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=kis_snapshot_id)
    orders = [
        _order("kis_buy", "MOCK_ETF_A", Currency.KRW, 100_000.0).model_copy(
            update={"account_id": "kis_isa"}
        )
    ]

    blocks = LiveExecutionGateService(
        orchestrator.config,
        orchestrator.state_store,
        orchestrator.audit,
    ).evaluate("run_account_risk", orders, [])

    assert not [
        issue
        for block in blocks
        if block.get("event_type") == "broker_risk_halt"
        for issue in block.get("issues", [])
        if issue.get("reason") == "buying_power_exceeded"
    ]


def test_broker_risk_validation_fails_closed_when_order_account_snapshot_missing(tmp_path):
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
        account_overrides={"account_id": "toss_brokerage"},
    )
    _save_passed_reconciliation(orchestrator.state_store, broker_snapshot_id=snapshot_id)
    orders = [
        _order("kis_buy", "MOCK_ETF_A", Currency.KRW, 100_000.0).model_copy(
            update={"account_id": "kis_isa"}
        )
    ]

    blocks = LiveExecutionGateService(
        orchestrator.config,
        orchestrator.state_store,
        orchestrator.audit,
    ).evaluate("run_missing_account_risk", orders, [])

    issues = [
        issue
        for block in blocks
        if block.get("event_type") == "broker_risk_halt"
        for issue in block.get("issues", [])
    ]
    assert {"reason": "missing_broker_snapshot", "account_id": "kis_isa"} in issues


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


def test_broker_risk_validation_allows_concentrated_broker_truth(tmp_path):
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

    assert summary.orders_created == 2
    assert orchestrator.state_store.list_system_events_by_type("broker_risk_halt") == []



def test_daily_loss_limit_by_currency_blocks_exceeded_currency(tmp_path):
    config = _currency_limit_config(
        tmp_path,
        live_order_limits={
            "max_order_notional_by_currency": {"KRW": 10_000_000, "USD": 10_000},
            "max_daily_notional_by_currency": {"KRW": 10_000_000, "USD": 10_000},
            "daily_loss_limit_by_currency": {"KRW": 1_000_000, "USD": 100},
            "max_daily_order_count": 10,
        },
    )
    store = StateStore(str(tmp_path / "currency_loss_state.db"), initial_cash=10_000.0)
    _save_broker_snapshot_with_quotes(
        store,
        {"KRW_ETF": 1_000.0, "USD_ETF": 100.0},
        account_overrides={"daily_pnl_by_currency": {"KRW": -50_000.0, "USD": -150.0}},
    )
    service = LiveExecutionGateService(
        config,
        store,
        AuditLogger(str(tmp_path / "currency_loss_audit.jsonl")),
    )

    blocks = service.evaluate(
        "run_currency_loss",
        [_order("usd_order", "USD_ETF", Currency.USD, 900.0)],
        [],
    )

    assert [block["reason"] for block in blocks] == ["broker_risk_failed"]
    issue = blocks[0]["issues"][0]
    assert issue["reason"] == "daily_loss_limit_exceeded"
    assert issue["currency"] == "USD"
    assert issue["broker_pnl"] == -150.0
    assert issue["daily_loss_limit"] == 100.0


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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
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



def _currency_limit_config(
    tmp_path,
    *,
    live_order_limits: dict[str, Any],
):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "currency_limits_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "currency_limits_audit.jsonl")
    raw["portfolio"]["allocation_mode"] = "currency_sleeves"
    raw["portfolio"]["cash_by_currency"] = {"KRW": 10_000_000, "USD": 10_000}
    raw["portfolio"].pop("initial_cash", None)
    raw["portfolio"]["currency_sleeves"] = {
        "KRW": {"cash_symbol": "CASH_KRW", "symbols": ["KRW_ETF"]},
        "USD": {"cash_symbol": "CASH_USD", "symbols": ["USD_ETF"]},
    }
    raw["portfolio"].pop("allowed_symbols", None)
    raw["universe"] = {
        "instruments": [
            {
                "symbol": "KRW_ETF",
                "asset_type": "etf",
                "region": "KR",
                "currency": "KRW",
                "broker": "kis",
                "broker_product": "kis_domestic_stock",
                "broker_symbol": "KRWETF",
                "exchange_code": "KRX",
                "quantity_step": 1,
                "price_tick": 1,
                "min_order_quantity": 1,
                "min_order_notional": 1,
            },
            {
                "symbol": "USD_ETF",
                "asset_type": "etf",
                "region": "US",
                "currency": "USD",
                "broker": "kis",
                "broker_product": "kis_overseas_stock",
                "broker_symbol": "USDETF",
                "exchange_code": "NASD",
                "quantity_step": 1,
                "price_tick": 0.01,
                "min_order_quantity": 1,
                "min_order_notional": 1,
            },
        ]
    }
    raw["strategies"] = []
    raw["accounts"] = [{"id": "test_sandbox", "broker": "sandbox", "enabled": True}]
    raw["kis"] = {
        "enabled": True,
        "provider": "mock",
        "account_id": "MOCK",
        "broker_products": ["kis_domestic_stock", "kis_overseas_stock"],
    }
    raw["execution"] = {
        "proposal_engine": "paper",
        "require_reconciliation_pass": False,
        "live_order_limits": live_order_limits,
    }
    raw["approval"] = {
        "enabled": True,
        "provider": "console",
        "require_approval": True,
        "default_decision": "approved",
    }
    config_path = tmp_path / "currency_limits.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


def _order(order_id: str, symbol: str, currency: Currency, notional: float) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=1,
        price=notional,
        notional=notional,
        currency=currency,
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
    raw["kis"] = {
        "enabled": True,
        "provider": "mock",
        "account_id": "MOCK",
        "broker_products": ["kis_domestic_stock"],
    }
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
