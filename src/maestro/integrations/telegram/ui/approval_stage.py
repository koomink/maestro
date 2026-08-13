"""Approval payloads -> what the card should show. Pure functions.

Two axes, deliberately separate. Progress is how far the run got and never
walks back; attention is whether a human still has to look and clears when the
incident does. Collapsing them into one ranked value -- with attention on top
and backward transitions forbidden -- means a recovered incident can never be
cleared, so a real new one becomes indistinguishable from an old resolved one.

Decisions read the payload, never the event type alone. The 2026-08-12 US run
recorded signal_approval_completed with approval_status='approved' and
orders_failed=1: a half-executed rotation that a type-only mapping would show
as finished.
"""

from collections.abc import Mapping
from typing import Any

PROGRESS_RANK = {"pending": 0, "in_progress": 1, "done": 2}

#: Terminal approval decisions. Anything else on an ack is unexpected and gets
#: a human's attention rather than a guess.
_TERMINAL_ACK_STATUSES = frozenset({"approved", "rejected", "expired"})
_CLEAN_COMPLETION_STATUSES = frozenset({"approved", "not_required"})


def approval_progress(
    ack: Mapping[str, Any] | None,
    completed: Mapping[str, Any] | None,
) -> str:
    """How far this approval got. Monotonic -- never walks back."""
    if completed is not None:
        return "done"
    if ack is not None:
        status = str(ack.get("status") or "")
        if status == "approved":
            return "in_progress"
        if status in {"rejected", "expired"}:
            return "done"
    return "pending"


def approval_needs_attention(
    ack: Mapping[str, Any] | None,
    completed: Mapping[str, Any] | None,
    unresolved_recovery: bool,
    unresolved_failure: bool = False,
) -> bool:
    """Whether a human still has to look.

    Recomputed every sweep, so a resolved recovery drops the flag. An order
    that actually failed does not: that is a fact about the past with no way to
    close it out until the stage 4 exception wizard exists, and letting it
    lapse would render a half-executed rotation as complete.

    unresolved_failure covers the resolution that raised and never completed.
    Without it an approval whose execution failed outright reads as "🔵 진행
    중" forever -- the operator waits for a run that already stopped.
    """
    if unresolved_recovery or unresolved_failure:
        return True
    if completed is not None:
        failed = int(completed.get("orders_failed") or 0)
        status = str(completed.get("approval_status") or "")
        if failed > 0 or status not in _CLEAN_COMPLETION_STATUSES:
            return True
    if ack is not None and str(ack.get("status") or "") not in _TERMINAL_ACK_STATUSES:
        return True
    return False


def card_stage(progress: str, needs_attention: bool) -> str:
    """What the card shows. Attention wins the display, not the history."""
    return "attention" if needs_attention else progress


def keep_forward_progress(previous: str | None, current: str) -> str:
    """Ignore a transition that would undo how far the run got.

    Out-of-order event arrival is explicitly in scope, and a status that lands
    late must not turn a completed run back into one in progress.
    """
    if previous is None:
        return current
    if PROGRESS_RANK.get(current, 0) < PROGRESS_RANK.get(previous, 0):
        return previous
    return current
