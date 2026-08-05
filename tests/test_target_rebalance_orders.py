"""Target-rebalance order generation across a sell-then-buy rotation.

A monthly rotation (Crescendo: QQQ -> TLT) is fully invested at rebalance time:
the cash that funds the buy only exists because the sell in the same batch
releases it. These tests pin that the builder both exits holdings the new target
dropped and sizes buys against the proceeds those exits produce.
"""

from datetime import UTC, datetime

from maestro.config.execution import ExecutionConfig
from maestro.core.enums import AssetType, BrokerProduct, Currency, MarketRegion, OrderSide
from maestro.core.instruments import TradableInstrument
from maestro.execution.order_builder import OrderBuilder
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState

USD_SLEEVE = {"USD": {"cash_symbol": "CASH_USD", "symbols": ["QQQ", "TLT"]}}


def _us_instrument(symbol: str) -> TradableInstrument:
    return TradableInstrument(
        symbol=symbol,
        asset_type=AssetType.ETF,
        region=MarketRegion.US,
        currency=Currency.USD,
        broker="kis",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        broker_symbol=symbol,
        exchange_code="NASD",
        quantity_step=1,
        price_tick=0.01,
        min_order_quantity=1,
        min_order_notional=1,
    )


def _sleeve_builder(fee_buffer_pct: float = 0.0) -> OrderBuilder:
    return OrderBuilder(
        config=ExecutionConfig(
            order_posture="armed",
            live_order_limits={"fee_buffer_pct": fee_buffer_pct},
        ),
        instruments=[_us_instrument("QQQ"), _us_instrument("TLT")],
        currency_sleeves=USD_SLEEVE,
    )


def _sleeve_target() -> PortfolioTarget:
    return PortfolioTarget(
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
        allocations={},
        allocation_sleeves={"USD": {"TLT": 1.0}},
    )


def _flat_target() -> PortfolioTarget:
    return PortfolioTarget(
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
        allocations={"TLT": 1.0},
        source_strategy_ids=["crescendo_us"],
    )


def test_sleeve_rotation_funds_buy_with_same_batch_sell_proceeds():
    # $10,000 of QQQ, no cash. Selling it funds the full $10,000 TLT buy.
    builder = _sleeve_builder()
    state = PortfolioState(cash=0, cash_by_currency={"USD": 0.0}, positions={"QQQ": 100})

    orders = builder.build_orders(state, _sleeve_target(), {"QQQ": 100.0, "TLT": 100.0})

    by_symbol = {order.symbol: order for order in orders}
    assert by_symbol["QQQ"].side == OrderSide.SELL
    assert by_symbol["QQQ"].quantity == 100
    assert by_symbol["TLT"].side == OrderSide.BUY
    assert by_symbol["TLT"].quantity == 100


def test_sleeve_rotation_holds_back_fee_buffer_from_sell_proceeds():
    # 1% buffer on $10,000 of proceeds leaves $9,900 spendable -> 99 shares.
    builder = _sleeve_builder(fee_buffer_pct=0.01)
    state = PortfolioState(cash=0, cash_by_currency={"USD": 0.0}, positions={"QQQ": 100})

    orders = builder.build_orders(state, _sleeve_target(), {"QQQ": 100.0, "TLT": 100.0})

    buys = {order.symbol: order.quantity for order in orders if order.side == OrderSide.BUY}
    assert buys == {"TLT": 99}


def test_rebalance_sells_holding_the_new_target_dropped():
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("QQQ"), _us_instrument("TLT")],
    )
    state = PortfolioState(cash=0.0, positions={"QQQ": 100})

    orders = builder.build_orders(state, _flat_target(), {"QQQ": 100.0, "TLT": 100.0})

    sells = {order.symbol: order.quantity for order in orders if order.side == OrderSide.SELL}
    assert sells == {"QQQ": 100}


def test_rebalance_funds_buy_with_same_batch_sell_proceeds():
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("QQQ"), _us_instrument("TLT")],
    )
    state = PortfolioState(cash=0.0, positions={"QQQ": 100})

    orders = builder.build_orders(state, _flat_target(), {"QQQ": 100.0, "TLT": 100.0})

    buys = {order.symbol: order.quantity for order in orders if order.side == OrderSide.BUY}
    assert buys == {"TLT": 100}


def test_rebalance_keeps_partial_exit_when_holding_stays_in_target():
    # QQQ 100 -> target 30%: a trim, not a full exit, and the freed $7,000 buys TLT.
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("QQQ"), _us_instrument("TLT")],
    )
    state = PortfolioState(cash=0.0, positions={"QQQ": 100})
    target = PortfolioTarget(
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
        allocations={"QQQ": 0.3, "TLT": 0.7},
        source_strategy_ids=["crescendo_us"],
    )

    orders = builder.build_orders(state, target, {"QQQ": 100.0, "TLT": 100.0})

    by_symbol = {order.symbol: order for order in orders}
    assert by_symbol["QQQ"].side == OrderSide.SELL
    assert by_symbol["QQQ"].quantity == 70
    assert by_symbol["TLT"].side == OrderSide.BUY
    assert by_symbol["TLT"].quantity == 70


def test_rebalance_leaves_position_outside_the_instrument_universe_alone():
    # portfolio.unknown_broker_position_policy=include_readonly parks foreign
    # broker holdings in the state so they can be valued. They carry no
    # instrument, cannot be routed to a broker, and must not be liquidated just
    # because today's target omits them.
    builder = OrderBuilder(
        config=ExecutionConfig(order_posture="armed"),
        instruments=[_us_instrument("QQQ"), _us_instrument("TLT")],
    )
    state = PortfolioState(cash=0.0, positions={"QQQ": 100, "MOCK_LEGACY": 1})

    orders = builder.build_orders(
        state,
        _flat_target(),
        {"QQQ": 100.0, "TLT": 100.0, "MOCK_LEGACY": 777.0},
    )

    assert "MOCK_LEGACY" not in {order.symbol for order in orders}
    assert {order.symbol for order in orders if order.side == OrderSide.SELL} == {"QQQ"}


def test_rebalance_ignores_sell_proceeds_from_another_sleeve():
    """A KRW sell must not bankroll a USD buy.

    The USD sleeve rotates SPY -> TLT on its own $1,000, which the 1% fee buffer
    trims just under the price of one TLT share, so the buy is dropped. The KRW
    sleeve liquidates $10,000 of QQQ in the same batch: pool the two and that
    buffer is covered many times over and the TLT share goes out.
    """
    builder = OrderBuilder(
        config=ExecutionConfig(
            order_posture="armed",
            live_order_limits={"fee_buffer_pct": 0.01},
        ),
        instruments=[_us_instrument("QQQ"), _us_instrument("SPY"), _us_instrument("TLT")],
        currency_sleeves={
            "KRW": {"cash_symbol": "CASH_KRW", "symbols": ["QQQ"]},
            "USD": {"cash_symbol": "CASH_USD", "symbols": ["SPY", "TLT"]},
        },
    )
    state = PortfolioState(
        cash=0,
        cash_by_currency={"KRW": 0.0, "USD": 0.0},
        positions={"QQQ": 100, "SPY": 1},
    )
    target = PortfolioTarget(
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
        allocations={},
        allocation_sleeves={"KRW": {}, "USD": {"TLT": 1.0}},
    )

    orders = builder.build_orders(
        state,
        target,
        {"QQQ": 100.0, "SPY": 1_000.0, "TLT": 1_000.0},
    )

    assert {(order.symbol, order.side) for order in orders} == {
        ("QQQ", OrderSide.SELL),
        ("SPY", OrderSide.SELL),
    }
