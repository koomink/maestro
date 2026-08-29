"""The card delivery record: one logical card, one copy per chat.

Pure functions over the event list. ``lifecycle.py`` owns the network and the
store; keeping the interpretation here means it can be tested without either.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NamedTuple

CardStage = Literal["pending", "in_progress", "done", "attention"]
Delivery = Literal["confirmed", "failed", "unknown"]

EVENT_TYPE = "telegram_ui_card"

#: Three straight failures means the card path is not working for this chat:
#: the lifecycle falls back to plain text and the telegram_ui health check
#: degrades. Both read this one number so they cannot drift apart -- a
#: fallback the operator sees while health still reports ok is the confusing
#: half-state this constant exists to prevent.
FALLBACK_AFTER_FAILURES = 3

# Only an explicit Telegram rejection proves non-delivery. An intent left
# without any outcome means the process died mid-call, which is not the same
# thing and must never be treated as one.
_PHASE_DELIVERY: dict[str, str] = {
    "intent": "unknown",
    "result": "confirmed",
    "failure": "failed",
}


class CardCopy(NamedTuple):
    card_key: str
    chat_id: int
    message_id: int | None
    stage: str
    render_hash: str
    delivery: str
    operation_id: str
    #: Consecutive known failures for this copy; reset by a confirmed send.
    #: Only meaningful when read from the projection.
    consecutive_failures: int = 0


def new_operation_id() -> str:
    return uuid.uuid4().hex[:16]


def _duplicate_key(
    phase: str, card_key: str, chat_id: int, stage: str, operation_id: str
) -> str:
    # operation_id is what makes a retry writable at all: system_events has a
    # UNIQUE index on duplicate_key (store.py:203), so a key that stopped at
    # the stage would make the second attempt raise IntegrityError.
    return f"telegram-ui-card:{phase}:{card_key}:{chat_id}:{stage}:{operation_id}"


def _event(
    phase: str,
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "phase": phase,
        # Carried on the payload so the store can project it without importing
        # this module -- state/ must not depend on an integration.
        "delivery": _PHASE_DELIVERY[phase],
        "card_key": card_key,
        "chat_id": chat_id,
        "stage": stage,
        "render_hash": render_hash,
        "operation_id": operation_id,
        "duplicate_key": _duplicate_key(phase, card_key, chat_id, stage, operation_id),
        **extra,
    }


def card_intent_event(
    card_key: str, chat_id: int, stage: str, render_hash: str, operation_id: str
) -> dict[str, Any]:
    """Written before the Telegram call, so a crash mid-send leaves a trace."""
    return _event("intent", card_key, chat_id, stage, render_hash, operation_id)


def card_result_event(
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    message_id: int,
) -> dict[str, Any]:
    """Written after the call. message_id does not exist before it."""
    return _event(
        "result", card_key, chat_id, stage, render_hash, operation_id, message_id=message_id
    )


def card_failure_event(
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    error: str,
    *,
    method: str | None = None,
    error_code: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Written when Telegram answered ok=false: it looked and refused.

    This is what separates a known failure from the unknown of a lost
    connection. Only a known failure may be retried automatically.
    """
    payload: dict[str, Any] = {"error": error}
    if method is not None:
        payload["method"] = method
    if error_code is not None:
        payload["error_code"] = error_code
    if description is not None:
        payload["description"] = description
    return _event(
        "failure",
        card_key,
        chat_id,
        stage,
        render_hash,
        operation_id,
        **payload,
    )


def resolve_card_copies(
    events: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], CardCopy]:
    """Fold the event list into the current state of every delivery copy.

    **Events must arrive oldest first.** ``list_system_events_by_type`` returns
    ``ORDER BY id DESC`` (store.py:1815), so callers reverse it; folding the
    store's order directly makes the oldest event win and a delivered card read
    back as unknown.

    Within that order the last event wins, in arrival order -- not in stage
    order. A status that arrives out of sequence still describes what we last
    knew, and inventing a stage ranking here would silently discard
    corrections.
    """
    copies: dict[tuple[str, int], CardCopy] = {}
    for event in events:
        card_key = str(event.get("card_key") or "")
        raw_chat_id = event.get("chat_id")
        if not card_key or not isinstance(raw_chat_id, int):
            continue
        key = (card_key, raw_chat_id)
        previous = copies.get(key)
        message_id = event.get("message_id")
        copies[key] = CardCopy(
            card_key=card_key,
            chat_id=raw_chat_id,
            message_id=(
                int(message_id)
                if isinstance(message_id, int)
                else (previous.message_id if previous else None)
            ),
            stage=str(event.get("stage") or ""),
            render_hash=str(event.get("render_hash") or ""),
            delivery=_PHASE_DELIVERY.get(str(event.get("phase") or ""), "unknown"),
            operation_id=str(event.get("operation_id") or ""),
        )
    return copies
