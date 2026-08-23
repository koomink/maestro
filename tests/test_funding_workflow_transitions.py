import pytest

from maestro.state.funding_workflow import (
    WorkflowClaimRefused,
    child_key,
    claim_workflow_attempt,
    complete_workflow,
    head_key,
    list_incomplete_workflows,
    load_workflow_child,
    publish_contribution_request,
)
from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _request(request_id):
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
        "status": "pending",
        "strategy_ids": ["s1"],
    }


def _published(store, request_id):
    return publish_contribution_request(
        store, "run-1", _request(request_id), phase="funding"
    )["workflow_id"]


def test_the_head_request_can_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert result["claimed"] is True
    assert result["attempt"] == 1


def test_a_second_callback_for_the_same_attempt_is_refused(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    again = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert again["claimed"] is False
    assert again["reason"] == "already_claimed"


def test_a_superseded_request_cannot_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert result["claimed"] is False
    assert result["reason"] == "not_head"


def test_a_head_that_moves_between_read_and_write_loses_the_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    # Simulate the TOCTOU: v2 lands after we read v1 as current. This is a
    # version-only race for the *same* request (e.g. a convergence-sweep
    # repair), not a supersession by a different request -- that scenario
    # is already covered by test_a_superseded_request_cannot_claim, and
    # would correctly resolve to "not_head" rather than "head_moved" since
    # the request truly stopped being head. Keeping request_id identical
    # here is what makes this test actually exercise the CAS's forbid
    # precondition instead of the request-identity fast path.
    store.save_system_events_atomic(
        "run-9",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "req-1",
                    "status": "pending",
                },
            }
        ],
    )
    result = claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        expected_version=1,
    )
    assert result["claimed"] is False
    assert result["reason"] == "head_moved"


def test_a_workflow_with_no_head_cannot_claim(tmp_path):
    store = _store(tmp_path)
    result = claim_workflow_attempt(
        store, "run-1", workflow_id="funding:x:2026-08", request_id="req-1", phase="funding"
    )
    assert result["claimed"] is False
    assert result["reason"] == "no_head"


def test_a_later_attempt_can_claim_after_the_first_one_stalled(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    resumed = claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
    )
    assert resumed["claimed"] is True
    assert resumed["attempt"] == 2


def test_two_threads_racing_the_same_attempt_yield_exactly_one_claim(tmp_path):
    import threading

    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        barrier.wait()
        results.append(
            claim_workflow_attempt(
                store,
                "run-1",
                workflow_id=workflow_id,
                request_id="req-1",
                phase="funding",
            )["claimed"]
        )

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_a_signal_run_without_a_source_records_no_lineage(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    orchestrator.run_signal(strategy_ids=["tranquillo"])
    # No source_request_id means no lineage record at all -- not "linked to
    # some arbitrary request id", but absent, for every event of this type.
    assert store.list_system_events_by_type("funding_workflow_child_created", limit=None) == []


def test_the_same_source_request_never_creates_two_children(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    first = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-1",
        source_workflow_id="wf-a",
        source_phase="funding",
    )
    second = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-1",
        source_workflow_id="wf-a",
        source_phase="funding",
    )
    assert second.signal_run_id == first.signal_run_id
    assert load_workflow_child(store, "req-1", "funding") == first.signal_run_id
    packages = store.list_system_events_by_type("signal_package", limit=None)
    assert len(packages) == 1


def test_a_different_source_request_gets_its_own_child(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    first = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-1",
        source_workflow_id="wf-a",
        source_phase="funding",
    )
    second = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-2",
        source_workflow_id="wf-a",
        source_phase="funding",
    )
    assert second.signal_run_id != first.signal_run_id


def test_a_child_pointing_at_a_missing_package_fails_loudly(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    # Lineage already names this signal_run_id but no signal_package was ever
    # written for it -- corruption, a retention purge, a race with a delete
    # path. That must surface as a loud failure, not as a phantom "nothing
    # required" summary.
    store.save_system_events_atomic(
        "run-missing-package",
        [
            {
                "event_type": "funding_workflow_child_created",
                "payload": {
                    "duplicate_key": child_key("req-1", "funding"),
                    "workflow_id": "wf-a",
                    "request_id": "req-1",
                    "phase": "funding",
                    "signal_run_id": "signal-does-not-exist",
                },
            }
        ],
    )
    with pytest.raises(ValueError, match="signal-does-not-exist"):
        orchestrator.run_signal(
            strategy_ids=["tranquillo"],
            source_request_id="req-1",
            source_workflow_id="wf-a",
            source_phase="funding",
        )


def test_completing_a_funding_workflow_also_writes_the_legacy_ack(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    result = complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )
    assert result["committed"] is True
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert [row["payload"]["request_id"] for row in acks] == ["req-1"]


def test_the_completed_event_and_the_legacy_ack_land_together(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert len(completed) == len(acks) == 1


def test_a_budget_workflow_dual_writes_the_decision_event(tmp_path):
    store = _store(tmp_path)
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="budget"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="budget"
    )
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="budget",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "selected", "selected_budget": 500000.0},
    )
    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert decisions[0]["payload"]["selected_budget"] == 500000.0


def test_completing_twice_is_an_idempotent_replay(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    payload = {"request_id": "req-1", "status": "confirmed", "decided_by": "op"}
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload=payload,
    )
    again = complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload=payload,
    )
    assert again["committed"] is False
    assert again["conflict"] == "already_committed"
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert len(acks) == 1


def test_a_late_attempt_cannot_complete_a_transition_a_newer_one_took_over(tmp_path):
    """Important 1: attempt is only a fencing token if completion checks it.

    Attempt 1 stalls inside run_signal on a process that merely looks dead,
    the operator resumes as attempt 2, and only then does attempt 1 come back
    to life. Its completion must be refused -- committing it would close the
    workflow out from under the attempt that now owns it, and drop it from
    the recovery list while attempt 2 is still mid-flight.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=2
    )

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        complete_workflow(
            store,
            "run-1",
            workflow_id=workflow_id,
            request_id="req-1",
            phase="funding",
            attempt=1,
            legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
        )

    assert excinfo.value.reason == "attempt_superseded"
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert store.list_system_events_by_type("contribution_funding_request_ack", limit=None) == []

def test_the_attempt_that_owns_the_transition_still_completes(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=2
    )

    result = complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )

    assert result["committed"] is True

def test_completing_an_attempt_that_never_claimed_is_refused(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        complete_workflow(
            store,
            "run-1",
            workflow_id=workflow_id,
            request_id="req-1",
            phase="funding",
            attempt=1,
            legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
        )

    assert excinfo.value.reason == "unclaimed_attempt"

def test_a_head_whose_phase_contradicts_the_transition_refuses_the_claim(tmp_path):
    """Important 3: request id and version alone are not enough.

    A head that names this request but a different phase can only be a
    corrupted or mis-backfilled record. Claiming on it would let a budget
    decision drive a funding confirmation.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    store.save_system_events_atomic(
        "run-9",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "req-1",
                    "phase": "budget",
                    "status": "pending",
                },
            }
        ],
    )

    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )

    assert result["claimed"] is False
    assert result["reason"] == "head_corrupt"
    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []

def test_a_head_whose_scope_contradicts_its_own_key_refuses_the_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    store.save_system_events_atomic(
        "run-9",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "req-1",
                    "phase": "funding",
                    "status": "pending",
                    "scope": ["core", "some-other-account", "krw", "KRW"],
                },
            }
        ],
    )

    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )

    assert result["claimed"] is False
    assert result["reason"] == "head_corrupt"

def test_a_head_written_before_scope_and_phase_existed_still_claims(tmp_path):
    """Absent is not the same as contradicting: heads from the release before
    these fields existed must keep working, or every workflow in flight at
    upgrade time becomes unactionable."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    store.save_system_events_atomic(
        "run-9",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "req-1",
                    "status": "pending",
                },
            }
        ],
    )

    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )

    assert result["claimed"] is True

def test_a_completed_workflow_cannot_be_claimed_by_a_new_attempt(tmp_path):
    """Re-review Important 1: head does not move when a workflow finishes.

    head stays where it is until the next request replaces it, so checking
    only "has head moved" says nothing about whether this transition is
    already over. Without forbidding the completion marker, attempt 2 walks
    straight in and re-runs the cash flow and the child run before
    complete_workflow finally refuses it at the very end.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )

    resumed = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=2
    )

    assert resumed["claimed"] is False
    assert resumed["reason"] == "already_completed"

def test_the_completed_guard_does_not_block_the_request_that_replaced_it(tmp_path):
    """The completion marker names one request, not the workflow: finishing
    req-1 must not lock req-2 out of the month it just took over."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    assert (
        claim_workflow_attempt(
            store, "run-1", workflow_id=workflow_id, request_id="req-2", phase="funding"
        )["claimed"]
        is True
    )


def _workflow_id(store) -> str:
    return store.list_funding_workflow_heads()[0]["workflow_id"]

def test_retrying_a_replacement_request_is_refused_not_a_conflict(tmp_path):
    """Important 2: a redelivered replacement must not raise.

    req-2 replaced req-1, so the batch that landed carried the supersede
    marker too -- and, in a signal run, the package as well. A retry can
    never reproduce that batch, so trying to replay it would fail as a
    provenance mismatch. The request already exists, which is what the
    caller wanted, so the retry is refused and nothing moves.
    """
    store = _store(tmp_path)
    _published(store, "req-1")
    first = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    assert first["committed"] is True

    retry = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    assert retry["committed"] is False
    assert retry["conflict"] == "already_published"
    assert retry["version"] == first["version"]
    superseded = store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    assert [row["payload"]["request_id"] for row in superseded] == ["req-1"]

def test_retrying_a_replacement_request_still_refuses_a_second_replacement(tmp_path):
    """The rebuilt supersede event must not make a *different* later request
    look like a replay of the one at head."""
    store = _store(tmp_path)
    _published(store, "req-1")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    third = publish_contribution_request(store, "run-3", _request("req-3"), phase="funding")

    assert third["committed"] is True
    assert third["version"] == 3
    assert store.load_funding_workflow_head(_workflow_id(store))["request_id"] == "req-3"

def test_a_child_runs_package_and_lineage_are_committed_as_one_batch(funding_orchestrator):
    """Critical 1: there is no longer a moment between the two writes.

    The lineage record is the only thing that tells a resumed attempt this
    request already has a child. Written after the package, a crash in
    between left a package nothing pointed at, and the resume built a second
    one for the same month. A shared, non-null batch_fingerprint is the
    store's own proof that a single transaction wrote both rows.
    """
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    summary = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-1",
        source_workflow_id="wf-a",
        source_phase="funding",
    )

    packages = store.list_system_events_by_type("signal_package", limit=None)
    lineage = store.list_system_events_by_type("funding_workflow_child_created", limit=None)
    assert len(packages) == len(lineage) == 1
    assert packages[0]["payload"]["signal_run_id"] == summary.signal_run_id
    assert packages[0]["batch_fingerprint"]
    assert packages[0]["batch_fingerprint"] == lineage[0]["batch_fingerprint"]

def test_a_package_committed_in_a_batch_can_still_be_amended(funding_orchestrator):
    """A package is not a one-shot record: a correction appends a row that
    load_signal_package then prefers. The batched write keys its row, so the
    amendment has to drop that inherited key -- otherwise the second write
    collides with the first under the unique index instead of superseding
    it, and the correction is lost as an IntegrityError."""
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    summary = orchestrator.run_signal(
        strategy_ids=["tranquillo"],
        source_request_id="req-1",
        source_workflow_id="wf-a",
        source_phase="funding",
    )

    package = store.load_signal_package(summary.signal_run_id)
    store.save_signal_package(summary.signal_run_id, {**package, "orders_preview_count": 99})

    assert store.load_signal_package(summary.signal_run_id)["orders_preview_count"] == 99


def test_a_late_redelivery_of_a_replaced_request_is_refused_not_raised(tmp_path):
    """Re-review Important 2: req1 -> req2 -> req3, then req2 arrives again.

    The immediate retry (req2 still at head) was already a clean replay. This
    one is not: req2's own event exists but head has moved on twice, so
    rebuilding it as a fresh transition submits one key that exists among
    three that do not -- a partial overlap, which raises. That exception
    escapes publish and takes down the entire signal run that carried it,
    including every unrelated request in the same run.
    """
    store = _store(tmp_path)
    _published(store, "req-1")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    publish_contribution_request(store, "run-3", _request("req-3"), phase="funding")

    late = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    assert late["committed"] is False
    assert late["conflict"] == "already_published"
    # Nothing moved: req-3 is still the active request for this month.
    head = store.load_funding_workflow_head(late["workflow_id"])
    assert head["request_id"] == "req-3"
    assert head["version"] == 3

def test_a_late_redelivery_does_not_stop_the_rest_of_its_signal_run(funding_orchestrator):
    """The whole point of refusing rather than raising: one stale request must
    not cost the run every other request in it."""
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    first = orchestrator.run_signal(strategy_ids=["tranquillo"])
    published = store.list_system_events_by_type("contribution_funding_request", limit=None)
    assert published, "fixture must produce a funding request to redeliver"

    # The same request object arrives again through publish, after its own
    # signal run already recorded it.
    outcome = publish_contribution_request(
        store, first.signal_run_id, dict(published[0]["payload"]), phase="funding"
    )

    assert outcome["committed"] is False
    assert outcome["conflict"] in {"already_committed", "already_published"}


def test_an_attempt_that_skips_a_number_is_refused(tmp_path):
    """Re-review Important: attempt is a fencing token only if claiming it
    requires the previous attempt to have claimed first.

    Without this, attempt 1 claims, attempt 3 claims (skipping 2 entirely),
    and then attempt 1's completion only checks that attempt 2's claim does
    not exist -- which it never did -- so attempt 1 could close a transition
    attempt 3 already owns. Refusing attempt 3's claim here is what keeps
    that from ever being reachable.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    first = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=1
    )
    assert first["claimed"] is True

    skipped = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=3
    )
    assert skipped["claimed"] is False
    assert skipped["reason"] == "attempt_out_of_order"

    # The transition is still attempt 1's alone: nobody took it over, so its
    # completion must still be free to land.
    outcome = complete_workflow(
        store,
        "run-2",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )
    assert outcome["committed"] is True

def test_a_non_positive_attempt_is_rejected(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    with pytest.raises(ValueError, match="positive"):
        claim_workflow_attempt(
            store,
            "run-1",
            workflow_id=workflow_id,
            request_id="req-1",
            phase="funding",
            attempt=0,
        )


def test_an_independent_publish_cannot_supersede_a_claimed_head(tmp_path):
    """Priority 1: two unrelated requests must never both become executable.

    req-1 claims (an operator confirms it, say) and its transition is still
    running -- cash flow recording, a child signal run, completion are all
    still ahead of it. An unrelated req-2 (a fresh scheduled signal run, a
    manual re-publish) must not be allowed to supersede req-1's head out from
    under it: complete_workflow never re-checks the head, so req-1 would go
    on to complete legitimately while req-2 also becomes claimable and runs
    its own cash flow and child signal -- one workflow's single live decision
    executing twice.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert claim["claimed"] is True

    result = publish_contribution_request(
        store, "run-2", _request("req-2"), phase="funding"
    )

    assert result["committed"] is False
    assert result["conflict"] == "head_claimed"
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-1"

    # req-1's own transition is unaffected: it is still the sole live
    # decision, and it can complete normally.
    outcome = complete_workflow(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )
    assert outcome["committed"] is True


def test_a_legitimate_child_successor_supersedes_a_claimed_head(tmp_path):
    """The child-run counterpart: a follow-up request the claimed transition
    itself generates (e.g. a budget request the confirm triggers) must still
    be able to supersede the head it is claimed on -- that is the normal,
    intended shape of a multi-step workflow, not the race this priority
    guards against."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert claim["claimed"] is True

    result = publish_contribution_request(
        store,
        "run-2",
        _request("req-2"),
        phase="funding",
        successor_of_request_id="req-1",
        successor_of_phase="funding",
    )

    assert result["committed"] is True
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-2"

    # req-1's own transition still completes on its own attempt fencing --
    # the legitimate successor does not disturb it.
    outcome = complete_workflow(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )
    assert outcome["committed"] is True


def test_a_successor_declaration_naming_the_wrong_request_is_refused(tmp_path):
    """A successor declaration is verified, not merely trusted: it must name
    the request that is actually claimed and open, or it is exactly the
    independent-publish race with an unverified label pasted on."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )

    result = publish_contribution_request(
        store,
        "run-2",
        _request("req-2"),
        phase="funding",
        successor_of_request_id="some-other-request",
        successor_of_phase="funding",
    )

    assert result["committed"] is False
    assert result["conflict"] == "head_claimed"


def test_a_publish_is_unblocked_once_the_claimed_transition_completes(tmp_path):
    """Once req-1 completes with no successor, the head still names it (head
    never moves on completion) but the transition is no longer open -- a
    routine, unrelated publish (next cycle's cron, say) must proceed exactly
    as it always has."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    complete_workflow(
        store,
        "run-2",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )

    result = publish_contribution_request(
        store, "run-3", _request("req-2"), phase="funding"
    )

    assert result["committed"] is True
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-2"


def test_a_publish_proceeds_normally_when_the_head_was_never_claimed(tmp_path):
    """The overwhelmingly common case -- nobody has acted on the current head
    yet -- must not regress: a fresh publish still supersedes it exactly as
    before."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")

    result = publish_contribution_request(
        store, "run-2", _request("req-2"), phase="funding"
    )

    assert result["committed"] is True
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-2"


def test_stale_attempt_fencing_remains_intact_around_the_head_claimed_guard(tmp_path):
    """Priority 1's guard must not weaken priority 1's own prerequisite: a
    skipped attempt number is still refused, whether or not an independent
    publish was also attempted against the same claimed head."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding", attempt=1
    )
    blocked = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    assert blocked["committed"] is False

    skipped = claim_workflow_attempt(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=3,
    )

    assert skipped["claimed"] is False
    assert skipped["reason"] == "attempt_out_of_order"


def test_a_claimed_parent_survived_by_its_successor_stays_recoverable(tmp_path):
    """2026-08 re-review, priority 1: complete_workflow never re-checks head,
    so a crash between the legitimate successor's publish and completing the
    parent must not strand the parent. list_incomplete_workflows has to
    surface req-1 even though req-2 -- its own declared successor -- is now
    head, and claim_workflow_attempt has to let a resumed attempt on req-1
    land despite req-1 no longer being head.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    publish_contribution_request(
        store,
        "run-2",
        _request("req-2"),
        phase="funding",
        successor_of_request_id="req-1",
        successor_of_phase="funding",
    )
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-2"

    incomplete = list_incomplete_workflows(store)
    assert [row["request_id"] for row in incomplete] == ["req-1"]

    resumed = claim_workflow_attempt(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
    )
    assert resumed["claimed"] is True

    outcome = complete_workflow(
        store,
        "run-4",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )
    assert outcome["committed"] is True
    # req-1 is done; req-2 -- unclaimed, untouched -- is still head.
    assert list_incomplete_workflows(store) == []
    assert store.load_funding_workflow_head(workflow_id)["request_id"] == "req-2"


def test_a_request_with_no_legitimate_successor_marker_stays_not_head(tmp_path):
    """The recoverability granted above is narrow: a request that is simply
    not head -- with no durable proof its own claimed transition produced
    whatever replaced it -- must still be refused as not_head. Otherwise any
    claimed-and-superseded request would look recoverable, which is exactly
    the double-execution risk priority 1 (independent publish) exists to
    close.
    """
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    complete_workflow(
        store,
        "run-2",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )
    # req-1 is completed and unclaimed at any later attempt; req-2 replaces
    # it as an entirely ordinary, non-successor publish.
    publish_contribution_request(store, "run-3", _request("req-2"), phase="funding")

    stale = claim_workflow_attempt(
        store,
        "run-4",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
    )

    assert stale["claimed"] is False
    assert stale["reason"] == "not_head"


def test_stale_attempt_fencing_still_holds_after_a_successor_recovery(tmp_path):
    """A skipped attempt number must still be refused when the request being
    resumed is reached through the legitimate-successor path, not just when
    it is still head."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    publish_contribution_request(
        store,
        "run-2",
        _request("req-2"),
        phase="funding",
        successor_of_request_id="req-1",
        successor_of_phase="funding",
    )

    skipped = claim_workflow_attempt(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=3,
    )

    assert skipped["claimed"] is False
    assert skipped["reason"] == "attempt_out_of_order"


def test_a_stale_attempt_cannot_complete_after_a_successor_recovery_takes_over(tmp_path):
    """The other half of fencing: once a resumed attempt claims req-1 through
    the successor path, an older attempt must still be refused at
    completion, exactly as if req-1 had never lost the head at all."""
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    publish_contribution_request(
        store,
        "run-2",
        _request("req-2"),
        phase="funding",
        successor_of_request_id="req-1",
        successor_of_phase="funding",
    )
    resumed = claim_workflow_attempt(
        store,
        "run-3",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
    )
    assert resumed["claimed"] is True

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        complete_workflow(
            store,
            "run-4",
            workflow_id=workflow_id,
            request_id="req-1",
            phase="funding",
            attempt=1,
            legacy_payload={"request_id": "req-1", "status": "confirmed"},
        )
    assert excinfo.value.reason == "attempt_superseded"
