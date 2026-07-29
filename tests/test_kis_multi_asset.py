from datetime import UTC, datetime
from pathlib import Path

import yaml

from maestro.config.execution import ExecutionConfig
from maestro.config.loader import load_config
from maestro.core.enums import BrokerProduct, Currency, OrderSide
from maestro.core.instruments import TradableInstrument
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
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")

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
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")
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


def test_order_builder_sells_sleeve_positions_missing_from_target():
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")
    state = PortfolioState(
        cash=0,
        cash_by_currency={"USD": 1_000.0},
        positions={"AAPL": 10},
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={"USD": {"VOO": 1.0}},
    )

    orders = OrderBuilder(
        instruments=config.universe.instruments,
        currency_sleeves=config.portfolio.currency_sleeves,
    ).build_orders(state, target, {"AAPL": 100.0, "VOO": 200.0})

    by_symbol = {order.symbol: order for order in orders}
    assert by_symbol["AAPL"].side == OrderSide.SELL
    assert by_symbol["AAPL"].quantity == 10
    assert by_symbol["AAPL"].notional == 1_000.0
    assert by_symbol["AAPL"].sleeve == "USD"
    assert by_symbol["VOO"].side == OrderSide.BUY
    assert by_symbol["VOO"].quantity == 5
    assert by_symbol["VOO"].notional == 1_000.0


def test_order_builder_scales_sleeve_buys_to_cash_after_fee_buffer():
    builder = OrderBuilder(
        config=ExecutionConfig(
            order_posture="armed",
            live_order_limits={"fee_buffer_pct": 0.002},
        ),
        instruments=[
            _us_instrument("AAPL"),
            _us_instrument("VOO"),
            _us_instrument("QQQ"),
        ],
        currency_sleeves={
            "USD": {"cash_symbol": "CASH_USD", "symbols": ["AAPL", "VOO", "QQQ"]}
        },
    )
    state = PortfolioState(
        cash=0,
        cash_by_currency={"USD": 1_000.0},
        positions={"AAPL": 10},
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={"USD": {"VOO": 0.5, "QQQ": 0.5}},
    )

    orders = builder.build_orders(state, target, {"AAPL": 100.0, "VOO": 1.0, "QQQ": 1.0})

    by_symbol = {order.symbol: order for order in orders}
    assert by_symbol["AAPL"].side == OrderSide.SELL
    assert by_symbol["AAPL"].quantity == 10
    assert by_symbol["AAPL"].notional == 1_000.0
    buy_orders = [order for order in orders if order.side == OrderSide.BUY]
    assert {order.symbol: order.notional for order in buy_orders} == {"VOO": 499.0, "QQQ": 499.0}
    assert sum(order.notional for order in buy_orders) == 998.0
    for order in buy_orders:
        assert order.metadata["cash_scaled"] is True
        assert order.metadata["cash_available"] == 998.0
        assert order.metadata["buy_notional_before_scaling"] == 2_000.0
        assert order.metadata["buy_notional_after_scaling"] == 998.0
        assert order.metadata["fee_buffer_pct"] == 0.002


def test_order_builder_drops_scaled_sleeve_buy_below_minimum():
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("AAPL"), _us_instrument("VOO")],
        currency_sleeves={"USD": {"cash_symbol": "CASH_USD", "symbols": ["AAPL", "VOO"]}},
    )
    state = PortfolioState(
        cash=0,
        cash_by_currency={"USD": 50.0},
        positions={"AAPL": 10},
    )
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={"USD": {"VOO": 1.0}},
    )

    orders = builder.build_orders(state, target, {"AAPL": 100.0, "VOO": 100.0})

    assert [order.symbol for order in orders] == ["AAPL"]
    assert orders[0].side == OrderSide.SELL


def test_order_builder_sleeve_buys_need_prices_in_the_sleeve_currency():
    """Sleeve cash and sleeve prices must share a currency.

    Regression: the orchestrator used to size orders with base-currency (FX
    converted) prices while the sleeve's cash stayed in its own currency. The
    cash-scaling step then divided a USD cash figure by a KRW notional, shrinking
    every buy by the FX rate until it rounded to zero shares. Sells were unaffected,
    so a USD sleeve could liquidate but never buy.
    """
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("AAPL"), _us_instrument("VOO")],
        currency_sleeves={"USD": {"cash_symbol": "CASH_USD", "symbols": ["AAPL", "VOO"]}},
    )
    state = PortfolioState(cash=0, cash_by_currency={"USD": 10_000.0}, positions={"AAPL": 10})
    target = PortfolioTarget(
        timestamp=datetime.now(UTC),
        allocations={},
        allocation_sleeves={"USD": {"VOO": 1.0}},
    )
    usd_prices = {"AAPL": 100.0, "VOO": 100.0}
    krw_rate = 1_350.0
    base_currency_prices = {symbol: price * krw_rate for symbol, price in usd_prices.items()}

    native = builder.build_orders(state, target, usd_prices)
    converted = builder.build_orders(state, target, base_currency_prices)

    native_buys = {order.symbol: order.quantity for order in native if order.side == OrderSide.BUY}
    # Sleeve value is $11,000 (cash $10,000 + AAPL 10 @ $100) but only $10,000 is
    # spendable, so the $11,000 of buys scales down to 100 shares.
    assert native_buys == {"VOO": 100.0}
    # Same portfolio, same target, prices quoted in the base currency: every buy is
    # lost while the sell survives.
    assert [order.symbol for order in converted] == ["AAPL"]
    assert converted[0].side == OrderSide.SELL


def test_paper_execution_updates_cash_by_order_currency():
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")
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
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_ID", "12345678-01")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "app-secret")
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")

    router = ProductRoutingKISLiveOrderClient(config)

    assert set(router.clients) == {
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    }


def test_multi_asset_live_approval_run_once_without_strategies_uses_adopted_sleeve_cash(
    tmp_path,
):
    raw = yaml.safe_load(
        Path("tests/fixtures/configs/live_approval_kis_multi_asset.yaml").read_text()
    )
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "kis_multi_asset_live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)

    orchestrator = MaestroOrchestrator(config)
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        PortfolioState(
            cash=1_000_000.0,
            cash_by_currency={"KRW": 1_000_000.0, "USD": 10_000.0},
            positions={},
        ),
    )

    summary = orchestrator.run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    latest_state = store.load_latest_portfolio_state()
    strategy_run = store.list_system_events_by_type("run_once_completed")[0]["payload"]

    assert summary.orders_created == 0
    assert summary.loaded_strategies == []
    assert latest_state.cash_by_currency == {"KRW": 1_000_000.0, "USD": 10_000.0}
    assert strategy_run["orders_created"] == 0
    assert store.status()["counts"]["approvals"] == 0


def test_kis_multi_product_readonly_service_filters_instruments_by_product(tmp_path):
    config = load_config("tests/fixtures/configs/live_readonly_multi_asset_kis.yaml")
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


def _us_instrument(symbol: str) -> TradableInstrument:
    return TradableInstrument(
        symbol=symbol,
        asset_type="etf",
        region="US",
        currency="USD",
        broker="kis",
        broker_product="kis_overseas_stock",
        broker_symbol=symbol,
        exchange_code="NASD",
        quantity_step=1,
        price_tick=0.01,
        min_order_quantity=1,
        min_order_notional=1,
    )
