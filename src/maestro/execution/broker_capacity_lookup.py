"""Ask a broker how much of an order's own currency the account can spend.

Two call sites need this — the pre-approval capacity partition and Telegram's
retry review — and they have to agree, because a retry that the first check
would have blocked is a retry the operator cannot submit. Keeping the currency
resolution and the currency check here is what makes them agree.

Building the read-only service, routing the account and caching the client stay
with the callers: the orchestrator holds a per-account client cache that a
factory call from in here would bypass.
"""

from maestro.config.models import MaestroConfig
from maestro.core.enums import Currency
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import (
    BrokerBuyingPower,
    BrokerReadOnlyClient,
    BuyingPowerCurrencyUnavailable,
)


def resolve_order_currency(config: MaestroConfig, order: OrderIntent) -> Currency:
    """The currency an order settles in.

    Orders normally carry it. When they do not, the instrument's own currency is
    authoritative, and the portfolio's base currency is the last resort.
    """
    if order.currency is not None:
        return order.currency
    instrument = config.universe.get(order.symbol)
    if instrument is not None:
        return instrument.currency
    return Currency(config.portfolio.base_currency)


def get_order_buying_power(
    client: BrokerReadOnlyClient,
    config: MaestroConfig,
    broker: str,
    order: OrderIntent,
) -> BrokerBuyingPower:
    """Buying power for `order`, in `order`'s currency.

    Raises `BuyingPowerCurrencyUnavailable` when the broker cannot answer for
    that currency, or answers in a different one — a dollar buy routed to a won
    account has to stop here rather than be judged against won.
    """
    currency = resolve_order_currency(config, order)
    instrument = config.universe.get(order.symbol)
    symbol = instrument.symbol_for_broker(broker) if instrument is not None else order.symbol
    return check_capacity_currency(
        client.get_buying_power(symbol, order.price, currency=currency.value),
        currency,
    )


def check_capacity_currency(
    capacity: BrokerBuyingPower,
    currency: Currency,
) -> BrokerBuyingPower:
    """Reject a figure that is not denominated in the order's own currency.

    An unnamed currency is rejected too. A number whose currency cannot be
    established is exactly the input that let a won balance block a dollar
    order; trusting it because the adapter happens to be routed correctly today
    leaves the same hole open for the next adapter.
    """
    if capacity.currency != currency.value:
        available = [capacity.currency] if capacity.currency is not None else []
        raise BuyingPowerCurrencyUnavailable(currency.value, available)
    return capacity


__all__ = [
    "check_capacity_currency",
    "get_order_buying_power",
    "resolve_order_currency",
]
