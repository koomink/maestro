"""Tests for authoritative workflow state projection into FundingWorkflowCardModel."""

from __future__ import annotations

from typing import Any

import pytest

from maestro.integrations.telegram.ui.funding_workflow import (
    FundingWorkflowAttention,
    FundingWorkflowCardModel,
    FundingWorkflowCardStage,
    FundingWorkflowPhase,
    FundingWorkflowRequestRef,
    funding_workflow_card_key,
    project_funding_workflow_card,
    recovery_owned_incomplete_attempts,
)
from maestro.state.funding_workflow import (
    claim_key,
    claim_workflow_attempt,
    complete_workflow,
    completed_key,
    funding_workflow_id,
    publish_contribution_request,
    superseded_key,
    workflow_id_from_request,
)
from maestro.state.store import StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    db_path = str(tmp_path / "state.db")
    return StateStore(db_path, initial_cash=1_000_000.0)


def _funding_req(
    request_id: str,
    *,
    month_key: str = "2026-08",
    card_delivery_version: int | None = 0,
) -> dict[str, Any]:
    req = {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "paper_cash",
        "execution_sleeve": "krw_contribution",
        "currency": "KRW",
        "month_key": month_key,
        "status": "pending",
        "strategy_ids": ["tranquillo"],
        "required_shortfall": 1_000_000.0,
    }
    if card_delivery_version is not None:
        req["card_delivery_version"] = card_delivery_version
    return req


def _budget_req(
    request_id: str,
    *,
    month_key: str = "2026-08",
    card_delivery_version: int | None = 0,
) -> dict[str, Any]:
    req = {
        "request_id": request_id,
        "source_signal_run_id": "signal-old",
        "contribution_group_id": "core",
        "account_id": "paper_cash",
        "execution_sleeve": "krw_contribution",
        "currency": "KRW",
        "available_cash": 2_000_000.0,
        "min_monthly_budget": 200_000.0,
        "recommended_budget": 400_000.0,
        "selectable_max_budget": 1_000_000.0,
        "month_key": month_key,
        "status": "pending",
        "strategy_ids": ["tranquillo"],
    }
    if card_delivery_version is not None:
        req["card_delivery_version"] = card_delivery_version
    return req


@pytest.mark.parametrize(
    ("phase", "intent", "completed", "expected_stage", "actions"),
    [
        ("funding", None, False, "funding_pending", True),
        ("funding", "confirm", False, "funding_confirming", False),
        ("funding", "cancel", False, "funding_canceling", False),
        ("budget", None, False, "budget_pending", True),
        ("budget", "confirm", False, "budget_applying", False),
        ("budget", "cancel", False, "budget_canceling", False),
        ("funding", "cancel", True, "funding_canceled", False),
        ("budget", "cancel", True, "budget_canceled", False),
        ("budget", "confirm", True, "budget_completed", False),
        ("funding", "confirm", True, "funding_completed", False),
    ],
)
def test_projects_truthful_monthly_stage(
    store: StateStore,
    phase: FundingWorkflowPhase,
    intent: str | None,
    completed: bool,
    expected_stage: FundingWorkflowCardStage,
    actions: bool,
):
    req = _funding_req("req-1") if phase == "funding" else _budget_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase=phase)
    wf_id = res["workflow_id"]

    selected_budget_input = 500_000.0 if (phase == "budget" and intent == "confirm") else None

    if intent is not None:
        claim_extra: dict[str, Any] = {"intent": intent}
        if selected_budget_input is not None:
            claim_extra["selected_budget"] = selected_budget_input
        claim_res = claim_workflow_attempt(
            store,
            "run-claim",
            workflow_id=wf_id,
            request_id="req-1",
            phase=phase,
            attempt=1,
            extra=claim_extra,
        )
        assert claim_res["claimed"]

        if completed:
            legacy_status = "canceled" if intent == "cancel" else ("confirmed" if phase == "funding" else "selected")
            legacy_payload: dict[str, Any] = {
                "request_id": "req-1",
                "status": legacy_status,
                "contribution_group_id": "core",
                "account_id": "paper_cash",
                "execution_sleeve": "krw_contribution",
                "currency": "KRW",
                "month_key": "2026-08",
            }
            if selected_budget_input is not None:
                legacy_payload["selected_budget"] = selected_budget_input
            comp_res = complete_workflow(
                store,
                "run-complete",
                workflow_id=wf_id,
                request_id="req-1",
                phase=phase,
                attempt=1,
                legacy_payload=legacy_payload,
            )
            assert comp_res["committed"]

    model = project_funding_workflow_card(store, wf_id)

    assert model.workflow_id == wf_id
    assert model.request_id == "req-1"
    assert model.phase == phase
    assert model.stage == expected_stage
    assert model.financial_actions_allowed == actions
    assert model.terminal_intent == intent
    assert model.month_key == "2026-08"
    assert model.scope == ("core", "paper_cash", "krw_contribution", "KRW")

    if phase == "budget" and intent == "confirm" and completed:
        assert model.selected_budget == 500_000.0
    elif intent is None:
        assert model.selected_budget is None

    if phase == "funding" and intent == "confirm" and completed:
        assert model.stage == "funding_completed"
        assert model.predecessor_request_id is None
        assert model.predecessor_completed is None


def test_funding_predecessor_incomplete_blocks_actions_on_budget_head(store: StateStore):
    # Funding A published and claimed, not completed
    req_a = _funding_req("req-a")
    publish_res_a = publish_contribution_request(store, "run-a", req_a, phase="funding")
    wf_id = publish_res_a["workflow_id"]
    claim_a = claim_workflow_attempt(
        store,
        "run-claim-a",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_a["claimed"]

    # Budget B published as legitimate successor of A
    req_b = _budget_req("req-b")
    publish_res_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    assert publish_res_b["committed"]

    # Project card for workflow (head is now B)
    model = project_funding_workflow_card(store, wf_id)
    assert model.request_id == "req-b"
    assert model.phase == "budget"
    assert model.stage == "budget_pending"
    assert model.attention == "predecessor_incomplete"
    assert model.predecessor_completed is False
    assert model.predecessor_request_id == "req-a"
    assert model.financial_actions_allowed is False
    assert model.lifecycle_stage == "attention"


def test_funding_predecessor_completed_allows_budget_head_actions(store: StateStore):
    # Funding A published and claimed
    req_a = _funding_req("req-a")
    publish_res_a = publish_contribution_request(store, "run-a", req_a, phase="funding")
    wf_id = publish_res_a["workflow_id"]
    claim_a = claim_workflow_attempt(
        store,
        "run-claim-a",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_a["claimed"]

    # Budget B published as legitimate successor of A
    req_b = _budget_req("req-b")
    publish_res_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    assert publish_res_b["committed"]

    # Now complete A
    comp_a = complete_workflow(
        store,
        "run-complete-a",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-a", "status": "confirmed"},
    )
    assert comp_a["committed"]

    # Project card for workflow (head is B)
    model = project_funding_workflow_card(store, wf_id)
    assert model.request_id == "req-b"
    assert model.phase == "budget"
    assert model.stage == "budget_pending"
    assert model.attention is None
    assert model.predecessor_completed is True
    assert model.predecessor_request_id == "req-a"
    assert model.financial_actions_allowed is True
    assert model.lifecycle_stage == "pending"


def test_in_flight_claim_without_recovery_notice_has_no_attention(store: StateStore):
    req = _funding_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]
    claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )

    model = project_funding_workflow_card(store, wf_id)
    assert model.stage == "funding_confirming"
    assert model.attention is None
    assert model.financial_actions_allowed is False
    assert model.lifecycle_stage == "in_progress"


def test_recovery_notice_for_different_attempt_or_request_does_not_affect_current_claim(
    store: StateStore,
):
    req = _funding_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]
    claim_workflow_attempt(
        store,
        "run-claim-1",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    claim_workflow_attempt(
        store,
        "run-claim-2",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
        extra={"intent": "confirm"},
    )

    # Record stalled notice for attempt 1 (not current attempt 2)
    store.save_system_events_atomic(
        "run-stalled",
        [
            {
                "event_type": "funding_workflow_stalled_notice",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-1",
                    "phase": "funding",
                    "attempt": 1,
                    "duplicate_key": f"funding-workflow-stalled:funding:req-1:a1:100",
                    "chat_id": 100,
                },
            }
        ],
    )

    # Notice for another workflow
    store.save_system_events_atomic(
        "run-stalled-other",
        [
            {
                "event_type": "funding_workflow_stalled_notice",
                "payload": {
                    "workflow_id": "funding:other:2026-08",
                    "request_id": "req-other",
                    "phase": "funding",
                    "attempt": 2,
                    "duplicate_key": f"funding-workflow-stalled:funding:req-other:a2:100",
                    "chat_id": 100,
                },
            }
        ],
    )

    model = project_funding_workflow_card(store, wf_id)
    assert model.stage == "funding_confirming"
    assert model.attention is None
    assert model.lifecycle_stage == "in_progress"


def test_lineage_follows_superseded_links_and_is_current_first(store: StateStore):
    # req-1 (funding) -> req-2 (funding) -> req-3 (budget)
    req1 = _funding_req("req-1")
    pub1 = publish_contribution_request(store, "run-1", req1, phase="funding")
    wf_id = pub1["workflow_id"]

    req2 = _funding_req("req-2")
    pub2 = publish_contribution_request(store, "run-2", req2, phase="funding")
    assert pub2["committed"]

    # Claim req-2 to legitimately publish successor req-3
    claim_workflow_attempt(
        store,
        "run-claim-2",
        workflow_id=wf_id,
        request_id="req-2",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )

    req3 = _budget_req("req-3")
    pub3 = publish_contribution_request(
        store,
        "run-3",
        req3,
        phase="budget",
        successor_of_request_id="req-2",
        successor_of_phase="funding",
    )
    assert pub3["committed"]

    model = project_funding_workflow_card(store, wf_id)
    assert model.request_id == "req-3"
    assert model.lineage == (
        FundingWorkflowRequestRef(request_id="req-3", phase="budget", lineage_distance=0),
        FundingWorkflowRequestRef(request_id="req-2", phase="funding", lineage_distance=1),
        FundingWorkflowRequestRef(request_id="req-1", phase="funding", lineage_distance=2),
    )


def test_lineage_rejects_cycle(store: StateStore):
    req1 = _funding_req("req-1")
    pub1 = publish_contribution_request(store, "run-1", req1, phase="funding")
    wf_id = pub1["workflow_id"]

    # Manually create a cycle: req-1 superseded_by req-2, req-2 superseded_by req-1
    store.save_system_events_atomic(
        "run-cycle",
        [
            {
                "event_type": "funding_workflow_superseded",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-1",
                    "superseded_by": "req-2",
                    "duplicate_key": superseded_key(wf_id, "req-1"),
                },
            },
            {
                "event_type": "funding_workflow_superseded",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-2",
                    "superseded_by": "req-1",
                    "duplicate_key": superseded_key(wf_id, "req-2"),
                },
            },
            {
                "event_type": "contribution_funding_request",
                "payload": {
                    **_funding_req("req-2"),
                    "funding_workflow_id": wf_id,
                    "duplicate_key": "contribution_funding_request:req-2",
                },
            },
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": f"head:{wf_id}:v2",
                    "workflow_id": wf_id,
                    "version": 2,
                    "request_id": "req-2",
                    "phase": "funding",
                    "status": "pending",
                    "scope": ["core", "paper_cash", "krw_contribution", "KRW"],
                },
            },
        ],
    )

    with pytest.raises(ValueError, match="cycle detected in lineage"):
        project_funding_workflow_card(store, wf_id)


def test_lineage_rejects_ambiguous_predecessors(store: StateStore):
    req = _funding_req("req-head")
    pub = publish_contribution_request(store, "run-head", req, phase="funding")
    wf_id = pub["workflow_id"]

    # Two distinct predecessors claim to be superseded by req-head
    store.save_system_events_atomic(
        "run-ambig",
        [
            {
                "event_type": "funding_workflow_superseded",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-p1",
                    "superseded_by": "req-head",
                    "duplicate_key": superseded_key(wf_id, "req-p1"),
                },
            },
            {
                "event_type": "funding_workflow_superseded",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-p2",
                    "superseded_by": "req-head",
                    "duplicate_key": superseded_key(wf_id, "req-p2"),
                },
            },
            {
                "event_type": "contribution_funding_request",
                "payload": {
                    **_funding_req("req-p1"),
                    "funding_workflow_id": wf_id,
                    "duplicate_key": "contribution_funding_request:req-p1",
                },
            },
            {
                "event_type": "contribution_funding_request",
                "payload": {
                    **_funding_req("req-p2"),
                    "funding_workflow_id": wf_id,
                    "duplicate_key": "contribution_funding_request:req-p2",
                },
            },
        ],
    )

    with pytest.raises(ValueError, match="ambiguous predecessors"):
        project_funding_workflow_card(store, wf_id)


def test_funding_completed_with_child_remains_funding_completed(store: StateStore):
    req = _funding_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]

    claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    # Child created event
    store.save_system_events_atomic(
        "run-child",
        [
            {
                "event_type": "funding_workflow_child_created",
                "payload": {
                    "duplicate_key": f"child:req-1:funding",
                    "signal_run_id": "signal-child-1",
                    "workflow_id": wf_id,
                    "request_id": "req-1",
                    "phase": "funding",
                },
            }
        ],
    )
    complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )

    model = project_funding_workflow_card(store, wf_id)
    assert model.stage == "funding_completed"
    assert model.lifecycle_stage == "done"
    assert model.selected_budget is None


def test_missing_card_delivery_version_defaults_to_zero(store: StateStore):
    req = _funding_req("req-no-version", card_delivery_version=None)
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]

    model = project_funding_workflow_card(store, wf_id)
    assert model.card_delivery_version == 0

    req_v1 = _funding_req("req-v1", card_delivery_version=1)
    res_v1 = publish_contribution_request(store, "run-2", req_v1, phase="funding")
    wf_id_v1 = res_v1["workflow_id"]

    model_v1 = project_funding_workflow_card(store, wf_id_v1)
    assert model_v1.card_delivery_version == 1


def test_projection_is_pure_read_only_and_deterministic_across_restarts(
    tmp_path,
):
    db_path = str(tmp_path / "restart.db")
    store1 = StateStore(db_path, initial_cash=1_000_000.0)

    req_a = _funding_req("req-a")
    pub_a = publish_contribution_request(store1, "run-a", req_a, phase="funding")
    wf_id = pub_a["workflow_id"]
    claim_workflow_attempt(
        store1,
        "run-claim-a",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    req_b = _budget_req("req-b")
    publish_contribution_request(
        store1,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    complete_workflow(
        store1,
        "run-comp-a",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-a", "status": "confirmed"},
    )

    events_before = len(store1.list_system_events(limit=1000))
    model1 = project_funding_workflow_card(store1, wf_id)
    events_after = len(store1.list_system_events(limit=1000))
    assert events_before == events_after

    # Reopen same SQLite file with new StateStore
    store2 = StateStore(db_path, initial_cash=1_000_000.0)
    events_before_2 = len(store2.list_system_events(limit=1000))
    model2 = project_funding_workflow_card(store2, wf_id)
    events_after_2 = len(store2.list_system_events(limit=1000))
    assert events_before_2 == events_after_2

    assert model1 == model2


def test_funding_workflow_card_key():
    assert funding_workflow_card_key("wf-123") == "funding-workflow:wf-123"


def test_recovery_owned_incomplete_attempts_helper(store: StateStore):
    req = _funding_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]

    assert recovery_owned_incomplete_attempts(store, wf_id) == frozenset()

    claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )

    # Claim exists, but no notice yet
    assert recovery_owned_incomplete_attempts(store, wf_id) == frozenset()

    # Record stalled notice
    store.save_system_events_atomic(
        "run-notice",
        [
            {
                "event_type": "funding_workflow_stalled_notice",
                "payload": {
                    "workflow_id": wf_id,
                    "request_id": "req-1",
                    "phase": "funding",
                    "attempt": 1,
                    "duplicate_key": f"funding-workflow-stalled:funding:req-1:a1:100",
                    "chat_id": 100,
                },
            }
        ],
    )

    assert recovery_owned_incomplete_attempts(store, wf_id) == frozenset(
        {("req-1", "funding", 1)}
    )

    # Complete it
    complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed"},
    )

    # Completed transition is no longer in list_incomplete_workflows, so intersection is empty
    assert recovery_owned_incomplete_attempts(store, wf_id) == frozenset()


def test_corrupt_terminal_state_without_claim_raises(store: StateStore):
    req = _funding_req("req-1")
    res = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = res["workflow_id"]

    # Directly insert a completed event without a claim
    store.save_system_events_atomic(
        "run-corrupt",
        [
            {
                "event_type": "funding_workflow_completed",
                "payload": {
                    "duplicate_key": completed_key(wf_id, "req-1", "funding"),
                    "workflow_id": wf_id,
                    "request_id": "req-1",
                    "phase": "funding",
                    "attempt": 1,
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="completed attempt 1 has no matching claim"):
        project_funding_workflow_card(store, wf_id)
