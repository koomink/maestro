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

from migration_fixtures import (  # noqa: F401 - `store` is a fixture
    claim_and_complete,
    delete_events,
    legacy_pending_request,
    legacy_terminal_event,
    publish_current_request,
    store,
    workflow_id,
)

from maestro.state import funding_workflow as fw


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


def test_a_superseded_request_is_terminal(store):
    publish_current_request(store, "req-1")
    publish_current_request(store, "req-2")

    assert fw.request_terminal_state(store, "req-1", "funding") == "superseded"
    assert fw.is_request_pending(store, "req-1", "funding") is False
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
