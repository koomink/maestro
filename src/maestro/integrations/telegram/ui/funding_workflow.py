"""Authoritative workflow state projection into an ephemeral card read model.

Pure projection over durable events. Never persisted; equal inputs produce
equal models deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from maestro.integrations.telegram.ui.card_state import CardStage
from maestro.state.funding_workflow import (
    completed_key,
    list_incomplete_workflows,
    load_request_payload,
    scope_prefix,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

FundingWorkflowPhase = Literal["funding", "budget"]
FundingWorkflowCardStage = Literal[
    "funding_pending",
    "funding_confirming",
    "funding_canceling",
    "budget_pending",
    "budget_applying",
    "budget_canceling",
    "funding_canceled",
    "budget_canceled",
    "budget_completed",
    "funding_completed",
]
FundingWorkflowAttention = Literal[
    "incomplete_transition",
    "predecessor_incomplete",
]


@dataclass(frozen=True)
class FundingWorkflowRequestRef:
    request_id: str
    phase: FundingWorkflowPhase
    lineage_distance: int  # current request is 0; direct predecessor is 1


@dataclass(frozen=True)
class FundingWorkflowCardModel:
    workflow_id: str
    month_key: str
    scope: tuple[object, object, object, object]
    phase: FundingWorkflowPhase
    request_id: str
    request: Mapping[str, Any]
    stage: FundingWorkflowCardStage
    attention: FundingWorkflowAttention | None
    financial_actions_allowed: bool
    terminal_intent: Literal["confirm", "cancel"] | None
    selected_budget: float | None
    predecessor_request_id: str | None
    predecessor_completed: bool | None
    card_delivery_version: int
    lineage: tuple[FundingWorkflowRequestRef, ...]

    @property
    def lifecycle_stage(self) -> CardStage:
        if self.attention is not None:
            return "attention"
        if self.stage in (
            "funding_canceled",
            "budget_canceled",
            "budget_completed",
            "funding_completed",
        ):
            return "done"
        if self.stage in (
            "funding_confirming",
            "funding_canceling",
            "budget_applying",
            "budget_canceling",
        ):
            return "in_progress"
        return "pending"


def funding_workflow_card_key(workflow_id: str) -> str:
    return f"funding-workflow:{workflow_id}"


def recovery_owned_incomplete_attempts(
    store: StateStore,
    workflow_id: str,
) -> frozenset[tuple[str, FundingWorkflowPhase, int]]:
    """Return exact (request_id, phase, attempt) identities already surfaced by recovery."""
    incomplete_rows = list_incomplete_workflows(store)
    candidates = {
        (
            str(row["request_id"]),
            str(row["phase"]),
            int(row["attempt"]),
        )
        for row in incomplete_rows
        if str(row.get("workflow_id") or "") == workflow_id
    }
    if not candidates:
        return frozenset()

    surfaced: set[tuple[str, str, int]] = set()
    for event_type in ("funding_workflow_stalled_notice", "funding_workflow_needs_attention"):
        for row in store.list_system_events_by_type(event_type, limit=None):
            payload = row.get("payload") or {}
            if str(payload.get("workflow_id") or "") != workflow_id:
                continue
            req_id = payload.get("request_id")
            phase = payload.get("phase")
            attempt = payload.get("attempt")
            if req_id is not None and phase is not None and attempt is not None:
                surfaced.add((str(req_id), str(phase), int(attempt)))

    matched = candidates & surfaced
    return frozenset(
        (req_id, phase if phase == "budget" else "funding", attempt)
        for req_id, phase, attempt in matched
    )


def _request_phase(store: StateStore, request_id: str) -> FundingWorkflowPhase:
    funding_payload = load_request_payload(store, request_id, "funding")
    budget_payload = load_request_payload(store, request_id, "budget")
    if funding_payload is not None and budget_payload is not None:
        raise ValueError(f"request {request_id} exists in both funding and budget phases")
    if funding_payload is not None:
        return "funding"
    if budget_payload is not None:
        return "budget"
    raise ValueError(f"request {request_id} not found in funding or budget requests")


def _lineage(
    store: StateStore,
    workflow_id: str,
    current_request_id: str,
) -> tuple[FundingWorkflowRequestRef, ...]:
    curr_phase = _request_phase(store, current_request_id)
    lineage: list[FundingWorkflowRequestRef] = [
        FundingWorkflowRequestRef(
            request_id=current_request_id,
            phase=curr_phase,
            lineage_distance=0,
        )
    ]
    seen: set[str] = {current_request_id}
    curr = current_request_id
    distance = 1

    while True:
        predecessors: set[str] = set()
        for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
            payload = row.get("payload") or {}
            if (
                str(payload.get("workflow_id") or "") == workflow_id
                and str(payload.get("superseded_by") or "") == curr
            ):
                pred_id = str(payload.get("request_id") or "")
                if pred_id:
                    predecessors.add(pred_id)

        if not predecessors:
            break
        if len(predecessors) > 1:
            raise ValueError(
                f"ambiguous predecessors for {curr} in workflow {workflow_id}: {predecessors}"
            )

        pred = next(iter(predecessors))
        if pred in seen:
            raise ValueError(
                f"cycle detected in lineage for workflow {workflow_id} at request {pred}"
            )
        seen.add(pred)
        pred_phase = _request_phase(store, pred)
        lineage.append(
            FundingWorkflowRequestRef(
                request_id=pred,
                phase=pred_phase,
                lineage_distance=distance,
            )
        )
        curr = pred
        distance += 1

    return tuple(lineage)


def _required_funding_predecessor(
    store: StateStore,
    workflow_id: str,
    request_id: str,
    phase: str,
) -> tuple[str | None, bool | None]:
    if phase != "budget":
        return None, None

    predecessors: set[str] = set()
    for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("workflow_id") or "") == workflow_id
            and str(payload.get("superseded_by") or "") == request_id
            and payload.get("legitimate_successor") is True
            and str(payload.get("successor_of_phase") or "") == "funding"
        ):
            pred_id = str(payload.get("request_id") or "")
            if pred_id:
                predecessors.add(pred_id)

    if not predecessors:
        return None, None
    if len(predecessors) > 1:
        raise ValueError(
            f"ambiguous legitimate funding predecessor for {request_id}: {predecessors}"
        )

    pred_id = next(iter(predecessors))
    is_completed = store.duplicate_key_exists(completed_key(workflow_id, pred_id, "funding"))
    return pred_id, is_completed


def _claims_and_completions(
    store: StateStore,
    workflow_id: str,
    request_id: str,
    phase: str,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    claims: dict[int, dict[str, Any]] = {}
    for row in store.list_system_events_by_type("funding_workflow_claim", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("workflow_id") or "") == workflow_id
            and str(payload.get("request_id") or "") == request_id
            and str(payload.get("phase") or "") == phase
        ):
            attempt = int(payload.get("attempt") or 0)
            claims[attempt] = payload

    completions: dict[int, dict[str, Any]] = {}
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("workflow_id") or "") == workflow_id
            and str(payload.get("request_id") or "") == request_id
            and str(payload.get("phase") or "") == phase
        ):
            attempt = int(payload.get("attempt") or 0)
            if attempt not in claims:
                raise ValueError(
                    f"completed attempt {attempt} has no matching claim for request {request_id}"
                )
            completions[attempt] = payload

    return claims, completions


def project_funding_workflow_card(
    store: StateStore,
    workflow_id: str,
) -> FundingWorkflowCardModel:
    """Project authoritative durable events for a workflow into an ephemeral card model."""
    head = store.load_funding_workflow_head(workflow_id)
    if head is None:
        raise ValueError(f"workflow {workflow_id} has no head")

    request_id = str(head.get("request_id") or "")
    phase_raw = head.get("phase")
    if not request_id or phase_raw not in ("funding", "budget"):
        raise ValueError(f"head for workflow {workflow_id} is missing request_id or valid phase")
    phase: FundingWorkflowPhase = "budget" if phase_raw == "budget" else "funding"

    scope_raw = head.get("scope")
    if scope_raw is None or not isinstance(scope_raw, (list, tuple)) or len(scope_raw) != 4:
        raise ValueError(f"head for workflow {workflow_id} has invalid scope")
    scope = (scope_raw[0], scope_raw[1], scope_raw[2], scope_raw[3])

    if not workflow_id.startswith(scope_prefix(scope)):
        raise ValueError(f"head scope does not match workflow_id {workflow_id}")

    request_payload = load_request_payload(store, request_id, phase)
    if request_payload is None:
        raise ValueError(f"request payload not found for {request_id} in phase {phase}")

    month_key = str(request_payload.get("month_key") or "")
    if not month_key:
        raise ValueError(f"request payload {request_id} missing month_key")

    card_delivery_version = int(request_payload.get("card_delivery_version") or 0)

    lineage = _lineage(store, workflow_id, request_id)
    predecessor_request_id, predecessor_completed = _required_funding_predecessor(
        store, workflow_id, request_id, phase
    )

    claims, completions = _claims_and_completions(store, workflow_id, request_id, phase)
    active_completion = completions.get(max(completions.keys())) if completions else None
    active_claim = claims.get(max(claims.keys())) if claims else None

    if active_completion is not None:
        comp_attempt = int(active_completion.get("attempt") or 0)
        completed_claim = claims[comp_attempt]
        raw_intent = completed_claim.get("intent")
        terminal_intent: Literal["confirm", "cancel"] | None = (
            "cancel" if raw_intent == "cancel" else "confirm"
        )
        raw_budget = completed_claim.get("selected_budget")
        selected_budget = float(raw_budget) if raw_budget is not None else None
        financial_actions_allowed = False
        attention = None
        if phase == "funding":
            stage: FundingWorkflowCardStage = (
                "funding_canceled" if terminal_intent == "cancel" else "funding_completed"
            )
        else:
            stage = "budget_canceled" if terminal_intent == "cancel" else "budget_completed"

    elif active_claim is not None:
        raw_intent = active_claim.get("intent")
        terminal_intent = "cancel" if raw_intent == "cancel" else "confirm"
        raw_budget = active_claim.get("selected_budget")
        selected_budget = float(raw_budget) if raw_budget is not None else None
        financial_actions_allowed = False
        if phase == "funding":
            stage = "funding_canceling" if terminal_intent == "cancel" else "funding_confirming"
        else:
            stage = "budget_canceling" if terminal_intent == "cancel" else "budget_applying"

        rec_attempts = recovery_owned_incomplete_attempts(store, workflow_id)
        active_attempt = int(active_claim.get("attempt") or 0)
        if (request_id, phase, active_attempt) in rec_attempts:
            attention = "incomplete_transition"
        else:
            attention = None

    else:
        terminal_intent = None
        selected_budget = None
        if phase == "funding":
            stage = "funding_pending"
            financial_actions_allowed = True
            attention = None
        else:
            stage = "budget_pending"
            if predecessor_request_id is not None and not predecessor_completed:
                financial_actions_allowed = False
                attention = "predecessor_incomplete"
            else:
                financial_actions_allowed = True
                attention = None

    return FundingWorkflowCardModel(
        workflow_id=workflow_id,
        month_key=month_key,
        scope=scope,
        phase=phase,
        request_id=request_id,
        request=request_payload,
        stage=stage,
        attention=attention,
        financial_actions_allowed=financial_actions_allowed,
        terminal_intent=terminal_intent,
        selected_budget=selected_budget,
        predecessor_request_id=predecessor_request_id,
        predecessor_completed=predecessor_completed,
        card_delivery_version=card_delivery_version,
        lineage=lineage,
    )


__all__ = [
    "FundingWorkflowAttention",
    "FundingWorkflowCardModel",
    "FundingWorkflowCardStage",
    "FundingWorkflowPhase",
    "FundingWorkflowRequestRef",
    "funding_workflow_card_key",
    "project_funding_workflow_card",
    "recovery_owned_incomplete_attempts",
]
