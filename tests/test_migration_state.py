"""NOT_STARTED / MIGRATING / COMPLETED / INVALID, and one immutable cutoff."""

from __future__ import annotations

import pytest
from migration_fixtures import legacy_pending_request, make_store, max_event_id

from maestro.state import migration_state as ms
from maestro.state.funding_workflow import load_migration_cutoff


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def test_a_fresh_database_has_not_started(store):
    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.NOT_STARTED
    assert state.cutoff is None


def test_starting_pins_the_cutoff_to_the_current_max_event_id(store):
    legacy_pending_request(store, "req-1")
    expected = max_event_id(store)

    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")

    assert state.phase is ms.MigrationPhase.MIGRATING
    assert state.cutoff == expected


def test_a_second_start_reuses_the_exact_same_cutoff(store):
    """A crash-and-retry sees a bigger table. Re-deriving the boundary from it
    would move rows the first attempt classified as legacy into the current
    generation, and vice versa."""
    with store.writer_lock("test"):
        first = ms.start_migration(store, "run-1")
    for index in range(10):
        legacy_pending_request(store, f"req-{index}", month_key=f"2026-{index + 1:02d}")

    with store.writer_lock("test"):
        second = ms.start_migration(store, "run-2")

    assert second.cutoff == first.cutoff
    assert len(store.list_system_events_by_type(ms.STARTED_EVENT, limit=None)) == 1


def test_completion_records_the_same_cutoff_and_reports_completed(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
        ms.complete_migration(store, "run-1", cutoff=state.cutoff)

    final = ms.load_migration_state(store)
    assert final.phase is ms.MigrationPhase.COMPLETED
    assert final.cutoff == state.cutoff


def test_completing_twice_is_a_no_op(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
        ms.complete_migration(store, "run-1", cutoff=state.cutoff)
        ms.complete_migration(store, "run-2", cutoff=state.cutoff)

    assert len(store.list_system_events_by_type(ms.COMPLETED_EVENT, limit=None)) == 1


def test_completing_with_a_different_cutoff_is_refused(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
        with pytest.raises(ms.MigrationStateInvalid):
            ms.complete_migration(store, "run-1", cutoff=state.cutoff + 1)

    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_completing_without_a_start_is_refused(store):
    with store.writer_lock("test"), pytest.raises(ms.MigrationStateInvalid):
        ms.complete_migration(store, "run-1", cutoff=0)


def test_both_writes_require_the_writer_lock(store):
    with pytest.raises(RuntimeError, match="writer lock"):
        ms.start_migration(store, "run-1")
    with pytest.raises(RuntimeError, match="writer lock"):
        ms.complete_migration(store, "run-1", cutoff=0)


def test_a_completed_marker_with_no_start_is_invalid(store):
    store.save_system_event(
        "r", ms.COMPLETED_EVENT, {"cutoff": 7, "duplicate_key": ms.COMPLETED_KEY}
    )
    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.INVALID
    assert state.reason == "completed_without_started"


@pytest.mark.parametrize("bad", ["seven", None, -1, True, 3.5])
def test_a_malformed_cutoff_is_invalid_not_zero(store, bad):
    """Zero is a meaningful boundary -- it says every row is post-migration --
    so coercing garbage into it would hand the whole pre-3a history to the
    convergence sweep."""
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": bad, "duplicate_key": ms.STARTED_KEY})
    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.INVALID
    assert state.reason == "malformed_started_marker"


def test_conflicting_start_cutoffs_are_invalid(store):
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": 7, "duplicate_key": ms.STARTED_KEY})
    store.save_system_event(
        "r", ms.STARTED_EVENT, {"cutoff": 9, "duplicate_key": f"{ms.STARTED_KEY}:stray"}
    )
    assert ms.load_migration_state(store).reason == "conflicting_start_cutoffs"


def test_conflicting_start_and_completed_cutoffs_are_invalid(store):
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": 7, "duplicate_key": ms.STARTED_KEY})
    store.save_system_event(
        "r", ms.COMPLETED_EVENT, {"cutoff": 9, "duplicate_key": ms.COMPLETED_KEY}
    )
    assert ms.load_migration_state(store).reason == "cutoff_mismatch"


def test_load_migration_cutoff_still_returns_none_before_any_migration(store):
    assert load_migration_cutoff(store) is None


def test_load_migration_cutoff_reports_the_pinned_boundary(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
    assert load_migration_cutoff(store) == state.cutoff


def test_load_migration_cutoff_raises_on_invalid_state(store):
    """It feeds the convergence sweep. A contradictory database must stop the
    sweep loudly, not hand it a guessed boundary or a silent None -- None means
    'pre-3a database, nothing to converge', which is a different fact."""
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY})
    with pytest.raises(ms.MigrationStateInvalid):
        load_migration_cutoff(store)
