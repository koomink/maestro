from maestro.core.enums import Currency, OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.order_capacity import OrderCapacityService


def test_capacity_blocks_38_when_broker_allows_31_and_keeps_other_account():
    capacities = {
        "kis_ps": BrokerBuyingPower(
            symbol="005930",
            order_price=13_140,
            cash_buying_power=500_000,
            max_buy_quantity=31,
            source="fake",
        ),
        "other": BrokerBuyingPower(
            symbol="AAPL",
            order_price=100,
            cash_buying_power=1_000,
            max_buy_quantity=10,
            source="fake",
        ),
    }
    service = OrderCapacityService(lambda order: capacities[order.account_id])

    accepted, blocked = service.partition(
        [
            _order("ord_kis", "005930", 38, 13_140, "kis_ps"),
            _order("ord_other", "AAPL", 2, 100, "other"),
        ]
    )

    assert [order.order_id for order in accepted] == ["ord_other"]
    assert blocked[0].requested_quantity == 38
    assert blocked[0].max_buy_quantity == 31
    assert blocked[0].reason == "max_buy_quantity_exceeded"


def test_capacity_reserves_earlier_orders_for_same_account():
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=500,
            max_buy_quantity=100,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order("ord_1", "AAA", 3, 100, "account"),
            _order("ord_2", "BBB", 3, 100, "account"),
        ]
    )

    assert [order.order_id for order in accepted] == ["ord_1"]
    assert blocked[0].cash_buying_power == 200
    assert blocked[0].max_buy_quantity == 2
    assert blocked[0].reason == "cash_buying_power_exceeded"


def test_capacity_fails_closed_per_buy_but_preserves_sell():
    def lookup(order):
        raise TimeoutError(f"capacity unavailable for {order.order_id}")

    # Different currencies, so the sell does not fund the buy and the buy still
    # has to stand on its own pre-fill capacity reading.
    accepted, blocked = OrderCapacityService(lookup).partition(
        [
            _order("ord_buy", "AAA", 1, 100, "account", currency=Currency.USD),
            _order(
                "ord_sell", "BBB", 1, 100, "account",
                side=OrderSide.SELL, currency=Currency.KRW,
            ),
        ]
    )

    assert [order.order_id for order in accepted] == ["ord_sell"]
    assert blocked[0].reason == "broker_capacity_unavailable"
    assert blocked[0].error_type == "TimeoutError"


def test_buy_funded_by_a_sell_in_the_same_batch_is_kept():
    # Fully invested account: the broker reports zero buying power until the
    # sell settles, which is exactly when a rotation needs to size its buy.
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order("sell_qqq", "QQQ", 100, 100, "toss", side=OrderSide.SELL),
            _order("buy_tlt", "TLT", 100, 100, "toss"),
        ]
    )

    assert blocked == []
    assert {order.order_id for order in accepted} == {"sell_qqq", "buy_tlt"}
    buy = next(order for order in accepted if order.order_id == "buy_tlt")
    assert buy.metadata["sell_fill_pending"] is True


def test_buy_larger_than_its_funding_sell_is_deferred_not_blocked():
    """Undersizing is the post-sell phase's job, not this one's.

    Every figure here is read before the sell settles, so it cannot say how much
    a rotation's buy should be. Blocking it now hides the buy leg from the
    operator entirely; deferring lets the post-sell partition shrink it against
    the balance that actually exists.
    """
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order("sell_qqq", "QQQ", 10, 100, "toss", side=OrderSide.SELL),
            _order("buy_tlt", "TLT", 100, 100, "toss"),
        ]
    )

    assert blocked == []
    buy = next(order for order in accepted if order.order_id == "buy_tlt")
    assert buy.metadata["sell_fill_pending"] is True


def test_sell_proceeds_do_not_cross_accounts():
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order("sell_a", "QQQ", 100, 100, "account_a", side=OrderSide.SELL),
            _order("buy_b", "TLT", 100, 100, "account_b"),
        ]
    )

    assert [order.order_id for order in accepted] == ["sell_a"]
    assert [item.order.order_id for item in blocked] == ["buy_b"]


def _order(
    order_id: str,
    symbol: str,
    quantity: float,
    price: float,
    account_id: str,
    *,
    side: OrderSide = OrderSide.BUY,
    currency: Currency | None = None,
    strategy: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        notional=quantity * price,
        account_id=account_id,
        currency=currency,
        metadata={"source_strategy_ids": [strategy]} if strategy else {},
    )


def test_sell_funded_buy_defers_max_buy_quantity_too():
    # A fully invested KIS account reports max_ord_psbl_qty = 0 before the sell
    # settles. Deferring only the cash dimension still drops the rotation.
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=0.0,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order("sell_qqq", "QQQ", 100, 100, "kis", side=OrderSide.SELL),
            _order("buy_tlt", "TLT", 100, 100, "kis"),
        ]
    )

    assert blocked == []
    assert {order.order_id for order in accepted} == {"sell_qqq", "buy_tlt"}


def test_sell_funded_buy_defers_a_failed_capacity_lookup():
    def lookup(order):
        raise TimeoutError(f"capacity unavailable for {order.order_id}")

    accepted, blocked = OrderCapacityService(lookup).partition(
        [
            _order("sell_qqq", "QQQ", 100, 100, "toss", side=OrderSide.SELL),
            _order("buy_tlt", "TLT", 100, 100, "toss"),
        ]
    )

    assert blocked == []
    assert {order.order_id for order in accepted} == {"sell_qqq", "buy_tlt"}


def test_sell_proceeds_do_not_cross_currencies():
    # A KRW sell must not bankroll a USD buy: the numbers are not comparable and
    # the USD buy would land in a cohort with no sells, so nothing re-checks it.
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order(
                "sell_krw", "005930", 100, 100_000, "toss",
                side=OrderSide.SELL, currency=Currency.KRW,
            ),
            _order("buy_usd", "TLT", 100, 100, "toss", currency=Currency.USD),
        ]
    )

    assert [order.order_id for order in accepted] == ["sell_krw"]
    assert [item.order.order_id for item in blocked] == ["buy_usd"]


def test_sell_in_another_approval_group_does_not_fund_a_buy():
    """Deferral must follow the unit that actually executes together.

    Orders are split into approval groups by source_strategy_ids and each group
    runs its own sell-then-buy phases. A buy whose funding sell lands in a
    different group is executed as a buy-only cohort — no resize, no post-sell
    capacity ruling — so deferring it here would submit it unchecked.
    """
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order(
                "sell_a", "QQQ", 100, 100, "toss",
                side=OrderSide.SELL, currency=Currency.USD, strategy="alpha",
            ),
            _order("buy_b", "TLT", 100, 100, "toss", currency=Currency.USD, strategy="beta"),
        ]
    )

    assert [order.order_id for order in accepted] == ["sell_a"]
    assert [item.order.order_id for item in blocked] == ["buy_b"]


def test_sell_in_the_same_approval_group_still_funds_a_buy():
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order(
                "sell_a", "QQQ", 100, 100, "toss",
                side=OrderSide.SELL, currency=Currency.USD, strategy="alpha",
            ),
            _order("buy_b", "TLT", 100, 100, "toss", currency=Currency.USD, strategy="alpha"),
        ]
    )

    assert blocked == []
    buy = next(order for order in accepted if order.order_id == "buy_b")
    assert buy.metadata["sell_fill_pending"] is True
