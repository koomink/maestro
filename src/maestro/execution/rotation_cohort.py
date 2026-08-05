from dataclasses import dataclass

from maestro.core.enums import OrderSide, OrderStatus
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
from maestro.execution.live_order_models import LiveOrderLifecycleResult
from maestro.execution.order_builder import floor_quantity_to_step


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


def rescale_buys_to_cash(
    buys: list[OrderIntent],
    available_cash: float,
    instruments: dict[str, TradableInstrument],
) -> list[OrderIntent]:
    """Fit approved buys inside the cash the sells actually raised.

    Quantities only ever shrink. The operator approved these sizes as a ceiling,
    so surplus cash must not buy more than what they were shown.
    """
    total_notional = sum(order.notional for order in buys)
    if total_notional <= 0:
        return []
    scale = min(1.0, max(0.0, available_cash) / total_notional)
    rescaled: list[OrderIntent] = []
    for order in buys:
        instrument = instruments.get(order.symbol)
        quantity = floor_quantity_to_step(order.quantity * scale, instrument)
        notional = quantity * order.price
        min_quantity = instrument.min_order_quantity if instrument else 0.0
        min_notional = instrument.min_order_notional if instrument else 0.0
        if quantity <= 0 or quantity < min_quantity or notional < min_notional:
            continue
        rescaled.append(order.model_copy(update={"quantity": quantity, "notional": notional}))
    return rescaled
