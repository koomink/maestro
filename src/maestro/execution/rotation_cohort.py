from dataclasses import dataclass

from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.base import OrderIntent
from maestro.execution.live_order_models import LiveOrderLifecycleResult


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


@dataclass(frozen=True)
class SellPhaseOutcome:
    complete: bool
    reason: str | None
    unfilled: tuple[LiveOrderLifecycleResult, ...]


def evaluate_sell_phase(results: list[LiveOrderLifecycleResult]) -> SellPhaseOutcome:
    """Decide whether the buy phase may run.

    Only a completely filled sell releases the cash the buy was sized against, so
    anything short of FILLED — partial, rejected, still working at the deadline —
    stops the cohort.
    """
    unfilled = tuple(result for result in results if result.final_status != OrderStatus.FILLED)
    if not unfilled:
        return SellPhaseOutcome(complete=True, reason=None, unfilled=())
    statuses = sorted({result.final_status.value for result in unfilled})
    return SellPhaseOutcome(
        complete=False,
        reason="sell_phase_incomplete:" + ",".join(statuses),
        unfilled=unfilled,
    )
