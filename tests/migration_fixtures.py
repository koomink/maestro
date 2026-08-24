"""Shared builders for the 3a-5 migration tests.

Two generations of state have to be constructible side by side here, and the
difference between them is the entire subject of these tests, so they are built
by two deliberately different routes. A *legacy* request is written with the
raw ``save_system_event`` API the pre-3a-4 binary used -- one row, no head, no
workflow identity. A *current* request goes through
``publish_contribution_request``, which commits the request and its head in one
transaction. Building the legacy shape by deleting rows from a current one
would test a state production never had.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from maestro.state.funding_workflow import (
    LEGACY_TERMINAL_EVENT,
    claim_workflow_attempt,
    complete_workflow,
    funding_workflow_id,
    publish_contribution_request,
)
from maestro.state.store import StateStore

ACCOUNT_ID = "paper_cash"
EXECUTION_SLEEVE = "krw_contribution"
CURRENCY = "KRW"
GROUP_ID = None

_REQUEST_EVENT = {
    "funding": "contribution_funding_request",
    "budget": "contribution_budget_request",
}
_LEGACY_KEY_PREFIX = {"funding": "funding-ack", "budget": "budget-decision"}


def make_store(tmp_path) -> StateStore:
    """The ``store`` fixture each migration test module declares over this.

    Deliberately not a fixture itself: importing a fixture by name into a
    module that also takes it as a parameter reads as a redefinition, and the
    indirection buys nothing over three lines of ``@pytest.fixture``.
    """
    return StateStore(tmp_path / "state.db", 1_000_000.0)


def workflow_id(month_key: str = "2026-08", **overrides: Any) -> str:
    scope = {
        "contribution_group_id": GROUP_ID,
        "account_id": ACCOUNT_ID,
        "execution_sleeve": EXECUTION_SLEEVE,
        "currency": CURRENCY,
    }
    scope.update(overrides)
    return funding_workflow_id(month_key=month_key, **scope)


def request_payload(
    request_id: str, *, month_key: str = "2026-08", status: str = "pending", **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "status": status,
        "month_key": month_key,
        "contribution_group_id": GROUP_ID,
        "account_id": ACCOUNT_ID,
        "execution_sleeve": EXECUTION_SLEEVE,
        "currency": CURRENCY,
        "strategy_ids": ["tranquillo"],
    }
    payload.update(overrides)
    return payload


def legacy_pending_request(
    store: StateStore,
    request_id: str,
    *,
    month_key: str = "2026-08",
    phase: str = "funding",
    **overrides: Any,
) -> dict[str, Any]:
    """A request as the pre-3a-4 binary wrote it: no head, no workflow id."""
    payload = request_payload(request_id, month_key=month_key, **overrides)
    payload["duplicate_key"] = f"{_REQUEST_EVENT[phase]}:{request_id}"
    store.save_system_event(f"run_{request_id}", _REQUEST_EVENT[phase], payload)
    return payload


def legacy_terminal_event(
    store: StateStore, request_id: str, *, phase: str = "funding", status: str = "acknowledged"
) -> None:
    """The old binary's terminal record, written without any workflow completion."""
    store.save_system_event(
        f"run_{request_id}",
        LEGACY_TERMINAL_EVENT[phase],
        {
            "request_id": request_id,
            "status": status,
            "duplicate_key": f"{_LEGACY_KEY_PREFIX[phase]}:{request_id}",
        },
    )


def publish_current_request(
    store: StateStore,
    request_id: str,
    *,
    month_key: str = "2026-08",
    phase: str = "funding",
    **overrides: Any,
) -> dict[str, Any]:
    payload = request_payload(request_id, month_key=month_key, **overrides)
    outcome = publish_contribution_request(store, f"run_{request_id}", payload, phase=phase)
    assert outcome["committed"], outcome
    return payload


def claim_only(
    store: StateStore,
    request_id: str,
    *,
    phase: str = "funding",
    attempt: int = 1,
    month_key: str = "2026-08",
    **extra: Any,
) -> None:
    claim = claim_workflow_attempt(
        store,
        f"run_{request_id}",
        workflow_id=workflow_id(month_key),
        request_id=request_id,
        phase=phase,
        attempt=attempt,
        extra={"intent": "confirm", **extra},
    )
    assert claim["claimed"], claim


def claim_and_complete(
    store: StateStore,
    request_id: str,
    *,
    phase: str = "funding",
    attempt: int = 1,
    month_key: str = "2026-08",
    legacy_payload: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    claim_only(
        store, request_id, phase=phase, attempt=attempt, month_key=month_key, **extra
    )
    complete_workflow(
        store,
        f"run_{request_id}",
        workflow_id=workflow_id(month_key),
        request_id=request_id,
        phase=phase,
        attempt=attempt,
        legacy_payload=legacy_payload
        or {
            "request_id": request_id,
            "status": "selected" if phase == "budget" else "acknowledged",
            "decided_by": "operator",
        },
    )


def delete_events(store: StateStore, event_type: str) -> int:
    """Remove an event type outright.

    Production never does this -- complete_workflow writes the workflow
    completion and its legacy projection in one transaction. It is exactly what
    makes the test meaningful: with the projection gone, anything that still
    answers correctly was not relying on it.
    """
    with sqlite3.connect(store.path) as conn:
        cursor = conn.execute("DELETE FROM system_events WHERE event_type = ?", (event_type,))
        return int(cursor.rowcount)


def max_event_id(store: StateStore) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM system_events").fetchone()[0])


def event_count(store: StateStore) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0])


def last_event_type(store: StateStore) -> str:
    with sqlite3.connect(store.path) as conn:
        return str(
            conn.execute(
                "SELECT event_type FROM system_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
