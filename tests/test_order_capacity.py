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
