"""After 3a-5 the workflow -- not the legacy contribution_* event -- decides.

The legacy ``contribution_funding_request_ack`` /
``contribution_budget_request_decision`` events are still written, atomically,
by ``complete_workflow``. They exist so a rollback to the pre-3a-4 binary can
still tell a finished request from a live one. What they are no longer allowed
to be is a second opinion the *current* runtime consults, because the two
answers can differ (the legacy event knows nothing about phase, attempt or
supersession) and a system with two definitions of "finished" eventually acts
on the wrong one.

The pattern that proves the separation runs in both directions and appears in
several tests below: delete the projection, and the current reader must still
understand its own completion -- while tests/test_rollback_preflight.py checks
that the very same database is refused for rollback.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    claim_and_complete,
    delete_events,
    legacy_pending_request,
    legacy_terminal_event,
    make_store,
    publish_current_request,
    workflow_id,
)

from test_funding_workflow_resume import operator_bot  # noqa: F401 - pytest fixture

from maestro.state import funding_workflow as fw


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def test_a_completed_workflow_is_terminal_without_any_legacy_projection(store):
    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1", phase="funding")
    assert delete_events(store, "contribution_funding_request_ack") == 1

    assert fw.request_terminal_state(store, "req-1", "funding") == "completed"
    assert fw.is_request_pending(store, "req-1", "funding") is False


def test_a_legacy_ack_alone_no_longer_makes_a_request_terminal(store):
    """Pre-3a-4 history: an ack with no workflow completion behind it.

    It is historical state, not current authoritative truth. The reader reports
    the request as still open; the upgrade backfill -- under a quiesce barrier,
    with the whole database in front of it -- is what classifies it, not a
    runtime read guessing one row at a time.
    """
    legacy_pending_request(store, "req-1")
    legacy_terminal_event(store, "req-1")

    assert fw.request_terminal_state(store, "req-1", "funding") is None
    assert fw.is_request_pending(store, "req-1", "funding") is True


def test_a_superseded_request_is_reported_terminal_but_stays_pending(store):
    """Two different questions, and 3a-5 must not merge them.

    ``request_terminal_state`` reports the supersession because rollback
    preflight and the upgrade backfill need to know the request's history is
    over. ``is_request_pending`` does not screen on it, because whether the
    transition may still be entered is decided atomically by the head inside
    claim_workflow_attempt -- which can tell a legitimate successor's parent
    (still resumable) from an ordinary replacement (refused as ``not_head``).
    """
    publish_current_request(store, "req-1")
    publish_current_request(store, "req-2")

    assert fw.request_terminal_state(store, "req-1", "funding") == "superseded"
    assert fw.is_request_pending(store, "req-1", "funding") is True
    assert fw.is_request_pending(store, "req-2", "funding") is True


def test_phase_is_not_ignored_when_deciding_completion(store):
    """A completion recorded for one phase must not close the other.

    The completion row is written here directly rather than through
    claim_and_complete: the claim path would refuse a budget attempt on a
    funding head (``head_corrupt``), which is the protection working. What is
    under test is narrower -- that the reader matches on phase and not on
    request_id alone, so a stray or mis-phased row cannot close a live request.
    """
    publish_current_request(store, "req-1", phase="funding")
    store.save_system_event(
        "run_req-1",
        "funding_workflow_completed",
        {
            "duplicate_key": fw.completed_key(workflow_id(), "req-1", "budget"),
            "workflow_id": workflow_id(),
            "request_id": "req-1",
            "phase": "budget",
            "attempt": 1,
        },
    )

    assert fw.request_terminal_state(store, "req-1", "funding") is None
    assert fw.is_request_pending(store, "req-1", "funding") is True
    assert fw.request_terminal_state(store, "req-1", "budget") == "completed"


def test_a_non_pending_request_is_not_pending(store):
    legacy_pending_request(store, "req-1", status="canceled")

    assert fw.is_request_pending(store, "req-1", "funding") is False


def test_an_unknown_request_has_no_payload_and_is_not_pending(store):
    assert fw.load_request_payload(store, "nope", "funding") is None
    assert fw.is_request_pending(store, "nope", "funding") is False


# --- the runtime loaders --------------------------------------------------
#
# Same three scenarios as above, read through the code that actually decides
# whether an operator's tap is allowed to move money.


def test_the_pending_loader_does_not_need_the_legacy_projection(operator_bot):
    """Current runtime truth is not the same thing as rollback compatibility.

    tests/test_rollback_preflight.py runs this exact database the other way
    round and refuses the rollback over the missing projection. Both are
    correct at once, and neither is derivable from the other -- which is the
    point of keeping the projection while not reading it.
    """
    store = operator_bot.store
    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1", phase="funding")
    assert delete_events(store, "contribution_funding_request_ack") == 1

    assert operator_bot._load_pending_funding_request("req-1") is None


def test_a_bare_legacy_ack_no_longer_hides_a_live_request(operator_bot):
    """Before 3a-5 this returned None purely because an ack row existed.

    Now the workflow decides, and a request the workflow still owns stays
    visible so the migration -- not a silent runtime read -- classifies it.
    """
    store = operator_bot.store
    legacy_pending_request(store, "req-1")
    legacy_terminal_event(store, "req-1")

    assert operator_bot._load_pending_funding_request("req-1") is not None


def test_the_budget_loader_follows_the_same_rule(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-b", phase="budget")
    claim_and_complete(store, "req-b", phase="budget")
    assert delete_events(store, "contribution_budget_request_decision") == 1

    assert operator_bot._load_pending_budget_request("req-b") is None


def test_a_superseded_request_is_still_loaded_so_the_head_can_refuse_it(operator_bot):
    """The refusal belongs to the claim, not the loader.

    claim_workflow_attempt checks the head inside the same transaction as the
    write, so it cannot be raced, and it can distinguish an ordinary
    replacement from a legitimate successor's still-resumable parent. Screening
    supersession out here would answer "no longer active" to both and lose the
    "already processed or superseded" message the operator actually needs.
    """
    store = operator_bot.store
    publish_current_request(store, "req-1")
    publish_current_request(store, "req-2")

    assert operator_bot._load_pending_funding_request("req-1") is not None
    assert operator_bot._load_pending_funding_request("req-2") is not None
