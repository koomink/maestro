from dataclasses import dataclass

from maestro.core.enums import OrderSide
from maestro.execution.base import OrderIntent


@dataclass(frozen=True)
class RotationCohort:
    """Orders that draw on one broker cash pool.

    A sell can only fund a buy that settles against the same account and
    currency, so that pair — not the execution sleeve — is the boundary the
    sell-then-buy barrier runs on.
    """

    account_id: str | None
    currency: str | None
    sells: tuple[OrderIntent, ...]
    buys: tuple[OrderIntent, ...]


def _cohort_key(order: OrderIntent) -> tuple[str | None, str | None]:
    currency = order.currency.value if order.currency is not None else order.sleeve
    return (order.account_id, currency)


def split_rotation_cohorts(orders: list[OrderIntent]) -> list[RotationCohort]:
    """Group orders into one cohort per account and currency, order preserved."""
    grouped: dict[tuple[str | None, str | None], tuple[list[OrderIntent], list[OrderIntent]]] = {}
    for order in orders:
        sells, buys = grouped.setdefault(_cohort_key(order), ([], []))
        if order.side == OrderSide.SELL:
            sells.append(order)
        else:
            buys.append(order)
    return [
        RotationCohort(
            account_id=account_id,
            currency=currency,
            sells=tuple(sells),
            buys=tuple(buys),
        )
        for (account_id, currency), (sells, buys) in grouped.items()
    ]
