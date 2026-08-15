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
from typing import Any, Literal

from pydantic import BaseModel

from maestro.core.clock import utc_now
from maestro.execution.live_order_tracking import TERMINAL_ORDER_STATUSES
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import save_audited_system_event
from maestro.state.store import StateStore

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


def build_order_evidence(evidence: dict[str, Any]) -> list[OrderEvidence]:
    """Turn `StateStore.load_approval_execution_evidence` into per-order facts.

    The roster is the envelope's proposed orders, so an order that was never
    submitted -- and therefore has no event anywhere -- still appears in its
    own batch.
    """
    envelope = evidence.get("envelope") or {}
    request = envelope.get("request") if isinstance(envelope, dict) else None
    proposed = (request or {}).get("proposed_orders") or []
    intents = evidence.get("intents") or {}
    results = evidence.get("results") or {}
    fills = evidence.get("fills") or {}
    final_statuses = evidence.get("final_statuses") or {}

    lines: list[OrderEvidence] = []
    for order in proposed:
        order_id = str(order.get("order_id"))
        result = results.get(order_id)
        broker_order_id = _broker_order_id(result)
        lines.append(
            OrderEvidence(
                order_id=order_id,
                symbol=str(order.get("symbol") or ""),
                side=str(order.get("side") or ""),
                ordered_quantity=float(order.get("quantity") or 0.0),
                has_intent=order_id in intents,
                has_result=result is not None,
                filled_quantity=_filled_quantity(result, fills, broker_order_id),
                final_status=final_statuses.get(order_id),
                broker_order_id=broker_order_id,
            )
        )
    return lines


def _broker_order_id(result: dict[str, Any] | None) -> str | None:
    broker_order = ((result or {}).get("result") or {}).get("broker_order") or {}
    value = broker_order.get("broker_order_id")
    return str(value) if value else None


def _filled_quantity(
    result: dict[str, Any] | None,
    fills: dict[str, float],
    broker_order_id: str | None,
) -> float:
    """The largest fill any record attests to.

    The result event holds the fill known at submit time, which is normally
    zero; the watermark holds the reconciled cumulative fill and lands later.
    Both are lower bounds on what filled, so the larger is the honest answer
    and neither being missing can talk the other down.
    """
    at_submit = float(((result or {}).get("result") or {}).get("filled_quantity") or 0.0)
    reconciled = float(fills.get(broker_order_id or "", 0.0))
    return max(at_submit, reconciled)


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


class SettlementRefused(Exception):
    """Settlement was refused because the batch cannot be described honestly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def settle_approval(
    store: StateStore,
    audit: AuditLogger,
    approval_id: str,
    *,
    reason: str,
    reconciled_with_broker: bool = False,
) -> BatchOutcome:
    """Close a half-executed approval on the operator's word.

    This is the only way to close an approval whose execution stopped part
    way, and it is the last word: preflight, the resume sweep and the card
    sweep all read `telegram_approval_resolution_completed` as terminal.

    Two things it deliberately does not do. It does not place orders --
    recalculating and re-sending is stage 4b, and settling is what makes that
    safe rather than a substitute for it. And it does not write `attempt`,
    because the deployed `_deliver_resume_completion_notices` treats a
    resolution event with `attempt >= 2` as a resume worth telling the
    operator about; nothing executed here, so claiming an attempt would be
    false and would send exactly the wrong message.
    """
    evidence = store.load_approval_execution_evidence(approval_id)
    ack = evidence["ack"]
    if ack is None:
        raise SettlementRefused("no_ack", f"{approval_id} has no ack to settle")
    schema_version = ack.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 2:
        # Pre-3a acks never had a resolution event, so they are already
        # terminal everywhere that reads them. Settling one would invent a
        # closure for something that was never open.
        raise SettlementRefused(
            "legacy_ack", f"{approval_id} predates two-phase persistence"
        )
    if evidence["resolution_completed"] is not None:
        raise SettlementRefused("already_settled", f"{approval_id} is already closed")

    outcome = summarize_batch(approval_id, build_order_evidence(evidence))
    if outcome.has_unknown and not reconciled_with_broker:
        unknown = [line.symbol for line in outcome.orders if line.outcome == "unknown"]
        raise SettlementRefused(
            "unknown_orders",
            f"{approval_id} has orders that may be live at the broker: "
            f"{', '.join(unknown)}. Check the broker, then pass "
            "--i-have-reconciled-with-broker.",
        )

    envelope = evidence["envelope"] or {}
    duplicate_key = f"telegram-approval-settled:{approval_id}"
    payload = {
        "approval_id": approval_id,
        "signal_run_id": ack.get("signal_run_id") or envelope.get("signal_run_id"),
        "status": ack.get("status"),
        "settled_by": "operator",
        "reason": reason,
        "reconciled_with_broker": reconciled_with_broker,
        "outcome": outcome.model_dump(mode="json"),
        "settled_at": utc_now().isoformat(),
        "duplicate_key": duplicate_key,
    }
    # Same convention as the sibling records: check and write under one lock,
    # so a concurrent settle loses the insert rather than the check.
    with store.writer_lock("telegram_approval_settlement"):
        if store.duplicate_key_exists(duplicate_key):
            raise SettlementRefused("already_settled", f"{approval_id} is already closed")
        save_audited_system_event(
            store,
            audit,
            str(envelope.get("run_id") or f"run_{approval_id}"),
            "telegram_approval_resolution_completed",
            payload,
        )
    return outcome


__all__ = [
    "BatchOutcome",
    "SettlementRefused",
    "build_order_evidence",
    "settle_approval",
    "OrderEvidence",
    "OrderLine",
    "OrderOutcome",
    "classify_order",
    "summarize_batch",
]
