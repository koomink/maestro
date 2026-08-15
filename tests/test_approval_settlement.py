"""Closing a half-executed approval on the operator's word, truthfully.

Settlement is the only way to close an approval whose execution stopped part
way, and it is the last word on that approval: preflight, the resume sweep
and the card sweep all read `telegram_approval_resolution_completed` as
terminal. So the bar is that it never closes something it cannot describe,
and never looks like a clean run when it was not one.
"""

import sqlite3

import pytest

from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.batch_execution import SettlementRefused, settle_approval
from maestro.state.store import StateStore

APPROVAL_ID = "appr_1589437a40424cd7a4e7141dbdf96e17"
SIGNAL_RUN_ID = "signal_8f6df2d748a64c08aaa34456151f3923"


def _audit(tmp_path) -> AuditLogger:
    return AuditLogger(str(tmp_path / "audit.jsonl"))


def _proposed(order_id: str, symbol: str, side: str, quantity: float) -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": 1.0,
    }


def _set_watermark(tmp_path, broker_order_id: str, quantity: float) -> None:
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        conn.execute(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional) VALUES (?, ?, 0.0)",
            (broker_order_id, quantity),
        )


def _store(tmp_path, *, schema_version: int | None = 2, orders: list[dict] | None = None):
    store = StateStore(str(tmp_path / "state.db"))
    orders = (
        orders
        if orders is not None
        else [
            _proposed("ord_pdbc", "PDBC", "buy", 366.0),
            _proposed("ord_sso", "SSO", "sell", 20.0),
            _proposed("ord_tip", "TIP", "sell", 23.0),
        ]
    )
    store.save_system_event(
        "run-1",
        "telegram_approval_pending",
        {
            "approval_id": APPROVAL_ID,
            "run_id": "run-1",
            "signal_run_id": SIGNAL_RUN_ID,
            "request": {"approval_id": APPROVAL_ID, "proposed_orders": orders},
        },
    )
    if schema_version is not None:
        store.save_system_event(
            "run-1",
            "telegram_approval_ack",
            {
                "approval_id": APPROVAL_ID,
                "signal_run_id": SIGNAL_RUN_ID,
                "status": "approved",
                "schema_version": schema_version,
            },
        )
    return store


def _submit(store, order_id: str, symbol: str, quantity: float, *, result: bool = True):
    request = {
        "order_id": order_id,
        "symbol": symbol,
        "side": "sell",
        "quantity": quantity,
        "approval_id": APPROVAL_ID,
    }
    store.save_system_event("run-1", "live_order_submit_intent", {"request": request})
    if result:
        store.save_system_event(
            "run-1",
            "live_order_result",
            {
                "request": request,
                "result": {
                    "order_id": order_id,
                    "broker_order": {"broker_order_id": f"brk_{order_id}"},
                    "filled_quantity": 0.0,
                },
            },
        )


def _incident(tmp_path):
    """The 08-11 batch: TIP filled, SSO cancelled unfilled, PDBC never sent."""
    store = _store(tmp_path)
    _submit(store, "ord_sso", "SSO", 20.0)
    _submit(store, "ord_tip", "TIP", 23.0)
    _set_watermark(tmp_path, "brk_ord_tip", 23.0)
    _set_watermark(tmp_path, "brk_ord_sso", 0.0)
    store.save_system_event(
        "run-1",
        "live_order_tracking_resolved",
        {"order_id": "ord_sso", "final_status": "canceled"},
    )
    return store


def _completed(store) -> list[dict]:
    return store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    )


# --- refusals --------------------------------------------------------------


def test_an_approval_with_no_ack_is_not_something_to_settle(tmp_path):
    store = _store(tmp_path, schema_version=None)
    with pytest.raises(SettlementRefused, match="no_ack"):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")


def test_a_pre_stage_3a_ack_is_not_something_to_settle(tmp_path):
    # A schema_version < 2 ack predates two-phase persistence, so no
    # resolution event was ever expected for it and it is already terminal.
    store = _store(tmp_path, schema_version=1)
    with pytest.raises(SettlementRefused, match="legacy_ack"):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")


def test_an_already_closed_approval_is_not_settled_twice(tmp_path):
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="first")

    with pytest.raises(SettlementRefused, match="already_settled"):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="second")

    assert len(_completed(store)) == 1


def test_an_order_that_may_be_at_the_broker_blocks_settlement(tmp_path):
    # Intent recorded, no result: we do not know whether the broker has it.
    # Closing over that would record a settled state we cannot vouch for.
    store = _store(tmp_path)
    _submit(store, "ord_sso", "SSO", 20.0, result=False)

    with pytest.raises(SettlementRefused, match="unknown_orders"):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    assert _completed(store) == []


def test_an_operator_who_checked_the_broker_can_settle_an_unknown_order(tmp_path):
    store = _store(tmp_path)
    _submit(store, "ord_sso", "SSO", 20.0, result=False)

    settle_approval(
        store,
        _audit(tmp_path),
        APPROVAL_ID,
        reason="checked in the app",
        reconciled_with_broker=True,
    )

    payload = _completed(store)[0]["payload"]
    # The override is itself a fact about how this was closed.
    assert payload["reconciled_with_broker"] is True
    assert payload["outcome"]["has_unknown"] is True


def test_settling_without_the_override_leaves_no_trace_of_the_attempt(tmp_path):
    store = _store(tmp_path)
    _submit(store, "ord_sso", "SSO", 20.0, result=False)
    with pytest.raises(SettlementRefused):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")
    assert _completed(store) == []


# --- what a settlement records --------------------------------------------


def test_a_settlement_is_marked_as_the_operators_and_carries_its_reason(tmp_path):
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="handled at next open")

    payload = _completed(store)[0]["payload"]
    assert payload["settled_by"] == "operator"
    assert payload["reason"] == "handled at next open"
    assert payload["approval_id"] == APPROVAL_ID
    assert payload["signal_run_id"] == SIGNAL_RUN_ID


def test_a_settlement_records_what_each_order_actually_did(tmp_path):
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    outcome = _completed(store)[0]["payload"]["outcome"]
    assert outcome["counts"] == {"not_sent": 1, "cancelled_unfilled": 1, "filled": 1}
    by_symbol = {line["symbol"]: line["outcome"] for line in outcome["orders"]}
    assert by_symbol == {"PDBC": "not_sent", "SSO": "cancelled_unfilled", "TIP": "filled"}


def test_a_settlement_does_not_claim_an_execution_attempt(tmp_path):
    """`attempt` is what the deployed resume-notice loop gates on.

    `_deliver_resume_completion_notices` treats any resolution event with
    `attempt >= 2` as a resume that needs a "we handled it" message. Nothing
    was executed here, so writing an attempt count would be both false and
    the trigger for a message telling the operator the opposite.
    """
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    assert "attempt" not in _completed(store)[0]["payload"]


def test_a_settlement_keys_itself_so_a_retry_cannot_double_write(tmp_path):
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    payload = _completed(store)[0]["payload"]
    assert payload["duplicate_key"] == f"telegram-approval-settled:{APPROVAL_ID}"


def test_the_settled_status_comes_from_the_ack_not_from_an_assumption(tmp_path):
    store = _incident(tmp_path)
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")
    assert _completed(store)[0]["payload"]["status"] == "approved"


def test_settlement_closes_the_gap_that_rollback_preflight_reports(tmp_path):
    """Preflight calls an approval unresolved while ack has no completion."""
    store = _incident(tmp_path)

    def _unresolved() -> set[str]:
        acked = {
            str(row["payload"].get("approval_id"))
            for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
            if isinstance(row["payload"].get("schema_version"), int)
        }
        completed = {str(row["payload"].get("approval_id")) for row in _completed(store)}
        return acked - completed

    assert _unresolved() == {APPROVAL_ID}
    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")
    assert _unresolved() == set()
