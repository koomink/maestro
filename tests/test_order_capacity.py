from maestro.core.enums import OrderSide
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

    accepted, blocked = OrderCapacityService(lookup).partition(
        [
            _order("ord_buy", "AAA", 1, 100, "account"),
            _order("ord_sell", "AAA", 1, 100, "account", side=OrderSide.SELL),
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


def test_buy_beyond_cash_and_sell_proceeds_is_still_blocked():
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

    assert [order.order_id for order in accepted] == ["sell_qqq"]
    assert [item.order.order_id for item in blocked] == ["buy_tlt"]
    assert blocked[0].reason == "cash_buying_power_exceeded"


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
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        notional=quantity * price,
        account_id=account_id,
    )
