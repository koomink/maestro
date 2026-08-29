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
    card_result_event,
)
from maestro.integrations.telegram.ui.funding_workflow import (
    funding_workflow_card_key,
)
from maestro.monitoring.audit_logger import AuditLogger
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
    def __init__(self, *, reject_for: set[int] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.reject_for = reject_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> dict[str, Any]:
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": self.next_message_id}}

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> dict[str, Any]:
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
    store.save_system_event(
        "r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY}
    )


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


def test_v0_head_with_failed_evidence_adopts_and_retries_on_direct_invocation_not_poll_once(
    tmp_path: Path,
) -> None:
    router, store, client = _setup_router(tmp_path, chat_ids=(100, 200))
    req = _funding_req("req-1", card_delivery_version=0)
    outcome = publish_contribution_request(store, "run-pub", req, phase="funding")
    wf_id = outcome["workflow_id"]

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

    # poll_once must NOT invoke the helper or send anything
    router.poll_once()
    assert client.sent == []
    assert client.edited == []

    # Direct test invocation adopts and retries under the workflow key
    res = router._refresh_funding_workflow_card(wf_id)
    assert res.outcome_for(100) == "sent"
    assert res.outcome_for(200) == "blocked"
    assert len(client.sent) == 1
    assert client.sent[0]["chat_id"] == 100


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
            pytest.fail(f"Financial side effect '{name}' was unexpectedly invoked during card sweep")

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


def test_poll_once_does_not_call_funding_workflow_sweep_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, _, _ = _setup_router(tmp_path)
    called: list[bool] = []
    monkeypatch.setattr(router, "_sweep_funding_workflow_cards", lambda: called.append(True))

    router.poll_once()

    assert called == []
