import pytest

from maestro.state.funding_workflow import (
    WorkflowClaimRefused,
    child_key,
    claim_workflow_attempt,
    complete_workflow,
    head_key,
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
