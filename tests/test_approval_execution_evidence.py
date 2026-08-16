"""Gathering one approval's execution evidence out of the event log.

The awkward part this pins down: intent and result events do **not** carry
`approval_id` at the top level, so the generated `approval_id` column is
empty for them. It lives at `$.request.approval_id`. Reading the column
instead of the path silently returns nothing, which classifies every order
as "never sent" -- the exact wrong answer, and a dangerous one, because
"never sent" is the only outcome that makes a re-order look safe.
"""

import sqlite3

from maestro.ops.batch_execution import build_order_evidence, summarize_batch
from maestro.state.store import StateStore

APPROVAL_ID = "appr_1589437a40424cd7a4e7141dbdf96e17"
SIGNAL_RUN_ID = "signal_8f6df2d748a64c08aaa34456151f3923"


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.db"))


def _set_watermark(tmp_path, broker_order_id: str, quantity: float) -> None:
    # The store has no single-watermark setter; reconciliation writes them in
    # bulk. Writing the row directly keeps this fixture to the one fact the
    # classification reads.
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        conn.execute(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional) VALUES (?, ?, 0.0) "
            "ON CONFLICT(broker_order_id) DO UPDATE SET "
            "cumulative_quantity = excluded.cumulative_quantity",
            (broker_order_id, quantity),
        )


def _proposed(order_id: str, symbol: str, side: str, quantity: float) -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": 1.0,
        "order_type": "limit",
        "account_id": "toss_brokerage",
    }


def _save_envelope(store: StateStore, orders: list[dict]) -> None:
    store.save_system_event(
        "run-1",
        "telegram_approval_pending",
        {
            "approval_id": APPROVAL_ID,
            "run_id": "run-1",
            "signal_run_id": SIGNAL_RUN_ID,
            "request": {
                "approval_id": APPROVAL_ID,
                "order_count": len(orders),
                "proposed_orders": orders,
            },
        },
    )


def _save_intent(store: StateStore, order_id: str, symbol: str, quantity: float) -> None:
    store.save_system_event(
        "run-1",
        "live_order_submit_intent",
        {
            "signal_run_id": SIGNAL_RUN_ID,
            # approval_id lives inside `request`, not at the top level.
            "request": {
                "order_id": order_id,
                "symbol": symbol,
                "side": "sell",
                "quantity": quantity,
                "approval_id": APPROVAL_ID,
            },
            "phase": "submit_intent",
        },
    )


def _save_result(
    store: StateStore,
    order_id: str,
    symbol: str,
    quantity: float,
    broker_order_id: str,
) -> None:
    store.save_system_event(
        "run-1",
        "live_order_result",
        {
            "signal_run_id": SIGNAL_RUN_ID,
            "request": {
                "order_id": order_id,
                "symbol": symbol,
                "side": "sell",
                "quantity": quantity,
                "approval_id": APPROVAL_ID,
            },
            "result": {
                "order_id": order_id,
                "status": "accepted_by_broker",
                "broker_order": {"broker_order_id": broker_order_id, "order_id": order_id},
                "filled_quantity": 0.0,
            },
        },
    )


def _incident_store(tmp_path) -> StateStore:
    """The 2026-08-11 crescendo batch, rebuilt from the shape in the live DB."""
    store = _store(tmp_path)
    _save_envelope(
        store,
        [
            _proposed("ord_pdbc", "PDBC", "buy", 366.0),
            _proposed("ord_spy", "SPY", "buy", 6.0),
            _proposed("ord_sso", "SSO", "sell", 20.0),
            _proposed("ord_bil", "BIL", "buy", 15.0),
            _proposed("ord_tip", "TIP", "sell", 23.0),
        ],
    )
    # Only the two sells reached the broker before the writer lock timed out.
    _save_intent(store, "ord_sso", "SSO", 20.0)
    _save_result(store, "ord_sso", "SSO", 20.0, "brk_sso")
    _save_intent(store, "ord_tip", "TIP", 23.0)
    _save_result(store, "ord_tip", "TIP", 23.0, "brk_tip")
    # TIP filled in full; SSO was cancelled unfilled the next day.
    _set_watermark(tmp_path, "brk_tip", 23.0)
    _set_watermark(tmp_path, "brk_sso", 0.0)
    store.save_system_event(
        "run-1",
        "live_order_tracking_resolved",
        {"order_id": "ord_sso", "broker_order_id": "brk_sso", "final_status": "canceled"},
    )
    return store


def test_an_approval_with_no_events_has_no_evidence(tmp_path):
    store = _store(tmp_path)
    evidence = store.load_approval_execution_evidence("appr_missing")
    assert evidence["envelope"] is None
    assert evidence["intents"] == {}
    assert evidence["results"] == {}


def test_intents_are_found_through_the_nested_request_approval_id(tmp_path):
    store = _incident_store(tmp_path)
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    assert set(evidence["intents"]) == {"ord_sso", "ord_tip"}
    assert set(evidence["results"]) == {"ord_sso", "ord_tip"}


def test_another_approvals_orders_are_not_picked_up(tmp_path):
    store = _incident_store(tmp_path)
    store.save_system_event(
        "run-2",
        "live_order_submit_intent",
        {"request": {"order_id": "ord_other", "approval_id": "appr_other"}},
    )
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    assert "ord_other" not in evidence["intents"]


def test_the_reconciled_fill_watermark_is_attached_to_the_order(tmp_path):
    store = _incident_store(tmp_path)
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    assert evidence["fills"]["brk_tip"] == 23.0
    assert evidence["fills"]["brk_sso"] == 0.0


def test_the_terminal_status_of_a_tracked_order_is_attached(tmp_path):
    store = _incident_store(tmp_path)
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    assert evidence["final_statuses"]["ord_sso"] == "canceled"


def test_the_2026_08_11_batch_classifies_into_the_five_recorded_outcomes(tmp_path):
    store = _incident_store(tmp_path)
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    batch = summarize_batch(APPROVAL_ID, build_order_evidence(evidence))

    by_symbol = {line.symbol: line for line in batch.orders}
    assert by_symbol["PDBC"].outcome == "not_sent"
    assert by_symbol["SPY"].outcome == "not_sent"
    assert by_symbol["BIL"].outcome == "not_sent"
    assert by_symbol["SSO"].outcome == "cancelled_unfilled"
    assert by_symbol["TIP"].outcome == "filled"
    assert by_symbol["TIP"].filled_quantity == 23.0
    assert batch.counts == {"not_sent": 3, "cancelled_unfilled": 1, "filled": 1}
    assert batch.has_unknown is False


def test_an_order_whose_result_never_landed_is_unknown_not_missing(tmp_path):
    store = _store(tmp_path)
    _save_envelope(store, [_proposed("ord_sso", "SSO", "sell", 20.0)])
    _save_intent(store, "ord_sso", "SSO", 20.0)

    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    batch = summarize_batch(APPROVAL_ID, build_order_evidence(evidence))
    assert [line.outcome for line in batch.orders] == ["unknown"]
    assert batch.has_unknown is True


def test_the_roster_comes_from_the_envelope_so_unsent_orders_still_appear(tmp_path):
    # An order that was never sent has no event of its own. If the roster
    # came from the events, it would vanish from the batch entirely.
    store = _incident_store(tmp_path)
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    batch = summarize_batch(APPROVAL_ID, build_order_evidence(evidence))
    assert len(batch.orders) == 5


def test_the_ack_and_its_schema_version_are_available_for_the_settle_gate(tmp_path):
    store = _incident_store(tmp_path)
    store.save_system_event(
        "run-1",
        "telegram_approval_ack",
        {"approval_id": APPROVAL_ID, "status": "approved", "schema_version": 2},
    )
    evidence = store.load_approval_execution_evidence(APPROVAL_ID)
    assert evidence["ack"]["schema_version"] == 2
    assert evidence["resolution_completed"] is None
