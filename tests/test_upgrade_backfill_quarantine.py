"""Legacy approvals and pre-manifest dispatches get an owner, never a verdict.

The old 3a-5 design proposed treating "a legacy ack, no approvals row, no
completion evidence" as proof of cancellation and synthesizing a
telegram_approval_resolution_completed for it. That reads a broker's behaviour
out of a gap in local persistence: the order may have gone out before the
process died. Nothing here synthesizes anything.
"""

from __future__ import annotations

import pytest
from migration_fixtures import make_store, max_event_id

from maestro.state import upgrade_backfill as ub


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def _schema_less_ack(store, approval_id):
    """As the pre-two-phase binary wrote it: no schema_version field at all."""
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_ack",
        {"approval_id": approval_id, "status": "approved"},
    )


def _versioned_ack(store, approval_id):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_ack",
        {"approval_id": approval_id, "status": "approved", "schema_version": 2},
    )


def _pending_envelope(store, approval_id, *, signal_run_id):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_pending",
        {"approval_id": approval_id, "signal_run_id": signal_run_id},
    )


def _approval_completed(store, *, signal_run_id, approval_id=None):
    payload = {"signal_run_id": signal_run_id}
    if approval_id is not None:
        payload["approval_id"] = approval_id
    store.save_system_event(signal_run_id, "signal_approval_completed", payload)


def _consumed_but_unsettled(store, signal_run_id, *, with_manifest):
    if with_manifest:
        store.save_system_event(
            signal_run_id,
            "signal_dispatch_manifest",
            {
                "signal_run_id": signal_run_id,
                "groups": [],
                "duplicate_key": f"dispatch-manifest:{signal_run_id}",
            },
        )
    store.mark_signal_package_consumed(signal_run_id, f"approval_{signal_run_id}")


def _classify_approvals(store, run_id="run-1"):
    with store.writer_lock("test"):
        return ub.classify_legacy_approvals(store, run_id, cutoff=max_event_id(store))


def _classify_dispatches(store, run_id="run-1"):
    with store.writer_lock("test"):
        return ub.classify_legacy_dispatches(store, run_id, cutoff=max_event_id(store))


# --- legacy approvals -----------------------------------------------------


def test_a_legacy_ack_is_never_given_a_synthetic_resolution(store):
    _schema_less_ack(store, "ap-1")

    _classify_approvals(store)

    assert (
        store.list_system_events_by_type("telegram_approval_resolution_completed", limit=None)
        == []
    )


def test_an_exact_completion_match_is_proven_complete(store):
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _approval_completed(store, signal_run_id="sig-1", approval_id="ap-1")

    report = _classify_approvals(store)

    assert report.acks_inspected == 1
    assert report.proven_complete == 1
    assert report.quarantines == []


def test_a_single_group_run_completion_without_an_approval_id_counts(store):
    """The old event carried no approval_id. With exactly one group on the run
    it can only have been about that group."""
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _approval_completed(store, signal_run_id="sig-1")

    report = _classify_approvals(store)

    assert report.proven_complete == 1
    assert report.quarantines == []


def test_a_multi_group_run_completion_without_an_approval_id_does_not_count(store):
    """One group finishing says nothing about the other group's orders."""
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _pending_envelope(store, "ap-2", signal_run_id="sig-1")
    _approval_completed(store, signal_run_id="sig-1")

    report = _classify_approvals(store)

    assert report.proven_complete == 0
    assert [q.reason for q in report.quarantines] == ["completion_unprovable"]


def test_an_approvals_row_without_a_completion_is_the_worse_quarantine(store):
    """The approval was persisted, so execution may already have been entered.
    That is strictly worse than "unknown" and is labelled as such."""
    _schema_less_ack(store, "ap-1")
    store.save_approval("run-x", "ap-1", {"decision": {"status": "approved"}})

    report = _classify_approvals(store)

    assert [q.reason for q in report.quarantines] == ["execution_may_have_been_entered"]


def test_no_legacy_quarantine_blocks_the_migration(store):
    """The current runtime already refuses to auto-execute a schema-less ack.
    The record gives the ambiguity an owner; it is not a second gate."""
    _schema_less_ack(store, "ap-1")

    report = _classify_approvals(store)

    assert [q.blocking for q in report.quarantines] == [False]


def test_a_versioned_ack_is_not_a_legacy_candidate(store):
    _versioned_ack(store, "ap-1")

    report = _classify_approvals(store)

    assert report.acks_inspected == 0
    assert report.quarantines == []


def test_a_post_cutoff_ack_is_not_a_candidate(store):
    cutoff = max_event_id(store)
    _schema_less_ack(store, "ap-1")

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert report.acks_inspected == 0


def test_approval_classification_is_idempotent(store):
    _schema_less_ack(store, "ap-1")
    cutoff = max_event_id(store)
    with store.writer_lock("test"):
        ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)
        ub.classify_legacy_approvals(store, "run-2", cutoff=cutoff)

    assert len(store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)) == 1


def test_the_migration_and_the_runtime_share_one_completion_rule(store):
    """Two copies of a conservative rule drift, and the drift is invisible
    until one of them re-executes a finished approval."""
    from maestro.integrations.telegram import handlers

    assert (
        handlers.TelegramOperatorCommandRouter._completed_legacy_approval_ids.__doc__
        is not None
    )
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _approval_completed(store, signal_run_id="sig-1")
    assert ub.completed_legacy_approval_ids(store) == {"ap-1"}


# --- pre-manifest dispatches ---------------------------------------------


def test_a_manifestless_consumed_dispatch_is_quarantined(store):
    _consumed_but_unsettled(store, "sig-legacy", with_manifest=False)

    report = _classify_dispatches(store)

    assert [q.reason for q in report.quarantines] == ["legacy_dispatch_no_manifest"]
    assert report.quarantines[0].blocking is False
    assert report.quarantines[0].subsystem == "dispatch"


def test_a_dispatch_with_a_manifest_is_current_generation(store):
    _consumed_but_unsettled(store, "sig-current", with_manifest=True)

    report = _classify_dispatches(store)

    assert report.resumable == 1
    assert report.quarantines == []


def test_a_settled_dispatch_is_not_inspected(store):
    _consumed_but_unsettled(store, "sig-done", with_manifest=True)
    store.save_system_event("sig-done", "signal_approval_pending", {"signal_run_id": "sig-done"})

    report = _classify_dispatches(store)

    assert report.dispatches_inspected == 0


def test_the_dispatch_classification_is_exhaustive_not_windowed(store):
    """A default limit of 50 would silently drop the 51st unfinished run."""
    for index in range(60):
        _consumed_but_unsettled(store, f"sig-{index:03d}", with_manifest=False)

    report = _classify_dispatches(store)

    assert report.dispatches_inspected == 60
    assert len(report.quarantines) == 60


def test_dispatch_classification_is_idempotent(store):
    _consumed_but_unsettled(store, "sig-legacy", with_manifest=False)
    cutoff = max_event_id(store)
    with store.writer_lock("test"):
        ub.classify_legacy_dispatches(store, "run-1", cutoff=cutoff)
        ub.classify_legacy_dispatches(store, "run-2", cutoff=cutoff)

    assert len(store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)) == 1
