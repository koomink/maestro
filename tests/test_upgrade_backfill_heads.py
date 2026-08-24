"""Legacy funding/budget requests get a v1 head only where history is unambiguous.

Everywhere else the migration writes a quarantine record and stops, because the
alternative is choosing which of two requests owns a month's investment on no
evidence -- and the wrong choice moves money.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    ACCOUNT_ID,
    CURRENCY,
    EXECUTION_SLEEVE,
    claim_and_complete,
    legacy_pending_request,
    legacy_terminal_event,
    make_store,
    max_event_id,
    publish_current_request,
    workflow_id,
)

from maestro.state import upgrade_backfill as ub
from maestro.state.funding_workflow import head_key


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def _backfill(store, run_id="run-1", *, cutoff=None):
    with store.writer_lock("test"):
        return ub.backfill_funding_heads(
            store, run_id, cutoff=max_event_id(store) if cutoff is None else cutoff
        )


def _raw_head(store, workflow, *, request_id, version=1):
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


def test_funding_and_budget_pending_in_one_workflow_are_judged_together(store):
    """A head is one per workflow, not one per phase.

    Grouped by phase, each of these looks like an unambiguous single candidate
    and the first phase processed wins the v1 slot; the second then collides
    with it mid-loop. Ownership is decided over every live candidate of the
    workflow at once, so both being open means nobody can be picked.
    """
    legacy_pending_request(store, "req-fund", phase="funding")
    legacy_pending_request(store, "req-budget", phase="budget")

    report = _backfill(store)

    assert report.heads_created == 0
    assert report.heads_already_coherent == 0
    assert [q.reason for q in report.blocking] == ["ambiguous_pending_requests"]
    assert report.blocking[0].detail["phases"] == ["budget", "funding"]
    assert report.blocking[0].detail["request_ids"] == ["req-budget", "req-fund"]
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


def test_a_post_cutoff_head_over_an_unproven_request_blocks_the_migration(store):
    """Newer is not a successor.

    The head names a request published after the cutoff. That proves only that
    something wrote later, not that the pre-cutoff request's transition ever
    produced it: a legitimate succession leaves a durable
    ``funding_workflow_superseded`` marker, and a request carrying one never
    reaches candidacy at all. So no durable proof of ownership exists here,
    and preserving the newer head while silently calling the older request
    inert would drop its intent without a person deciding that.
    """
    legacy_pending_request(store, "req-old")
    cutoff = max_event_id(store)
    publish_current_request(store, "req-new")

    report = _backfill(store, cutoff=cutoff)

    assert [q.reason for q in report.blocking] == ["head_ownership_conflict"]
    assert report.blocking[0].detail["head_request_id"] == "req-new"
    # The migration never overwrites: the head it found is left exactly as it was.
    assert store.load_funding_workflow_head(workflow_id())["request_id"] == "req-new"


def test_a_cas_conflict_with_the_exact_intended_head_is_coherent(store):
    """Losing the race for the v1 slot against this exact head changes nothing.

    The head here carries byte-identical content to what the backfill writes
    (a prior attempt's work, adopted on rerun), so after the collision the
    committed state is reloaded and compared -- and matches.
    """
    legacy_pending_request(store, "req-1")
    intended = {
        "duplicate_key": head_key(workflow_id(), 1),
        "workflow_id": workflow_id(),
        "version": 1,
        "request_id": "req-1",
        "phase": "funding",
        "status": "pending",
        "scope": [None, ACCOUNT_ID, EXECUTION_SLEEVE, CURRENCY],
        "reason": "legacy_backfill_v1",
    }
    store.save_system_event("run-earlier", "funding_workflow_head", dict(intended))
    cutoff = max_event_id(store)

    report = _backfill(store, cutoff=cutoff)

    assert report.heads_created == 0
    assert report.heads_already_coherent == 1
    assert len(store.list_system_events_by_type("funding_workflow_head", limit=None)) == 1


def test_a_cas_conflict_with_a_foreign_head_is_never_counted_coherent(store, monkeypatch):
    """A colliding write whose committed content differs must not be adopted.

    The stale-snapshot bug counted any lost CAS as "already coherent" without
    looking at what actually committed. Here a foreign head lands between the
    backfill's read of "no head" and its write; the collision is classified by
    reloading and comparing, not from the exception alone.
    """
    legacy_pending_request(store, "req-1")
    real_save = type(store).save_system_events_atomic
    collisions: list[str] = []

    def racing_save(store_, run_id, events, **kwargs):
        keys = {
            item["payload"]["duplicate_key"]
            for item in events
            if "duplicate_key" in item.get("payload", {})
        }
        if keys == {head_key(workflow_id(), 1)}:
            # Another writer wins the slot in the gap between the backfill's
            # read and its write.
            collisions.append(run_id)
            _raw_head(store_, workflow_id(), request_id="req-ghost")
            raise ValueError(
                f"atomic system events conflict with an existing record with "
                f"different content: {sorted(keys)}"
            )
        return real_save(store_, run_id, events, **kwargs)

    monkeypatch.setattr(type(store), "save_system_events_atomic", racing_save)
    try:
        report = _backfill(store)
    finally:
        monkeypatch.undo()

    assert len(collisions) == 1  # the race happened
    assert [q.reason for q in report.blocking] == ["head_ownership_conflict"]
    assert report.blocking[0].detail["head_request_id"] == "req-ghost"
    assert report.heads_already_coherent == 0


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
    assert quarantine.detail["phases"] == ["funding"]
    assert ub.list_quarantines(store) == report.quarantines


def test_a_legacy_ack_written_after_the_cutoff_does_not_silently_close_its_request(store):
    """A post-cutoff row must not rewrite pre-cutoff history.

    A legacy ack above the cutoff can only exist if a writer ignored the
    barrier. Reading it as proof the request finished would absorb that breach
    silently -- and possibly strand a request whose transition already had
    broker side effects. It becomes a blocking quarantine instead: no head,
    and a person looks before anything runs.
    """
    legacy_pending_request(store, "req-1")
    cutoff = max_event_id(store)
    legacy_terminal_event(store, "req-1")

    report = _backfill(store, cutoff=cutoff)

    assert [q.reason for q in report.blocking] == ["post_cutoff_legacy_terminal"]
    assert report.blocking[0].identifier == "req-1"
    assert store.load_funding_workflow_head(workflow_id()) is None


def test_a_workflow_marker_written_after_the_cutoff_is_still_terminal_evidence(store):
    """The documented manual resolution writes its supersede marker while the
    migration sits MIGRATING -- necessarily above the cutoff. That evidence the
    design explicitly permits: only current-generation code (or the operator
    following the runbook) writes these markers, unlike legacy acks."""
    legacy_pending_request(store, "req-a")
    legacy_pending_request(store, "req-b")
    cutoff = max_event_id(store)
    from maestro.state.funding_workflow import superseded_key

    store.save_system_event(
        "run-operator",
        "funding_workflow_superseded",
        {
            "duplicate_key": superseded_key(workflow_id(), "req-b"),
            "workflow_id": workflow_id(),
            "request_id": "req-b",
            "reason": "operator_migration_decision",
        },
    )

    report = _backfill(store, cutoff=cutoff)

    assert report.blocking == []
    assert report.heads_created == 1
    assert store.load_funding_workflow_head(workflow_id())["request_id"] == "req-a"
