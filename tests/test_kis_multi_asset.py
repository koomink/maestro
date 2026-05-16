from datetime import UTC, datetime
from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.core.enums import BrokerProduct, Currency, OrderSide
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_order_factory import ProductRoutingKISLiveOrderClient
from maestro.execution.order_builder import OrderBuilder
from maestro.execution.paper import PaperExecutionEngine
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.portfolio.manager import PortfolioTarget
from maestro.sdk import TargetAllocationResult
from maestro.signals.validator import SignalValidator
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_multi_asset_example_config_loads_domestic_and_overseas_products():
    config = load_config("configs/kis_multi_asset_live_approval.example.yaml")

    assert config.portfolio.allocation_mode == "currency_sleeves"
    assert config.portfolio.cash_by_currency == {"KRW": 1_000_000.0, "USD": 10_000.0}
    assert config.kis.effective_broker_products() == [
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    ]
    assert config.universe.get("SAMSUNG").broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert config.universe.get("AAPL").broker_product == BrokerProduct.KIS_OVERSEAS_STOCK


def test_signal_validator_accepts_sleeve_allocations():
    result = TargetAllocationResult(
        strategy_id="multi",
        strategy_version="1",
        timestamp=datetime.now(UTC),
        allocation_sleeves={
            "KRW": {"SAMSUNG": 0.5, "CASH_KRW": 0.5},
            "USD": {"AAPL": 0.5, "CASH_USD": 0.5},
        },
        confidence=1.0,
    )
    validator = SignalValidator.with_universe_boundaries(
        tradable_symbols={"CASH_KRW", "CASH_USD", "SAMSUNG", "AAPL"},
        research_only_symbols=set(),
        strategy_ids={"multi"},
    )

    validation = validator.validate(result)

    assert validation.ok is True


def test_order_builder_creates_independent_currency_sleeve_orders():
    config = load_config("configs/kis_multi_asset_live_approval.example.yaml")
    state = PortfolioState(
        cash=0,
        cash_by_currency={"KRW": 1_000_000.0, "USD": 10_000.0},
        positions={},
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={
            "KRW": {"SAMSUNG": 0.5, "CASH_KRW": 0.5},
            "USD": {"AAPL": 0.5, "CASH_USD": 0.5},
        },
    )

    orders = OrderBuilder(
        instruments=config.universe.instruments,
        currency_sleeves=config.portfolio.currency_sleeves,
    ).build_orders(state, target, {"SAMSUNG": 70_000.0, "AAPL": 200.0})

    assert len(orders) == 2
    domestic = next(order for order in orders if order.symbol == "SAMSUNG")
    overseas = next(order for order in orders if order.symbol == "AAPL")
    assert domestic.side == OrderSide.BUY
    assert domestic.currency == Currency.KRW
    assert domestic.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert domestic.quantity == 7
    assert domestic.notional == 490_000.0
    assert overseas.currency == Currency.USD
    assert overseas.broker_product == BrokerProduct.KIS_OVERSEAS_STOCK
    assert overseas.notional == 5_000.0


def test_paper_execution_updates_cash_by_order_currency():
    config = load_config("configs/kis_multi_asset_live_approval.example.yaml")
    state = PortfolioState(
        cash=0,
        cash_by_currency={"KRW": 1_000_000.0, "USD": 10_000.0},
        positions={},
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={
            "KRW": {"SAMSUNG": 0.5, "CASH_KRW": 0.5},
            "USD": {"AAPL": 0.5, "CASH_USD": 0.5},
        },
    )
    engine = PaperExecutionEngine(
        instruments=config.universe.instruments,
        currency_sleeves=config.portfolio.currency_sleeves,
    )

    orders, _, next_state = engine.execute(
        state,
        target,
        {"SAMSUNG": 70_000.0, "AAPL": 200.0},
    )

    assert len(orders) == 2
    assert next_state.cash_by_currency == {"KRW": 510_000.0, "USD": 5_000.0}
    assert next_state.positions["SAMSUNG"] == 7
    assert next_state.positions["AAPL"] == 25.0


def test_kis_live_order_router_builds_product_clients(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_ID", "12345678-01")
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "app-secret")
    config = load_config("configs/kis_multi_asset_live_approval.example.yaml")

    router = ProductRoutingKISLiveOrderClient(config)

    assert set(router.clients) == {
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    }


def test_multi_asset_live_approval_run_once_without_strategies_uses_sleeve_cash(tmp_path):
    raw = yaml.safe_load(Path("configs/kis_multi_asset_live_approval.example.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "kis_multi_asset_live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)

    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    latest_state = store.load_latest_portfolio_state()
    strategy_run = store.list_system_events_by_type("run_once_completed")[0]["payload"]

    assert summary.orders_created == 0
    assert summary.loaded_strategies == []
    assert latest_state.cash_by_currency == {"KRW": 1_000_000.0, "USD": 10_000.0}
    assert strategy_run["orders_created"] == 0
    assert store.status()["counts"]["approvals"] == 0


def test_kis_multi_product_readonly_service_filters_instruments_by_product(tmp_path):
    config = load_config("configs/multi_asset_readonly.example.yaml")
    store = StateStore(str(tmp_path / "state.db"), config.portfolio.initial_cash)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = KISReadOnlyService(
        config.kis,
        store,
        audit,
        instruments=config.universe.instruments,
    )

    overseas_symbols = {
        instrument.symbol
        for instrument in service._instruments_for_product(BrokerProduct.KIS_OVERSEAS_STOCK)
    }
    domestic_symbols = {
        instrument.symbol
        for instrument in service._instruments_for_product(BrokerProduct.KIS_DOMESTIC_STOCK)
    }

    assert overseas_symbols == {"CASH_USD", "AAPL", "VOO"}
    assert domestic_symbols == {"CASH_KRW", "SAMSUNG", "KODEX200"}
