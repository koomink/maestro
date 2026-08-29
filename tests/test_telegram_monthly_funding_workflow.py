"""Tests for migration-fenced monthly funding workflow card refresh and sweep service."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.integrations.telegram.ui.card_state import (
    card_failure_event,
    card_intent_event,
    card_result_event,
)
from maestro.integrations.telegram.ui.funding_workflow import (
    funding_workflow_card_key,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import SignalRunSummary
from maestro.state import migration_state as ms
from maestro.state.funding_workflow import (
    claim_workflow_attempt,
    complete_workflow,
    publish_contribution_request,
    workflow_id_from_request,
)
from maestro.state.store import StateStore


def _telegram_config_path(tmp_path: Path, *, chat_ids: tuple[int, ...] = (100, 200)) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": list(chat_ids),
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "telegram_operator.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


class FakeTelegramClient:
    def __init__(
        self,
        *,
        reject_for: set[int] | None = None,
        timeout_for: set[int] | None = None,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.reject_for = reject_for or set()
        self.timeout_for = timeout_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> dict[str, Any]:
        if chat_id in self.timeout_for:
            raise TimeoutError(f"telegram timeout chat {chat_id}")
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": self.next_message_id}}

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> dict[str, Any]:
        if chat_id in self.timeout_for:
            raise TimeoutError(f"telegram timeout edit in chat {chat_id}")
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused edit in chat {chat_id}")
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    def get_updates(
        self, *, offset: int | None = None, timeout_seconds: int = 0, allowed_updates: Any = None
    ) -> dict[str, Any]:
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict[str, Any]:
        return {"ok": True}


def _setup_router(
    tmp_path: Path,
    *,
    chat_ids: tuple[int, ...] = (100, 200),
    client: FakeTelegramClient | None = None,
) -> tuple[TelegramOperatorCommandRouter, StateStore, FakeTelegramClient]:
    config = load_config(_telegram_config_path(tmp_path, chat_ids=chat_ids))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    cli = client if client is not None else FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=cli,
    )
    return router, store, cli


def _funding_req(
    request_id: str,
    *,
    month_key: str = "2026-08",
    card_delivery_version: int = 0,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "paper_cash",
        "execution_sleeve": "krw_contribution",
        "currency": "KRW",
        "month_key": month_key,
        "status": "pending",
        "strategy_ids": ["tranquillo"],
        "required_shortfall": 1_000_000.0,
        "card_delivery_version": card_delivery_version,
    }


def _budget_req(
    request_id: str,
    *,
    month_key: str = "2026-08",
    card_delivery_version: int = 0,
) -> dict[str, Any]:
    return {
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
        "card_delivery_version": card_delivery_version,
    }


def _start_migration(store: StateStore) -> None:
    with store.writer_lock("test"):
        ms.start_migration(store, "run-mig")


def _make_invalid_migration(store: StateStore) -> None:
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY})


# ---------------------------------------------------------------------------
# Step 1: Generation and restart sweep tests
# ---------------------------------------------------------------------------


def test_sweep_v0_head_without_evidence_sends_nothing(tmp_path: Path) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=0)
    outcome = publish_contribution_request(store, "run-pub", req, phase="funding")
    wf_id = outcome["workflow_id"]

    router._sweep_funding_workflow_cards()

    assert client.sent == []
    assert client.edited == []

    res = router._refresh_funding_workflow_card(wf_id)
    assert res.outcome_for(100) == "blocked"
    assert res.outcome_for(200) == "blocked"
    assert client.sent == []
    assert client.edited == []


def test_v0_head_with_failed_evidence_adopts_and_retries_on_poll_once(
    tmp_path: Path,
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=0)
    publish_contribution_request(store, "run-pub", req, phase="funding")

    # Record request-scoped failed delivery evidence for chat 100 only
    req_card_key = "funding-request:req-1"
    store.record_card_event(
        "run-fail",
        card_failure_event(
            req_card_key,
            100,
            stage="funding_pending",
            render_hash="oldhash",
            operation_id="op-fail-1",
            error="telegram refused",
        ),
    )

    # poll_once executes _sweep_funding_workflow_cards, which adopts and retries
    # under the workflow key
    router.poll_once()
    assert len(client.sent) == 1
    assert client.sent[0]["chat_id"] == 100
    assert client.edited == []


def test_sweep_v1_head_sends_workflow_scoped_card_to_all_configured_chats(tmp_path: Path) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=1)
    outcome = publish_contribution_request(store, "run-pub", req, phase="funding")
    wf_id = outcome["workflow_id"]

    router._sweep_funding_workflow_cards()

    assert len(client.sent) == 2
    assert {m["chat_id"] for m in client.sent} == {100, 200}

    expected_card_key = f"funding-workflow:{wf_id}"
    assert funding_workflow_card_key(wf_id) == expected_card_key
    assert set(store.load_card_audience(expected_card_key)) == {100, 200}
    delivery_states = store.load_card_delivery_state(expected_card_key)
    assert {row["chat_id"] for row in delivery_states} == {100, 200}
    assert all(row["delivery"] == "confirmed" for row in delivery_states)


def test_sweep_restart_does_not_send_duplicate(tmp_path: Path) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=1)
    publish_contribution_request(store, "run-pub", req, phase="funding")

    router._sweep_funding_workflow_cards()
    assert len(client.sent) == 2

    # New router instance over the exact same SQLite database
    client2 = FakeTelegramClient()
    router2, _, _ = _setup_router(tmp_path, chat_ids=(100, 200), client=client2)

    router2._sweep_funding_workflow_cards()
    assert client2.sent == []
    assert client2.edited == []


# ---------------------------------------------------------------------------
# Step 2: Shared-boundary migration and side-effect tests
# ---------------------------------------------------------------------------


def _event_count(store: StateStore) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0])


@pytest.mark.parametrize("marker_type", ["MIGRATING", "INVALID"])
def test_refresh_and_sweep_blocked_when_migrating_or_invalid(
    tmp_path: Path, marker_type: str
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=1)
    outcome = publish_contribution_request(store, "run-pub", req, phase="funding")
    wf_id = outcome["workflow_id"]

    if marker_type == "MIGRATING":
        _start_migration(store)
    else:
        _make_invalid_migration(store)

    events_before = _event_count(store)
    target_card_key = funding_workflow_card_key(wf_id)

    # 1. Direct refresh invocation stands down
    res = router._refresh_funding_workflow_card(wf_id)
    assert res.outcome_for(100) == "blocked"
    assert res.outcome_for(200) == "blocked"
    assert client.sent == []
    assert client.edited == []
    assert _event_count(store) == events_before
    assert store.load_card_audience(target_card_key) == []
    assert store.load_card_delivery_state(target_card_key) == []

    # 2. Sweep stands down
    router._sweep_funding_workflow_cards()
    assert client.sent == []
    assert client.edited == []
    assert _event_count(store) == events_before
    assert store.load_card_audience(target_card_key) == []
    assert store.load_card_delivery_state(target_card_key) == []


@pytest.mark.parametrize("marker_type", ["MIGRATING", "INVALID"])
def test_migration_race_financial_truth_preserved_ui_refresh_stands_down(
    tmp_path: Path, marker_type: str
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))

    # Normal migration state: funding completed, budget successor published
    req_f = _funding_req("req-f", card_delivery_version=1)
    pub_f = publish_contribution_request(store, "run-f", req_f, phase="funding")
    wf_id = pub_f["workflow_id"]

    claim_res = claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-f",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_res["claimed"]

    complete_res = complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-f",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-f", "status": "acknowledged"},
    )
    assert complete_res["committed"]

    req_b = _budget_req("req-b", card_delivery_version=1)
    pub_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-f",
        successor_of_phase="funding",
    )
    assert pub_b["committed"]

    head_before = store.load_funding_workflow_head(wf_id)
    assert head_before is not None
    assert head_before["request_id"] == "req-b"
    assert head_before["phase"] == "budget"

    # Migration state becomes MIGRATING or INVALID
    if marker_type == "MIGRATING":
        _start_migration(store)
    else:
        _make_invalid_migration(store)

    events_before = _event_count(store)
    target_card_key = funding_workflow_card_key(wf_id)

    # UI refresh stands down
    res = router._refresh_funding_workflow_card(wf_id)
    assert res.outcome_for(100) == "blocked"
    assert res.outcome_for(200) == "blocked"
    assert client.sent == []
    assert client.edited == []

    # Financial truth is preserved
    head_after = store.load_funding_workflow_head(wf_id)
    assert head_after == head_before
    assert _event_count(store) == events_before
    assert store.load_card_audience(target_card_key) == []
    assert store.load_card_delivery_state(target_card_key) == []


def test_sweep_does_not_invoke_financial_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=1)
    publish_contribution_request(store, "run-pub", req, phase="funding")

    def _fail_side_effect(name: str) -> Any:
        def _raise(*args: Any, **kwargs: Any) -> None:
            pytest.fail(
                f"Financial side effect '{name}' was unexpectedly invoked during card sweep"
            )

        return _raise

    monkeypatch.setattr(
        "maestro.state.funding_workflow.claim_workflow_attempt",
        _fail_side_effect("claim_workflow_attempt"),
    )
    monkeypatch.setattr(
        "maestro.state.funding_workflow.complete_workflow",
        _fail_side_effect("complete_workflow"),
    )
    monkeypatch.setattr(
        "maestro.state.funding_workflow.converge_workflow_invariants",
        _fail_side_effect("converge_workflow_invariants"),
    )
    monkeypatch.setattr(
        router,
        "_run_child_signal",
        _fail_side_effect("_run_child_signal"),
    )
    monkeypatch.setattr(
        router,
        "_record_account_cash_flow_from_funding_request",
        _fail_side_effect("_record_account_cash_flow_from_funding_request"),
    )
    monkeypatch.setattr(
        router,
        "_record_strategy_cash_flow_from_funding_request",
        _fail_side_effect("_record_strategy_cash_flow_from_funding_request"),
    )

    router._sweep_funding_workflow_cards()
    assert len(client.sent) == 2


# ---------------------------------------------------------------------------
# Step 3: Malformed isolation and poll_once spy
# ---------------------------------------------------------------------------


def test_sweep_isolates_malformed_workflow_and_logs_failure(tmp_path: Path) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))

    # 1. One valid workflow
    req_valid = _funding_req("req-valid", card_delivery_version=1)
    pub_valid = publish_contribution_request(store, "run-valid", req_valid, phase="funding")
    wf_valid = pub_valid["workflow_id"]

    # 2. One broken head pointing to a nonexistent request
    broken_wf_id = "monthly_funding:2026-08:core:paper_cash:krw_contribution:KRW:broken"
    store.save_system_event(
        "run-broken",
        "funding_workflow_head",
        {
            "workflow_id": broken_wf_id,
            "request_id": "nonexistent-req",
            "phase": "funding",
            "version": 1,
            "scope": ["core", "paper_cash", "krw_contribution", "KRW"],
            "duplicate_key": f"funding_workflow_head:{broken_wf_id}:1",
        },
    )

    router._sweep_funding_workflow_cards()

    # Valid workflow card was still processed and delivered
    valid_card_key = funding_workflow_card_key(wf_valid)
    assert set(store.load_card_audience(valid_card_key)) == {100, 200}
    assert len(client.sent) == 2

    # Malformed head failure was logged as an error
    error_events = [
        e
        for e in store.list_system_events_by_type("telegram_command", limit=None)
        if e["payload"].get("status") == "error"
    ]
    assert len(error_events) >= 1
    assert "ValueError" in [e["payload"].get("error_type") for e in error_events]


@pytest.mark.parametrize(
    ("req_factory", "phase"),
    [
        (_funding_req, "funding"),
        (_budget_req, "budget"),
    ],
)
def test_refresh_request_workflow_card_delegates_to_refresh_funding_workflow_card(
    tmp_path: Path,
    req_factory: Any,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = req_factory("req-1", card_delivery_version=1)
    publish_contribution_request(store, "run-pub", req, phase=phase)

    expected_wf_id = workflow_id_from_request(req)
    spy_calls: list[str] = []
    orig_refresh = router._refresh_funding_workflow_card

    def spy_refresh(wf_id: str):
        spy_calls.append(wf_id)
        return orig_refresh(wf_id)

    monkeypatch.setattr(router, "_refresh_funding_workflow_card", spy_refresh)

    result = router._refresh_request_workflow_card(req)

    assert spy_calls == [expected_wf_id]
    assert result.card_key == funding_workflow_card_key(expected_wf_id)
    assert result.outcome_for(100) == "sent"
    assert result.outcome_for(200) == "sent"


# ---------------------------------------------------------------------------
# Step 5 (Task 8 Step 2): Direct-seam migration fence tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker_type", ["MIGRATING", "INVALID"])
def test_request_workflow_card_refresh_blocked_when_migrating_or_invalid(
    tmp_path: Path, marker_type: str
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))

    # Persist financial completion and head state
    req_f = _funding_req("req-f", card_delivery_version=1)
    pub_f = publish_contribution_request(store, "run-f", req_f, phase="funding")
    wf_id = pub_f["workflow_id"]

    claim_res = claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-f",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_res["claimed"]

    complete_res = complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-f",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-f", "status": "acknowledged"},
    )
    assert complete_res["committed"]

    req_b = _budget_req("req-b", card_delivery_version=1)
    pub_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-f",
        successor_of_phase="funding",
    )
    assert pub_b["committed"]

    head_before = store.load_funding_workflow_head(wf_id)
    assert head_before is not None
    assert head_before["request_id"] == "req-b"
    assert head_before["phase"] == "budget"

    # Set migration state to MIGRATING or INVALID
    if marker_type == "MIGRATING":
        _start_migration(store)
    else:
        _make_invalid_migration(store)

    events_before = _event_count(store)
    target_card_key = funding_workflow_card_key(wf_id)

    # Call _refresh_request_workflow_card directly
    res = router._refresh_request_workflow_card(req_b)

    # Assert blocked outcome
    assert res.outcome_for(100) == "blocked"
    assert res.outcome_for(200) == "blocked"
    assert client.sent == []
    assert client.edited == []

    # Durable financial truth preserved and no card events / audience recorded
    head_after = store.load_funding_workflow_head(wf_id)
    assert head_after == head_before
    assert _event_count(store) == events_before
    assert store.load_card_audience(target_card_key) == []
    assert store.load_card_delivery_state(target_card_key) == []


# ---------------------------------------------------------------------------
# Step 6 (Task 8 Step 3): Ambiguity and crash-boundary integration tests
# ---------------------------------------------------------------------------


def test_refresh_request_workflow_card_unknown_current_request_with_confirmed_predecessor(
    tmp_path: Path,
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))

    # 1. Predecessor funding request A confirmed
    req_a = _funding_req("req-a", card_delivery_version=1)
    pub_a = publish_contribution_request(store, "run-a", req_a, phase="funding")
    wf_id = pub_a["workflow_id"]

    store.record_card_event(
        "run-0",
        card_intent_event("funding-request:req-a", 100, "pending", "hash-a", "op-a"),
    )
    store.record_card_event(
        "run-0",
        card_result_event(
            "funding-request:req-a", 100, "pending", "hash-a", "op-a", message_id=4001
        ),
    )

    claim_res = claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_res["claimed"]

    complete_res = complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-a", "status": "acknowledged"},
    )
    assert complete_res["committed"]

    # 2. Current budget request B has intent only (unknown delivery)
    req_b = _budget_req("req-b", card_delivery_version=1)
    pub_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    assert pub_b["committed"]

    store.record_card_event(
        "run-1",
        card_intent_event("budget-request:req-b", 100, "budget_pending", "hash-b", "op-b"),
    )

    # 3. Direct seam invocation
    res = router._refresh_request_workflow_card(req_b)

    # Adopts current unknown dominance -> outcome unknown, no edit to predecessor 4001,
    # ambiguity notice sent
    assert res.outcome_for(100) == "unknown"
    assert len(client.edited) == 0
    assert len(client.sent) == 1
    assert client.sent[0]["chat_id"] == 100
    assert "⚠️" in client.sent[0]["text"]

    target_card_key = funding_workflow_card_key(wf_id)
    delivery_states = store.load_card_delivery_state(target_card_key)
    assert len(delivery_states) == 1
    assert delivery_states[0]["delivery"] == "unknown"


def test_refresh_request_workflow_card_generic_edit_rejection(tmp_path: Path) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))

    # 1. Publish funding request A and deliver card
    req_a = _funding_req("req-a", card_delivery_version=1)
    pub_a = publish_contribution_request(store, "run-a", req_a, phase="funding")
    wf_id = pub_a["workflow_id"]

    res_a = router._refresh_request_workflow_card(req_a)
    assert res_a.outcome_for(100) == "sent"
    assert len(client.sent) == 1

    # 2. Transition: complete funding A, publish budget B
    claim_res = claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_res["claimed"]

    complete_res = complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-a", "status": "acknowledged"},
    )
    assert complete_res["committed"]

    req_b = _budget_req("req-b", card_delivery_version=1)
    pub_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    assert pub_b["committed"]

    # 3. Telegram rejects edit in chat 100
    client.reject_for.add(100)

    res_b = router._refresh_request_workflow_card(req_b)
    assert res_b.outcome_for(100) == "failed"
    assert len(client.edited) == 0

    target_card_key = funding_workflow_card_key(wf_id)
    delivery_states = store.load_card_delivery_state(target_card_key)
    assert len(delivery_states) == 1
    assert delivery_states[0]["delivery"] == "failed"


def test_refresh_request_workflow_card_edit_timeout_preserves_truth_no_resend(
    tmp_path: Path,
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))

    # 1. Publish funding request A and deliver card
    req_a = _funding_req("req-a", card_delivery_version=1)
    pub_a = publish_contribution_request(store, "run-a", req_a, phase="funding")
    wf_id = pub_a["workflow_id"]

    res_a = router._refresh_request_workflow_card(req_a)
    assert res_a.outcome_for(100) == "sent"
    assert len(client.sent) == 1

    # 2. Transition: complete funding A, publish budget B
    claim_res = claim_workflow_attempt(
        store,
        "run-claim",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim_res["claimed"]

    complete_res = complete_workflow(
        store,
        "run-comp",
        workflow_id=wf_id,
        request_id="req-a",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-a", "status": "acknowledged"},
    )
    assert complete_res["committed"]

    req_b = _budget_req("req-b", card_delivery_version=1)
    pub_b = publish_contribution_request(
        store,
        "run-b",
        req_b,
        phase="budget",
        successor_of_request_id="req-a",
        successor_of_phase="funding",
    )
    assert pub_b["committed"]

    head_before = store.load_funding_workflow_head(wf_id)
    assert head_before is not None
    assert head_before["phase"] == "budget"
    assert head_before["request_id"] == "req-b"

    # 3. Telegram edit times out in chat 100
    client.timeout_for.add(100)

    res_b = router._refresh_request_workflow_card(req_b)
    assert res_b.outcome_for(100) == "unknown"

    # 4. Assert durable financial truth is preserved
    head_after = store.load_funding_workflow_head(wf_id)
    assert head_after == head_before
    target_card_key = funding_workflow_card_key(wf_id)
    delivery_states = store.load_card_delivery_state(target_card_key)
    assert len(delivery_states) == 1
    assert delivery_states[0]["delivery"] == "unknown"

    # 5. Subsequent directly invoked sweep does NOT resend replacement card
    client.timeout_for.clear()
    sent_count_before_sweep = len(client.sent)
    edited_count_before_sweep = len(client.edited)

    router._sweep_funding_workflow_cards()

    assert len(client.edited) == edited_count_before_sweep
    # Sweep sends at most an ambiguity notice if not already present, but never a replacement card
    assert not any("신규" in s.get("text", "") for s in client.sent[sent_count_before_sweep:])

    # Head and financial truth remain untouched
    assert store.load_funding_workflow_head(wf_id) == head_before


def test_all_live_transitions_delegate_immediate_refresh_through_shared_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))
    raw_signal = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw_signal["portfolio"]["initial_cash"] = 5_000_000
    raw_signal["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw_signal["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    sig_path = tmp_path / "signal.yaml"
    sig_path.write_text(yaml.safe_dump(raw_signal))
    router.signal_config_path = sig_path

    refreshed_workflows: list[str] = []
    real_refresh = router._refresh_funding_workflow_card

    def spy_refresh(wf_id: str):
        refreshed_workflows.append(wf_id)
        return real_refresh(wf_id)

    monkeypatch.setattr(router, "_refresh_funding_workflow_card", spy_refresh)
    monkeypatch.setattr(
        router,
        "_run_child_signal",
        lambda req, wf_id, attempt=1, phase="funding": SignalRunSummary(
            signal_run_id="signal-child",
            loaded_strategies=["tranquillo"],
            action_required=False,
            orders_preview_count=0,
        ),
    )

    # 1. funding cancel callback
    req_f1 = _funding_req("req-f1", card_delivery_version=1)
    publish_contribution_request(store, "run-f1", req_f1, phase="funding")
    router.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "data": "operator:funding:cancel:req-f1",
                "message": {"chat": {"id": 100}, "message_id": 10, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 1
    assert "2026-08" in refreshed_workflows[-1]

    # 2. funding complete callback
    req_f2 = _funding_req("req-f2", card_delivery_version=1)
    publish_contribution_request(store, "run-f2", req_f2, phase="funding")
    router.process_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-2",
                "data": "operator:funding:complete:req-f2",
                "message": {"chat": {"id": 100}, "message_id": 11, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 2

    # 3. budget cancel callback
    req_b1 = _budget_req("req-b1", card_delivery_version=1)
    publish_contribution_request(store, "run-b1", req_b1, phase="budget")
    router.process_update(
        {
            "update_id": 3,
            "callback_query": {
                "id": "cb-3",
                "data": "operator:budget:cancel:req-b1",
                "message": {"chat": {"id": 100}, "message_id": 12, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 3

    # 4. budget select callback
    req_b2 = _budget_req("req-b2", card_delivery_version=1)
    publish_contribution_request(store, "run-b2", req_b2, phase="budget")
    router.process_update(
        {
            "update_id": 4,
            "callback_query": {
                "id": "cb-4",
                "data": "operator:budget:sel:req-b2:r",
                "message": {"chat": {"id": 100}, "message_id": 13, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 4

    # 5. /budget command
    req_b3 = _budget_req("req-b3", card_delivery_version=1)
    publish_contribution_request(store, "run-b3", req_b3, phase="budget")
    router.process_update(
        {
            "update_id": 5,
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100, "username": "op"},
                "text": "/budget req-b3 300000",
            },
        }
    )
    assert len(refreshed_workflows) == 5

    # 6. funding resume callback
    req_f3 = _funding_req("req-f3", card_delivery_version=1)
    pub_f3 = publish_contribution_request(store, "run-f3", req_f3, phase="funding")
    claim_workflow_attempt(
        store,
        "run-claim-f3",
        workflow_id=pub_f3["workflow_id"],
        request_id="req-f3",
        phase="funding",
        attempt=1,
        extra={"intent": "cancel"},
    )
    router.process_update(
        {
            "update_id": 6,
            "callback_query": {
                "id": "cb-6",
                "data": "operator:wfresume:funding:req-f3",
                "message": {"chat": {"id": 100}, "message_id": 14, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 6

    # 7. budget resume callback
    req_b4 = _budget_req("req-b4", card_delivery_version=1)
    pub_b4 = publish_contribution_request(store, "run-b4", req_b4, phase="budget")
    claim_workflow_attempt(
        store,
        "run-claim-b4",
        workflow_id=pub_b4["workflow_id"],
        request_id="req-b4",
        phase="budget",
        attempt=1,
        extra={"intent": "cancel"},
    )
    router.process_update(
        {
            "update_id": 7,
            "callback_query": {
                "id": "cb-7",
                "data": "operator:wfresume:budget:req-b4",
                "message": {"chat": {"id": 100}, "message_id": 15, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 7

    # 8. child handoff
    req_f4 = _funding_req("req-f4", card_delivery_version=1)
    publish_contribution_request(store, "run-f4", req_f4, phase="funding")
    store.save_signal_package(
        "signal-child-handoff",
        {"orders_preview": [], "funding_requests": [req_f4], "budget_requests": []},
    )
    router._deliver_child_signal_outcome(
        100,
        SignalRunSummary(
            signal_run_id="signal-child-handoff",
            loaded_strategies=["tranquillo"],
            action_required=False,
            orders_preview_count=0,
        ),
    )
    assert len(refreshed_workflows) == 8

    # 9. transition exception after claim in finally
    req_f5 = _funding_req("req-f5", card_delivery_version=1)
    publish_contribution_request(store, "run-f5", req_f5, phase="funding")
    monkeypatch.setattr(
        router,
        "_run_child_signal",
        pytest.fail,
    )
    router.process_update(
        {
            "update_id": 9,
            "callback_query": {
                "id": "cb-9",
                "data": "operator:funding:complete:req-f5",
                "message": {"chat": {"id": 100}, "message_id": 16, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )
    assert len(refreshed_workflows) == 9


def test_migration_race_through_callback_finally_migrating_blocks_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))
    raw_signal = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw_signal["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw_signal["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    sig_path = tmp_path / "signal.yaml"
    sig_path.write_text(yaml.safe_dump(raw_signal))
    router.signal_config_path = sig_path

    req = _funding_req("req-f-mig", card_delivery_version=1)
    pub = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = pub["workflow_id"]

    import maestro.integrations.telegram.handlers as handlers_mod

    real_complete = handlers_mod.complete_workflow

    def racing_complete(*args, **kwargs):
        res = real_complete(*args, **kwargs)
        _start_migration(store)
        return res

    monkeypatch.setattr(handlers_mod, "complete_workflow", racing_complete)

    router.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "data": "operator:funding:cancel:req-f-mig",
                "message": {"chat": {"id": 100}, "message_id": 10, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )

    # Financial completion / head truth remains durable
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    assert any(e["payload"]["request_id"] == "req-f-mig" for e in completed)

    # Workflow card delivery state was blocked by migration (not created/projected)
    assert store.load_card_delivery_state(funding_workflow_card_key(wf_id)) == []


def test_migration_race_through_callback_finally_invalid_blocks_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100,))
    raw_signal = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw_signal["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw_signal["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    sig_path = tmp_path / "signal.yaml"
    sig_path.write_text(yaml.safe_dump(raw_signal))
    router.signal_config_path = sig_path

    req = _funding_req("req-f-inv", card_delivery_version=1)
    pub = publish_contribution_request(store, "run-1", req, phase="funding")
    wf_id = pub["workflow_id"]

    import maestro.integrations.telegram.handlers as handlers_mod

    real_complete = handlers_mod.complete_workflow

    def racing_complete(*args, **kwargs):
        res = real_complete(*args, **kwargs)
        _make_invalid_migration(store)
        return res

    monkeypatch.setattr(handlers_mod, "complete_workflow", racing_complete)

    router.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "data": "operator:funding:cancel:req-f-inv",
                "message": {"chat": {"id": 100}, "message_id": 10, "text": "test"},
                "from": {"id": 100, "username": "op"},
            },
        }
    )

    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    assert any(e["payload"]["request_id"] == "req-f-inv" for e in completed)
    assert store.load_card_delivery_state(funding_workflow_card_key(wf_id)) == []
