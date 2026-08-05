from maestro.core.enums import (
    AssetType,
    BrokerProduct,
    Currency,
    MarketRegion,
    OrderSide,
    OrderStatus,
)
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
from maestro.execution.live_order_models import LiveOrderLifecycleResult
from maestro.execution.rotation_cohort import (
    evaluate_sell_phase,
    rescale_buys_to_cash,
    split_rotation_cohorts,
)


def _order(
    order_id: str,
    side: OrderSide,
    currency: Currency,
    account_id: str = "toss",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol="QQQ",
        side=side,
        quantity=1,
        price=100.0,
        notional=100.0,
        currency=currency,
        account_id=account_id,
    )


def test_split_groups_by_account_and_currency():
    orders = [
        _order("usd_sell", OrderSide.SELL, Currency.USD),
        _order("usd_buy", OrderSide.BUY, Currency.USD),
        _order("krw_sell", OrderSide.SELL, Currency.KRW),
    ]

    cohorts = split_rotation_cohorts(orders)

    by_currency = {cohort.currency: cohort for cohort in cohorts}
    assert set(by_currency) == {"USD", "KRW"}
    assert [order.order_id for order in by_currency["USD"].sells] == ["usd_sell"]
    assert [order.order_id for order in by_currency["USD"].buys] == ["usd_buy"]
    assert [order.order_id for order in by_currency["KRW"].sells] == ["krw_sell"]
    assert by_currency["KRW"].buys == ()


def test_split_keeps_accounts_apart_within_one_currency():
    orders = [
        _order("a_sell", OrderSide.SELL, Currency.USD, account_id="toss"),
        _order("b_buy", OrderSide.BUY, Currency.USD, account_id="kis"),
    ]

    cohorts = split_rotation_cohorts(orders)

    assert {cohort.account_id for cohort in cohorts} == {"toss", "kis"}
    assert all(len(cohort.sells) + len(cohort.buys) == 1 for cohort in cohorts)


def _lifecycle(order_id: str, status: OrderStatus) -> LiveOrderLifecycleResult:
    return LiveOrderLifecycleResult(
        run_id="run_1",
        order_id=order_id,
        final_status=status,
        broker_order_id=f"broker_{order_id}",
        checked_at="2026-08-05T13:40:00+00:00",
    )


def test_sell_phase_completes_when_every_sell_filled():
    outcome = evaluate_sell_phase(
        [_lifecycle("a", OrderStatus.FILLED), _lifecycle("b", OrderStatus.FILLED)]
    )

    assert outcome.complete is True
    assert outcome.unfilled == ()


def test_sell_phase_with_no_sells_completes():
    # A buy-only rebalance has nothing to wait for.
    assert evaluate_sell_phase([]).complete is True


def test_partially_filled_sell_blocks_the_buy_phase():
    outcome = evaluate_sell_phase(
        [_lifecycle("a", OrderStatus.FILLED), _lifecycle("b", OrderStatus.PARTIALLY_FILLED)]
    )

    assert outcome.complete is False
    assert [result.order_id for result in outcome.unfilled] == ["b"]
    assert "partially_filled" in outcome.reason


def test_rejected_sell_blocks_the_buy_phase():
    outcome = evaluate_sell_phase([_lifecycle("a", OrderStatus.REJECTED)])

    assert outcome.complete is False
    assert "rejected" in outcome.reason


def _instrument(symbol: str) -> TradableInstrument:
    return TradableInstrument(
        symbol=symbol,
        asset_type=AssetType.ETF,
        region=MarketRegion.US,
        currency=Currency.USD,
        broker="toss",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        broker_symbol=symbol,
        exchange_code="NASD",
        quantity_step=1,
        price_tick=0.01,
        min_order_quantity=1,
        min_order_notional=1,
    )


def _buy(order_id: str, symbol: str, quantity: float, price: float) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        price=price,
        notional=quantity * price,
        currency=Currency.USD,
        account_id="toss",
    )


def test_buys_keep_approved_size_when_cash_covers_them():
    buys = [_buy("b1", "TLT", 100, 100.0)]

    rescaled = rescale_buys_to_cash(buys, 10_000.0, {"TLT": _instrument("TLT")})

    assert [(order.symbol, order.quantity) for order in rescaled] == [("TLT", 100)]


def test_buys_never_grow_beyond_the_approved_quantity():
    # The operator approved 100 shares; extra cash must not buy 150.
    buys = [_buy("b1", "TLT", 100, 100.0)]

    rescaled = rescale_buys_to_cash(buys, 15_000.0, {"TLT": _instrument("TLT")})

    assert rescaled[0].quantity == 100


def test_short_cash_shrinks_buys_proportionally_and_floors_to_step():
    buys = [_buy("b1", "TLT", 100, 100.0), _buy("b2", "SCHD", 100, 100.0)]

    # $15,000 of $20,000 -> 0.75 each -> 75 whole shares each.
    rescaled = rescale_buys_to_cash(
        buys,
        15_000.0,
        {"TLT": _instrument("TLT"), "SCHD": _instrument("SCHD")},
    )

    assert {order.symbol: order.quantity for order in rescaled} == {"TLT": 75, "SCHD": 75}
    assert all(order.notional == order.quantity * order.price for order in rescaled)


def test_buy_that_shrinks_below_minimum_quantity_is_dropped():
    buys = [_buy("b1", "TLT", 1, 900.0)]

    rescaled = rescale_buys_to_cash(buys, 450.0, {"TLT": _instrument("TLT")})

    assert rescaled == []
