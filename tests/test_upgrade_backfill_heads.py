"""Legacy funding/budget requests get a v1 head only where history is unambiguous.

Everywhere else the migration writes a quarantine record and stops, because the
alternative is choosing which of two requests owns a month's investment on no
evidence -- and the wrong choice moves money.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    claim_and_complete,
    legacy_pending_request,
    legacy_terminal_event,
    make_store,
    max_event_id,
    publish_current_request,
    workflow_id,
)

from maestro.state import upgrade_backfill as ub


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def _backfill(store, run_id="run-1", *, cutoff=None):
    with store.writer_lock("test"):
        return ub.backfill_funding_heads(
            store, run_id, cutoff=max_event_id(store) if cutoff is None else cutoff
        )


def _raw_head(store, workflow, *, request_id, version=1):
    from maestro.state.funding_workflow import head_key

    store.save_system_event(
        "run-raw",
        "funding_workflow_head",
        {
            "duplicate_key": head_key(workflow, version),
            "workflow_id": workflow,
            "version": version,
            "request_id": request_id,
            "phase": "funding",
            "status": "pending",
        },
    )


def test_one_unambiguous_pending_request_gets_a_v1_head(store):
    legacy_pending_request(store, "req-1")

    report = _backfill(store)

    assert report.heads_created == 1
    head = store.load_funding_workflow_head(workflow_id())
    assert head["request_id"] == "req-1"
    assert head["version"] == 1
    assert head["phase"] == "funding"
    assert head["reason"] == "legacy_backfill_v1"


def test_a_budget_request_gets_a_head_under_its_own_phase(store):
    legacy_pending_request(store, "req-b", phase="budget")

    report = _backfill(store)

    assert report.heads_created == 1
    assert store.load_funding_workflow_head(workflow_id())["phase"] == "budget"


def test_separate_scopes_get_separate_heads(store):
    legacy_pending_request(store, "req-krw", currency="KRW")
    legacy_pending_request(store, "req-usd", currency="USD")

    report = _backfill(store)

    assert report.heads_created == 2
    assert store.load_funding_workflow_head(workflow_id(currency="KRW"))["request_id"] == "req-krw"
    assert store.load_funding_workflow_head(workflow_id(currency="USD"))["request_id"] == "req-usd"


def test_a_legacy_acked_request_is_not_resurrected(store):
    """The ack is history, not current truth -- but it is still proof this
    request's transition happened. Giving it a live head would put a finished
    request back in front of the operator with a working Confirm button."""
    legacy_pending_request(store, "req-1")
    legacy_terminal_event(store, "req-1")

    report = _backfill(store)

    assert report.heads_created == 0
    assert report.terminal_skipped == 1
    assert store.load_funding_workflow_head(workflow_id()) is None


def test_a_legacy_budget_decision_is_terminal_too(store):
    legacy_pending_request(store, "req-b", phase="budget")
    legacy_terminal_event(store, "req-b", phase="budget", status="selected")

    report = _backfill(store)

    assert report.heads_created == 0
    assert report.terminal_skipped == 1


def test_a_workflow_completion_also_proves_terminal_history(store):
    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1")
    cutoff = max_event_id(store)

    report = _backfill(store, cutoff=cutoff)

    assert report.heads_created == 0
    assert report.terminal_skipped == 1


def test_a_non_pending_request_is_not_a_candidate(store):
    legacy_pending_request(store, "req-1", status="canceled")

    report = _backfill(store)

    assert report.legacy_requests_inspected == 1
    assert report.heads_created == 0
    assert report.terminal_skipped == 1


def test_two_pending_requests_in_one_workflow_are_quarantined_not_guessed(store):
    """Picking a winner assigns this month's investment to one of two requests
    on no evidence at all."""
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-2")

    report = _backfill(store)

    assert report.heads_created == 0
    assert [q.reason for q in report.blocking] == ["ambiguous_pending_requests"]
    assert report.blocking[0].detail["request_ids"] == ["req-1", "req-2"]
    assert store.load_funding_workflow_head(workflow_id()) is None


def test_an_ambiguous_workflow_does_not_stop_a_clean_one(store):
    legacy_pending_request(store, "req-a1", month_key="2026-08")
    legacy_pending_request(store, "req-a2", month_key="2026-08")
    legacy_pending_request(store, "req-clean", month_key="2026-09")

    report = _backfill(store)

    assert report.heads_created == 1
    assert len(report.blocking) == 1
    assert store.load_funding_workflow_head(workflow_id("2026-09"))["request_id"] == "req-clean"


def test_a_coherent_existing_head_is_idempotent(store):
    legacy_pending_request(store, "req-1")
    cutoff = max_event_id(store)
    _backfill(store, cutoff=cutoff)

    report = _backfill(store, "run-2", cutoff=cutoff)

    assert report.heads_created == 0
    assert report.heads_already_coherent == 1
    assert len(store.list_system_events_by_type("funding_workflow_head", limit=None)) == 1


def test_a_post_cutoff_successor_head_is_preserved_not_overwritten(store):
    """3a already published a newer request for this workflow. That lineage is
    coherent and current; the pre-cutoff request is inert history, and nothing
    the migration writes may take the head away from the live one."""
    legacy_pending_request(store, "req-old")
    cutoff = max_event_id(store)
    publish_current_request(store, "req-new")

    report = _backfill(store, cutoff=cutoff)

    assert report.heads_created == 0
    assert report.superseded_by_newer == 1
    assert store.load_funding_workflow_head(workflow_id())["request_id"] == "req-new"


def test_a_head_pointing_somewhere_unprovable_blocks_the_migration(store):
    """The head names a request no event in this database records. Overwriting
    it would be inventing ownership; leaving it silently would leave the
    workflow pointing at nothing."""
    legacy_pending_request(store, "req-1")
    _raw_head(store, workflow_id(), request_id="req-ghost")

    report = _backfill(store)

    assert [q.reason for q in report.blocking] == ["head_ownership_conflict"]
    assert report.blocking[0].detail["head_request_id"] == "req-ghost"


def test_a_head_naming_another_pre_cutoff_request_blocks_the_migration(store):
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-other", month_key="2026-08", status="canceled")
    _raw_head(store, workflow_id(), request_id="req-other")

    report = _backfill(store)

    assert [q.reason for q in report.blocking] == ["head_ownership_conflict"]


def test_a_request_with_no_month_key_is_quarantined(store):
    store.save_system_event(
        "run-bad",
        "contribution_funding_request",
        {
            "request_id": "req-bad",
            "status": "pending",
            "duplicate_key": "contribution_funding_request:req-bad",
        },
    )

    report = _backfill(store)

    assert [q.reason for q in report.blocking] == ["malformed_workflow_identity"]
    assert report.blocking[0].identifier == "req-bad"


def test_a_post_cutoff_request_is_not_a_backfill_candidate(store):
    cutoff = max_event_id(store)
    legacy_pending_request(store, "req-1")

    report = _backfill(store, cutoff=cutoff)

    assert report.legacy_requests_inspected == 0
    assert report.heads_created == 0


def test_the_backfill_refuses_to_run_without_the_writer_lock(store):
    """Observing "no head exists" and writing one are a single decision only if
    nothing can write between them."""
    with pytest.raises(RuntimeError, match="writer lock"):
        ub.backfill_funding_heads(store, "run-1", cutoff=0)


def test_quarantine_rows_are_deterministic_and_idempotent(store):
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-2")
    cutoff = max_event_id(store)

    _backfill(store, cutoff=cutoff)
    _backfill(store, "run-2", cutoff=cutoff)

    rows = store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)
    assert len(rows) == 1


def test_a_quarantine_says_what_was_found_and_what_it_blocks(store):
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-2")

    report = _backfill(store)
    quarantine = report.blocking[0]

    assert quarantine.subsystem == "funding"
    assert quarantine.identifier == workflow_id()
    assert quarantine.blocking is True
    assert quarantine.detail["phase"] == "funding"
    assert ub.list_quarantines(store) == report.quarantines
