"""Tests for legacy funding request card adoption and per-chat delivery coordination."""

from __future__ import annotations

from typing import Any

import pytest

from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.ui.card_state import (
    CardCopy,
    card_adoption_event,
    card_failure_event,
    card_intent_event,
    card_result_event,
    resolve_card_copies,
)
from maestro.integrations.telegram.ui.funding_workflow import (
    FundingWorkflowCardModel,
    FundingWorkflowRequestRef,
    funding_workflow_card_key,
)
from maestro.integrations.telegram.ui.funding_workflow_delivery import (
    FundingWorkflowCardDelivery,
    FundingWorkflowCardSyncResult,
)
from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class FakeClient:
    def __init__(self, *, reject_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.reject_for = reject_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> dict[str, Any]:
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.next_message_id += 1
        self.sent.append((chat_id, text))
        return {"message_id": self.next_message_id}

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> dict[str, Any]:
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused edit in chat {chat_id}")
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}


def _setup_delivery(
    tmp_path: Any,
    client: Any | None = None,
    chat_ids: tuple[int, ...] = (100, 200),
) -> tuple[StateStore, CardLifecycleManager, FundingWorkflowCardDelivery, FakeClient]:
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000.0)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    cli = client if client is not None else FakeClient()
    lifecycle = CardLifecycleManager(store, audit, cli, chat_ids=chat_ids)
    delivery = FundingWorkflowCardDelivery(store, lifecycle)
    return store, lifecycle, delivery, cli


def _make_model(
    *,
    workflow_id: str = "wf_1",
    phase: str = "funding",
    request_id: str = "req_1",
    stage: str = "funding_pending",
    card_delivery_version: int = 1,
    lineage: tuple[FundingWorkflowRequestRef, ...] | None = None,
) -> FundingWorkflowCardModel:
    if lineage is None:
        lineage = (
            FundingWorkflowRequestRef(
                request_id=request_id,
                phase=phase,  # type: ignore[arg-type]
                lineage_distance=0,
            ),
        )
    return FundingWorkflowCardModel(
        workflow_id=workflow_id,
        month_key="2026-08",
        scope=("core", "paper_cash", "krw_contribution", "KRW"),
        phase=phase,  # type: ignore[arg-type]
        request_id=request_id,
        request={
            "request_id": request_id,
            "contribution_group_id": "core",
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "currency": "KRW",
            "month_key": "2026-08",
            "status": "pending",
            "strategy_ids": ["tranquillo"],
            "required_shortfall": 1_000_000.0,
            "card_delivery_version": card_delivery_version,
        },
        stage=stage,  # type: ignore[arg-type]
        attention=None,
        financial_actions_allowed=True,
        terminal_intent=None,
        selected_budget=None,
        predecessor_request_id=None,
        predecessor_completed=None,
        card_delivery_version=card_delivery_version,
        lineage=lineage,
    )


# --- Step 1: Adoption precedence and provenance tests ---


def test_card_adoption_event_structure_and_fold():
    source = CardCopy(
        card_key="funding-request:req_1",
        chat_id=100,
        message_id=5001,
        stage="pending",
        render_hash="old_hash_123",
        delivery="confirmed",
        operation_id="op_src_1",
    )
    event = card_adoption_event(
        "funding-workflow:wf_1",
        100,
        source=source,
        source_request_id="req_1",
        source_phase="funding",
    )

    assert event["phase"] == "adoption"
    assert event["delivery"] == "confirmed"
    assert event["card_key"] == "funding-workflow:wf_1"
    assert event["chat_id"] == 100
    assert event["stage"] == "pending"
    assert event["render_hash"] == "old_hash_123"
    assert event["operation_id"] == "adopt:op_src_1"
    assert event["message_id"] == 5001
    assert event["adopted_from_card_key"] == "funding-request:req_1"
    assert event["adopted_from_operation_id"] == "op_src_1"
    assert event["adopted_from_request_id"] == "req_1"
    assert event["adopted_from_phase"] == "funding"
    assert (
        event["duplicate_key"]
        == "telegram-ui-card:adoption:funding-workflow:wf_1:100:funding-request:req_1:op_src_1"
    )

    # Fold with resolve_card_copies
    copies = resolve_card_copies([event])
    copy = copies[("funding-workflow:wf_1", 100)]
    assert copy.delivery == "confirmed"
    assert copy.message_id == 5001
    assert copy.stage == "pending"
    assert copy.render_hash == "old_hash_123"
    assert copy.operation_id == "adopt:op_src_1"

    # Test unknown delivery preserved in fold
    source_unknown = CardCopy(
        card_key="funding-request:req_1",
        chat_id=100,
        message_id=None,
        stage="pending",
        render_hash="old_hash_123",
        delivery="unknown",
        operation_id="op_src_2",
    )
    event_unknown = card_adoption_event(
        "funding-workflow:wf_1",
        100,
        source=source_unknown,
        source_request_id="req_1",
        source_phase="funding",
    )
    copy_unknown = resolve_card_copies([event_unknown])[("funding-workflow:wf_1", 100)]
    assert copy_unknown.delivery == "unknown"
    assert copy_unknown.message_id is None

    # Test failed delivery preserved in fold
    source_failed = CardCopy(
        card_key="funding-request:req_1",
        chat_id=100,
        message_id=None,
        stage="pending",
        render_hash="old_hash_123",
        delivery="failed",
        operation_id="op_src_3",
    )
    event_failed = card_adoption_event(
        "funding-workflow:wf_1",
        100,
        source=source_failed,
        source_request_id="req_1",
        source_phase="funding",
    )
    copy_failed = resolve_card_copies([event_failed])[("funding-workflow:wf_1", 100)]
    assert copy_failed.delivery == "failed"
    assert copy_failed.message_id is None


def test_adopt_current_request_confirmed(tmp_path):
    """Current request confirmed -> adopt its message_id, edit to workflow projection, send nothing."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    # Setup legacy confirmed copy
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "old_hash", "op_req_1"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_1", 100, "pending", "old_hash", "op_req_1", message_id=7001
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert len(client.sent) == 0
    assert len(client.edited) == 1
    assert client.edited[0][0] == 100
    assert client.edited[0][1] == 7001

    # Verify adoption history row
    events = store.list_system_events_by_type("telegram_ui_card", limit=None)
    adoption_events = [e for e in events if e.get("payload", {}).get("phase") == "adoption"]
    assert len(adoption_events) == 1
    payload = adoption_events[0]["payload"]
    assert payload["phase"] == "adoption"
    assert payload["delivery"] == "confirmed"
    assert payload["stage"] == "pending"
    assert payload["render_hash"] == "old_hash"
    assert payload["adopted_from_card_key"] == "funding-request:req_1"
    assert payload["adopted_from_operation_id"] == "op_req_1"
    assert payload["adopted_from_request_id"] == "req_1"
    assert payload["adopted_from_phase"] == "funding"

    # Verify no result event for adopt operation
    adopt_results = [
        e
        for e in events
        if e.get("payload", {}).get("phase") == "result"
        and e.get("payload", {}).get("operation_id") == "adopt:op_req_1"
    ]
    assert len(adopt_results) == 0


def test_adopt_current_request_failed_allows_retry(tmp_path):
    """Current request failed -> adopt known non-delivery and allow a retry in that chat."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "old_hash", "op_req_1"),
    )
    store.record_card_event(
        "run_0",
        card_failure_event(
            "funding-request:req_1", 100, "pending", "old_hash", "op_req_1", "telegram error"
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "sent"
    assert len(client.sent) == 1
    assert client.sent[0][0] == 100


def test_adopt_current_request_unknown_blocks_resend_and_predecessor_promotion(tmp_path):
    """Current request unknown -> adopt unknown, emit buttonless ambiguity notice, do not send, do not edit predecessor."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    # Setup confirmed predecessor
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_0", 100, "pending", "hash_0", "op_req_0"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_0", 100, "pending", "hash_0", "op_req_0", message_id=4001
        ),
    )

    # Setup current request with intent only (unknown delivery)
    store.record_card_event(
        "run_1",
        card_intent_event("budget-request:req_1", 100, "budget_pending", "hash_1", "op_req_1"),
    )

    lineage = (
        FundingWorkflowRequestRef(request_id="req_1", phase="budget", lineage_distance=0),
        FundingWorkflowRequestRef(request_id="req_0", phase="funding", lineage_distance=1),
    )
    model = _make_model(
        phase="budget",
        request_id="req_1",
        stage="budget_pending",
        card_delivery_version=1,
        lineage=lineage,
    )
    result = delivery.sync("run_2", model)

    assert result.outcome_for(100) == "unknown"
    assert len(client.edited) == 0
    # Ambiguity notice sent via send_message
    assert len(client.sent) == 1
    assert client.sent[0][0] == 100
    assert "⚠️" in client.sent[0][1]

    # Ambiguity notice recorded in system events
    notice_events = store.list_system_events_by_type("telegram_ui_card_ambiguous", limit=None)
    assert len(notice_events) >= 1
    assert "funding-workflow:wf_1" in str(notice_events[0]["payload"])


def test_no_current_copy_adopts_nearest_confirmed_predecessor(tmp_path):
    """No current copy + nearest confirmed predecessor -> adopt predecessor and edit it."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_0", 100, "pending", "hash_0", "op_req_0"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_0", 100, "pending", "hash_0", "op_req_0", message_id=4001
        ),
    )

    lineage = (
        FundingWorkflowRequestRef(request_id="req_1", phase="budget", lineage_distance=0),
        FundingWorkflowRequestRef(request_id="req_0", phase="funding", lineage_distance=1),
    )
    model = _make_model(
        phase="budget",
        request_id="req_1",
        stage="budget_pending",
        card_delivery_version=1,
        lineage=lineage,
    )
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert len(client.edited) == 1
    assert client.edited[0][1] == 4001
    assert len(client.sent) == 0


def test_superseded_predecessor_unknown_does_not_block_current_generation_1_send(tmp_path):
    """Superseded predecessor unknown + current generation 1 with no current evidence -> legacy unknown remains visible, current sent."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    # Predecessor left as unknown
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_0", 100, "pending", "hash_0", "op_req_0"),
    )

    lineage = (
        FundingWorkflowRequestRef(request_id="req_1", phase="budget", lineage_distance=0),
        FundingWorkflowRequestRef(request_id="req_0", phase="funding", lineage_distance=1),
    )
    model = _make_model(
        phase="budget",
        request_id="req_1",
        stage="budget_pending",
        card_delivery_version=1,
        lineage=lineage,
    )
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "sent"
    assert len(client.sent) == 1

    # Predecessor still unknown in legacy key
    pred_copies = store.load_card_delivery_state("funding-request:req_0")
    assert len(pred_copies) == 1
    assert pred_copies[0]["delivery"] == "unknown"


def test_multiple_confirmed_predecessors_picks_smallest_lineage_distance(tmp_path):
    """Multiple confirmed predecessors -> choose smallest lineage_distance even if older candidate has newer event."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    # Older lineage (distance 2, req_0) recorded with later message_id / timestamp
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_0", 100, "pending", "hash_0", "op_0"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_0", 100, "pending", "hash_0", "op_0", message_id=3001
        ),
    )

    # Nearer lineage (distance 1, req_1) recorded
    store.record_card_event(
        "run_1",
        card_intent_event("funding-request:req_1", 100, "pending", "hash_1", "op_1"),
    )
    store.record_card_event(
        "run_1",
        card_result_event(
            "funding-request:req_1", 100, "pending", "hash_1", "op_1", message_id=4001
        ),
    )

    lineage = (
        FundingWorkflowRequestRef(request_id="req_2", phase="budget", lineage_distance=0),
        FundingWorkflowRequestRef(request_id="req_1", phase="funding", lineage_distance=1),
        FundingWorkflowRequestRef(request_id="req_0", phase="funding", lineage_distance=2),
    )
    model = _make_model(
        phase="budget",
        request_id="req_2",
        stage="budget_pending",
        card_delivery_version=1,
        lineage=lineage,
    )
    result = delivery.sync("run_2", model)

    assert result.outcome_for(100) == "edited"
    assert len(client.edited) == 1
    # Must have edited req_1's message (4001), not req_0's message (3001)
    assert client.edited[0][1] == 4001


def test_existing_workflow_scoped_copy_wins_over_request_events(tmp_path):
    """Existing workflow-scoped copy in a chat -> ignore every request-scoped event for that chat."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100,))

    # Pre-existing workflow copy
    store.record_card_event(
        "run_0",
        card_intent_event("funding-workflow:wf_1", 100, "funding_pending", "wf_hash_0", "op_wf_0"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-workflow:wf_1",
            100,
            "funding_pending",
            "wf_hash_0",
            "op_wf_0",
            message_id=9001,
        ),
    )

    # Legacy request copy with a different message_id
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "req_hash", "op_req_1"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_1", 100, "pending", "req_hash", "op_req_1", message_id=5001
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert len(client.edited) == 1
    assert client.edited[0][1] == 9001  # Edited the workflow copy, not legacy copy 5001

    # No adoption event recorded
    events = store.list_system_events_by_type("telegram_ui_card", limit=None)
    adoptions = [e for e in events if e.get("payload", {}).get("phase") == "adoption"]
    assert len(adoptions) == 0


# --- Step 2: Audience and per-chat tests ---


def test_generation_0_with_no_evidence_is_blocked(tmp_path):
    """Generation 0 with no lifecycle evidence -> every configured chat outcome is blocked; no audience or send."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    model = _make_model(request_id="req_1", card_delivery_version=0)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "blocked"
    assert result.outcome_for(200) == "blocked"
    assert result.outcome_for(999) == "blocked"
    assert len(client.sent) == 0
    assert len(client.edited) == 0
    assert len(store.load_card_audience("funding-workflow:wf_1")) == 0


def test_generation_1_with_no_evidence_pins_audience_and_sends(tmp_path):
    """Generation 1 with no lifecycle evidence -> current configured audience is pinned and each chat gets initial send."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "sent"
    assert result.outcome_for(200) == "sent"
    assert len(client.sent) == 2
    assert store.load_card_audience("funding-workflow:wf_1") == [100, 200]


def test_generation_0_with_evidence_in_one_chat_only_updates_that_chat(tmp_path):
    """Generation 0 with evidence in chat 100 and newly configured chat 200 -> only chat 100 is pinned/updated; chat 200 blocked."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    # Evidence only in chat 100
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "old_hash", "op_100"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_1", 100, "pending", "old_hash", "op_100", message_id=6001
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=0)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert result.outcome_for(200) == "blocked"
    assert len(client.sent) == 0
    assert len(client.edited) == 1
    assert client.edited[0][0] == 100
    assert store.load_card_audience("funding-workflow:wf_1") == [100]


def test_current_unknown_in_one_chat_and_no_evidence_in_second_chat_gen1(tmp_path):
    """Current request unknown in chat 100 and no evidence in chat 200 for generation 1 -> 100 unknown, 200 sent."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    # Chat 100 has unknown current copy
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "old_hash", "op_100"),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "unknown"
    assert result.outcome_for(200) == "sent"
    # One ambiguity notice to chat 100, one initial card send to chat 200
    assert len(client.sent) == 2
    sent_100 = [s for s in client.sent if s[0] == 100]
    sent_200 = [s for s in client.sent if s[0] == 200]
    assert len(sent_100) == 1
    assert "⚠️" in sent_100[0][1]
    assert len(sent_200) == 1
    assert "입금이 필요해요" in sent_200[0][1]
    assert store.load_card_audience("funding-workflow:wf_1") == [100, 200]


def test_per_chat_independent_handling_confirmed_and_failed(tmp_path):
    """Current confirmed copy in chat 100 and current failed copy in chat 200 -> adopt/edit 100 and retry 200 independently."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    # Chat 100 confirmed
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 100, "pending", "old_hash", "op_100"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_1", 100, "pending", "old_hash", "op_100", message_id=6001
        ),
    )

    # Chat 200 failed
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 200, "pending", "old_hash", "op_200"),
    )
    store.record_card_event(
        "run_0",
        card_failure_event(
            "funding-request:req_1", 200, "pending", "old_hash", "op_200", "rejected"
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert result.outcome_for(200) == "sent"
    assert len(client.edited) == 1
    assert client.edited[0][0] == 100
    assert len(client.sent) == 1
    assert client.sent[0][0] == 200


def test_workflow_state_in_one_chat_does_not_prevent_adoption_in_other_chat(tmp_path):
    """A workflow-scoped state in chat 100 prevents legacy reclaim only in chat 100; chat 200 follows precedence."""
    store, lifecycle, delivery, client = _setup_delivery(tmp_path, chat_ids=(100, 200))

    # Chat 100 has workflow-scoped copy
    store.record_card_event(
        "run_0",
        card_intent_event("funding-workflow:wf_1", 100, "funding_pending", "wf_hash", "op_wf_100"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-workflow:wf_1",
            100,
            "funding_pending",
            "wf_hash",
            "op_wf_100",
            message_id=9001,
        ),
    )

    # Chat 200 has legacy confirmed copy
    store.record_card_event(
        "run_0",
        card_intent_event("funding-request:req_1", 200, "pending", "old_hash", "op_req_200"),
    )
    store.record_card_event(
        "run_0",
        card_result_event(
            "funding-request:req_1", 200, "pending", "old_hash", "op_req_200", message_id=6002
        ),
    )

    model = _make_model(request_id="req_1", card_delivery_version=1)
    result = delivery.sync("run_1", model)

    assert result.outcome_for(100) == "edited"
    assert result.outcome_for(200) == "edited"
    assert len(client.edited) == 2
    edited_map = {c[0]: c[1] for c in client.edited}
    assert edited_map[100] == 9001
    assert edited_map[200] == 6002

    # Adoption event only for chat 200
    events = store.list_system_events_by_type("telegram_ui_card", limit=None)
    adoptions = [e for e in events if e.get("payload", {}).get("phase") == "adoption"]
    assert len(adoptions) == 1
    assert adoptions[0]["payload"]["chat_id"] == 200
