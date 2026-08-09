import pytest

from maestro.core.enums import Currency, OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import (
    BrokerBuyingPower,
    BuyingPowerCurrencyUnavailable,
)
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


def test_the_whole_us_cohort_fits_inside_the_dollar_buying_power():
    """Regression for the 2026-08-05 Toss block.

    Five USD buys totalling 25,594.50 were rejected against a KRW figure of 2.0
    while the account held 26,072 USD. Given the dollar figure they all fit.
    """
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=26_072.0,
            currency="USD",
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(_us_cohort())

    assert blocked == []
    assert [order.order_id for order in accepted] == [
        "ord_bil",
        "ord_spy",
        "ord_sso",
        "ord_agg",
        "ord_pdbc",
    ]


def test_dollar_reservations_accumulate_until_the_dollars_run_out():
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=26_072.0,
            currency="USD",
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            *_us_cohort(),
            # 25,594.50 is already reserved, leaving 477.50.
            _order("ord_over", "IEF", 10, 95.0, "toss_brokerage", currency=Currency.USD,
                   strategy="crescendo_us"),
        ]
    )

    assert [item.order.order_id for item in blocked] == ["ord_over"]
    assert blocked[0].cash_buying_power == pytest.approx(477.5)
    assert blocked[0].capacity_currency == "USD"
    assert len(accepted) == 5


def test_a_currency_the_broker_cannot_price_blocks_with_its_own_reason():
    def lookup(order):
        raise BuyingPowerCurrencyUnavailable("USD", ["KRW"])

    _, blocked = OrderCapacityService(lookup).partition(
        [_order("ord_bil", "BIL", 102, 91.43, "toss_brokerage", currency=Currency.USD)]
    )

    assert blocked[0].reason == "buying_power_by_currency_unavailable"
    assert blocked[0].requested_currency == "USD"
    assert blocked[0].available_currencies == ["KRW"]
    # Nothing was measured, so no currency can be named as the one measured.
    assert blocked[0].capacity_currency is None
    assert blocked[0].error_type == "BuyingPowerCurrencyUnavailable"


def test_max_buy_quantity_is_reported_in_whole_shares_when_the_step_is_known():
    """0.0218747 shares of BIL is not an order anyone can place."""
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=2.0,
            currency="USD",
            max_buy_quantity=None,
            source="fake",
        ),
        quantity_step=lambda order: 1.0,
    )

    _, blocked = service.partition(
        [_order("ord_bil", "BIL", 102, 91.43, "toss_brokerage", currency=Currency.USD)]
    )

    assert blocked[0].max_buy_quantity == 0.0


def test_a_whole_share_is_not_lost_to_float_division():
    """0.3 / 0.1 is 2.9999999999999996, and the account can afford three."""
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=0.3,
            currency="USD",
            max_buy_quantity=None,
            source="fake",
        ),
        quantity_step=lambda order: 1.0,
    )

    _, blocked = service.partition(
        [_order("ord_penny", "PENNY", 10, 0.1, "toss_brokerage", currency=Currency.USD)]
    )

    assert blocked[0].max_buy_quantity == 3.0


def test_max_buy_quantity_stays_continuous_without_a_step_lookup():
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=2.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    _, blocked = service.partition(
        [_order("ord_bil", "BIL", 102, 91.43, "toss_brokerage", currency=Currency.USD)]
    )

    assert blocked[0].max_buy_quantity == pytest.approx(2.0 / 91.43)


def _us_cohort() -> list[OrderIntent]:
    """The five buys from run_71d88f73a23c42c48c4688d0639c2b8d, one approval group."""
    return [
        _order("ord_bil", "BIL", 102, 91.43, "toss_brokerage",
               currency=Currency.USD, strategy="crescendo_us"),
        _order("ord_spy", "SPY", 8, 775.86, "toss_brokerage",
               currency=Currency.USD, strategy="crescendo_us"),
        _order("ord_sso", "SSO", 89, 72.24, "toss_brokerage",
               currency=Currency.USD, strategy="crescendo_us"),
        _order("ord_agg", "AGG", 25, 97.64, "toss_brokerage",
               currency=Currency.USD, strategy="crescendo_us"),
        _order("ord_pdbc", "PDBC", 70, 17.02, "toss_brokerage",
               currency=Currency.USD, strategy="crescendo_us"),
    ]


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


def test_cash_reservation_is_shared_across_approval_groups():
    """Two strategies draw on one balance, not one each.

    Scoping reservations by strategy group let each of two buy-only groups be
    approved for the whole account balance, and the second one is then rejected
    by the broker.
    """
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=10_000.0,
            max_buy_quantity=None,
            source="fake",
        )
    )

    accepted, blocked = service.partition(
        [
            _order(
                "buy_alpha", "TLT", 100, 100, "toss",
                currency=Currency.USD, strategy="alpha",
            ),
            _order(
                "buy_beta", "SCHD", 100, 100, "toss",
                currency=Currency.USD, strategy="beta",
            ),
        ]
    )

    assert [order.order_id for order in accepted] == ["buy_alpha"]
    assert [item.order.order_id for item in blocked] == ["buy_beta"]
    assert blocked[0].reason == "cash_buying_power_exceeded"
