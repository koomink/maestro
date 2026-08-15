"""Classify what actually happened to the orders of one approved batch.

A rotation sells first and buys with the proceeds, so a batch that stops
half way is not "one order did not fill" -- it is "we sold but could not
buy". Deciding what to do about that needs the batch read as a unit, and
read from evidence: `live_order_submit_intent`, `live_order_result` and the
reconciled `fill_watermarks`.

The one rule everything here serves: **an order we have no result for is
`unknown`, not "did not go out".** Folding `unknown` into `not_sent` would
let a re-order go out against an order the broker may already be holding.
That is the same reason stage 2 keeps a third value for card delivery.

Pure functions only. Reading the evidence is `StateStore`'s job and
rendering it is stage 4b's; keeping the classification free of both is what
lets the card reuse it unchanged.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel

from maestro.execution.live_order_tracking import TERMINAL_ORDER_STATUSES

OrderOutcome = Literal[
    "not_sent",
    "unknown",
    "filled",
    "partially_filled",
    "cancelled_unfilled",
    "still_open",
]

_TERMINAL_STATUS_VALUES = {status.value for status in TERMINAL_ORDER_STATUSES}


class OrderEvidence(BaseModel):
    """What the record says about a single order of a batch."""

    order_id: str
    symbol: str
    side: str
    ordered_quantity: float
    has_intent: bool
    has_result: bool
    filled_quantity: float = 0.0
    final_status: str | None = None
    broker_order_id: str | None = None


class OrderLine(OrderEvidence):
    """An order's evidence together with the outcome it was classified to."""

    outcome: OrderOutcome


class BatchOutcome(BaseModel):
    approval_id: str
    orders: list[OrderLine]
    counts: dict[str, int]
    has_unknown: bool


def classify_order(evidence: OrderEvidence) -> OrderOutcome:
    """Classify one order from its recorded evidence alone."""
    if not evidence.has_intent:
        return "not_sent"
    if not evidence.has_result:
        # The intent is on record but no result is. We cannot tell whether
        # the broker saw it, and "cannot tell" must not be reported as "no".
        return "unknown"
    if evidence.filled_quantity > 0:
        # A broker correction can report more than we asked for; that is
        # still a completed fill, not a partial one.
        if evidence.filled_quantity >= evidence.ordered_quantity:
            return "filled"
        return "partially_filled"
    if evidence.final_status in _TERMINAL_STATUS_VALUES:
        return "cancelled_unfilled"
    # Nothing filled and nobody has seen the order end: it may still be
    # resting at the broker.
    return "still_open"


def summarize_batch(approval_id: str, evidence: list[OrderEvidence]) -> BatchOutcome:
    """Classify every order of a batch and summarize the batch as a whole."""
    lines = [
        OrderLine(**item.model_dump(), outcome=classify_order(item)) for item in evidence
    ]
    counts = Counter(line.outcome for line in lines)
    return BatchOutcome(
        approval_id=approval_id,
        orders=lines,
        counts=dict(counts),
        has_unknown=any(line.outcome == "unknown" for line in lines),
    )


__all__ = [
    "BatchOutcome",
    "OrderEvidence",
    "OrderLine",
    "OrderOutcome",
    "classify_order",
    "summarize_batch",
]
