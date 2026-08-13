from maestro.integrations.telegram.ui.card_state import (
    card_failure_event,
    card_intent_event,
    card_result_event,
    resolve_card_copies,
)


def test_one_logical_card_keeps_a_separate_copy_per_chat():
    """The storage key is (card_key, chat_id), not card_key.

    With a single operator chat today the natural implementation collapses
    this, and then a second chat only ever sees the first render.
    """
    events = [
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
        card_result_event("approval:appr_1", 200, "pending", "h1", "op2", 5002),
    ]

    copies = resolve_card_copies(events)

    assert set(copies) == {("approval:appr_1", 100), ("approval:appr_1", 200)}
    assert copies[("approval:appr_1", 100)].message_id == 5001
    assert copies[("approval:appr_1", 200)].message_id == 5002


def test_an_intent_without_a_result_is_unknown_not_undelivered():
    """The crash window must read as "we do not know", never as "not sent".

    If sendMessage succeeded and the process died before the result was
    written, the card exists in Telegram with no message_id here. Calling that
    undelivered invites a resend that duplicates a card nobody can update.
    """
    events = [card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")]

    copy = resolve_card_copies(events)[("approval:appr_1", 100)]

    assert copy.delivery == "unknown"
    assert copy.message_id is None


def test_a_caught_error_records_a_known_failure():
    """Distinct from unknown: Telegram refused, so it never landed."""
    events = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_failure_event("approval:appr_1", 100, "pending", "h1", "op1", "chat not found"),
    ]

    assert resolve_card_copies(events)[("approval:appr_1", 100)].delivery == "failed"


def test_a_result_supersedes_its_intent():
    events = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
    ]

    copy = resolve_card_copies(events)[("approval:appr_1", 100)]

    assert copy.delivery == "confirmed"
    assert copy.message_id == 5001


def test_a_retry_at_the_same_stage_gets_a_distinct_duplicate_key():
    """system_events has a UNIQUE index on duplicate_key (store.py:203).

    Without a per-attempt id the second attempt at one stage dies with
    IntegrityError, which would make retry impossible by construction.
    """
    first = card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")
    second = card_intent_event("approval:appr_1", 100, "pending", "h1", "op2")

    assert first["duplicate_key"] != second["duplicate_key"]
    assert first["operation_id"] == "op1"


def test_a_later_stage_supersedes_an_earlier_one_for_that_chat_only():
    events = [
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
        card_result_event("approval:appr_1", 200, "pending", "h1", "op2", 5002),
        card_result_event("approval:appr_1", 100, "done", "h2", "op3", 5001),
    ]

    copies = resolve_card_copies(events)

    assert copies[("approval:appr_1", 100)].stage == "done"
    assert copies[("approval:appr_1", 200)].stage == "pending"


def test_events_are_ordered_by_arrival_not_by_stage_name():
    """Out-of-order arrival is explicitly in scope (spec, card section A)."""
    events = [
        card_result_event("approval:appr_1", 100, "done", "h2", "op1", 5001),
        card_result_event("approval:appr_1", 100, "in_progress", "h3", "op2", 5001),
    ]

    assert resolve_card_copies(events)[("approval:appr_1", 100)].stage == "in_progress"


def test_intent_and_result_of_one_attempt_share_an_operation_id():
    """3a-3 has to pair an outcome with the intent that caused it."""
    intent = card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")
    result = card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001)

    assert intent["operation_id"] == result["operation_id"] == "op1"
    assert intent["duplicate_key"] == "telegram-ui-card:intent:approval:appr_1:100:pending:op1"
    assert result["duplicate_key"] == "telegram-ui-card:result:approval:appr_1:100:pending:op1"


def test_events_must_be_folded_oldest_first():
    """The store returns ORDER BY id DESC (store.py:1815).

    Feeding that straight in makes the oldest event win, so a delivered card
    reads back as unknown and the sweep sends it again. This pins the direction
    the resolver expects; callers are responsible for reversing.
    """
    oldest_first = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
    ]

    assert resolve_card_copies(oldest_first)[("approval:appr_1", 100)].delivery == "confirmed"
    assert (
        resolve_card_copies(list(reversed(oldest_first)))[("approval:appr_1", 100)].delivery
        == "unknown"
    )
