"""One approval, one terminal resolution -- whichever writer gets there first.

`telegram_approval_resolution_completed` is what rollback preflight, the
resume sweep and the card sweep all read as "this approval is closed". Two
writers can reach it: the resume path once it has executed, and an operator
settlement closing a half-executed batch by hand. They key their rows
differently (`telegram-approval-completed:` vs `telegram-approval-settled:`),
so neither one's duplicate-key guard can see the other, and readers take the
newest row for the approval. A race there does not merely duplicate a record
-- it decides which story about the batch wins, and the losing story may be
the one that says orders went out.
"""

import sqlite3
import threading

import pytest

from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.batch_execution import SettlementRefused, settle_approval
from maestro.state.store import StateStore

APPROVAL_ID = "appr_1589437a40424cd7a4e7141dbdf96e17"
SIGNAL_RUN_ID = "signal_8f6df2d748a64c08aaa34456151f3923"

COMPLETED_KEY = f"telegram-approval-completed:{APPROVAL_ID}"
SETTLED_KEY = f"telegram-approval-settled:{APPROVAL_ID}"


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.db"))


def _audit(tmp_path) -> AuditLogger:
    return AuditLogger(str(tmp_path / "audit.jsonl"))


def _resolutions(store: StateStore) -> list[dict]:
    return store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    )


def _resume_payload(**extra) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "signal_run_id": SIGNAL_RUN_ID,
        "status": "approved",
        "attempt": 2,
        "duplicate_key": COMPLETED_KEY,
        **extra,
    }


def _settlement_payload(**extra) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "signal_run_id": SIGNAL_RUN_ID,
        "status": "approved",
        "settled_by": "operator",
        "duplicate_key": SETTLED_KEY,
        **extra,
    }


# --- the shared terminal transition ---------------------------------------


def test_the_first_resolution_for_an_approval_is_written(tmp_path):
    store = _store(tmp_path)

    payload, created = store.insert_approval_resolution("run-1", _resume_payload())

    assert created is True
    assert payload["duplicate_key"] == COMPLETED_KEY


def test_a_second_resolution_is_refused_even_under_a_different_key(tmp_path):
    # This is the whole point: the two writers key their rows differently, so
    # a duplicate_key guard lets both through. Uniqueness has to be per
    # approval, not per key.
    store = _store(tmp_path)
    store.insert_approval_resolution("run-1", _resume_payload())

    payload, created = store.insert_approval_resolution("run-1", _settlement_payload())

    assert created is False
    assert payload["duplicate_key"] == COMPLETED_KEY
    assert len(_resolutions(store)) == 1


def test_the_writer_that_lost_is_handed_the_resolution_that_won(tmp_path):
    # The loser must continue with what is on record, not with what it was
    # about to write -- otherwise it reports a batch outcome nobody stored.
    store = _store(tmp_path)
    store.insert_approval_resolution("run-1", _settlement_payload(reason="closed by hand"))

    payload, created = store.insert_approval_resolution("run-1", _resume_payload())

    assert created is False
    assert payload["settled_by"] == "operator"
    assert payload["reason"] == "closed by hand"


def test_resolutions_for_different_approvals_do_not_block_each_other(tmp_path):
    store = _store(tmp_path)
    store.insert_approval_resolution("run-1", _resume_payload())

    _, created = store.insert_approval_resolution(
        "run-2",
        {"approval_id": "appr_other", "duplicate_key": "telegram-approval-completed:other"},
    )

    assert created is True


def test_concurrent_writers_settle_on_exactly_one_resolution(tmp_path):
    # writer_lock is an advisory flock: a recovery script or a process on a
    # different release has no reason to hold it, so the race has to be
    # resolved inside one transaction rather than assumed away.
    store = _store(tmp_path)
    barrier = threading.Barrier(8)
    results: list[tuple[dict, bool]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def attempt(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            outcome = store.insert_approval_resolution(
                "run-1",
                {
                    "approval_id": APPROVAL_ID,
                    "attempt": index,
                    "duplicate_key": f"telegram-approval-completed:{APPROVAL_ID}:{index}",
                },
            )
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sum(1 for _, created in results if created) == 1
    assert len(_resolutions(store)) == 1

    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type = ?",
            ("telegram_approval_resolution_completed",),
        ).fetchone()[0]
    assert count == 1


# --- settlement under that transition --------------------------------------


def _settleable(tmp_path) -> StateStore:
    """An approval whose batch stopped part way: one order sent, one not."""
    store = _store(tmp_path)
    store.save_system_event(
        "run-1",
        "telegram_approval_pending",
        {
            "approval_id": APPROVAL_ID,
            "run_id": "run-1",
            "signal_run_id": SIGNAL_RUN_ID,
            "request": {
                "approval_id": APPROVAL_ID,
                "proposed_orders": [
                    {"order_id": "ord_sso", "symbol": "SSO", "side": "sell", "quantity": 20.0},
                    {"order_id": "ord_pdbc", "symbol": "PDBC", "side": "buy", "quantity": 366.0},
                ],
            },
        },
    )
    store.save_system_event(
        "run-1",
        "telegram_approval_ack",
        {
            "approval_id": APPROVAL_ID,
            "signal_run_id": SIGNAL_RUN_ID,
            "status": "approved",
            "schema_version": 2,
        },
    )
    request = {
        "order_id": "ord_sso",
        "symbol": "SSO",
        "side": "sell",
        "quantity": 20.0,
        "approval_id": APPROVAL_ID,
    }
    store.save_system_event("run-1", "live_order_submit_intent", {"request": request})
    store.save_system_event(
        "run-1",
        "live_order_result",
        {
            "request": request,
            "result": {
                "order_id": "ord_sso",
                "broker_order": {"broker_order_id": "brk_ord_sso"},
                "filled_quantity": 20.0,
            },
        },
    )
    return store


def test_a_resume_that_lands_mid_settlement_does_not_add_a_second_closure(tmp_path, monkeypatch):
    """The window Codex named: settlement decides, then a resume completes.

    Settlement reads the evidence, finds no resolution, and goes on to write
    one. If a resume completes in between under its own key, both rows land
    and the reader takes whichever is newer.
    """
    store = _settleable(tmp_path)
    original = store.load_approval_execution_evidence
    fired: list[bool] = []

    def racing(approval_id: str):
        evidence = original(approval_id)
        if not fired:
            fired.append(True)
            store.insert_approval_resolution(
                "run-1", _resume_payload(orders_submitted=2, orders_failed=0)
            )
        return evidence

    monkeypatch.setattr(store, "load_approval_execution_evidence", racing)

    with pytest.raises(SettlementRefused, match="already_settled"):
        settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="closing by hand")

    resolutions = _resolutions(store)
    assert len(resolutions) == 1
    # The resume's account of the batch is the one that survives: it is the
    # one that reflects orders actually placed.
    assert resolutions[0]["payload"]["orders_submitted"] == 2


def test_settlement_reads_the_evidence_inside_its_critical_section(tmp_path, monkeypatch):
    """Evidence read outside the lock is evidence that can be stale by the
    time it is written down as the batch's final word."""
    store = _settleable(tmp_path)
    original = store.load_approval_execution_evidence
    held: list[bool] = []

    def observing(approval_id: str):
        held.append(store.holds_writer_lock())
        return original(approval_id)

    monkeypatch.setattr(store, "load_approval_execution_evidence", observing)

    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    assert held == [True]


def test_settlement_holds_the_live_order_lock_while_it_decides(tmp_path, monkeypatch):
    """A resume executing this same approval submits orders under
    live_order_lock. Settlement that does not hold it can read the evidence
    while those orders are going out and then record "not_sent" as the
    batch's final word."""
    store = _settleable(tmp_path)
    original = store.load_approval_execution_evidence
    refused: list[BaseException] = []

    def observing(approval_id: str):
        def compete() -> None:
            try:
                # A separate thread means a separate fd, so this contends for
                # the flock for real rather than re-entering it.
                with store.live_order_lock("competing_execution", timeout_seconds=0.3):
                    pass
            except BaseException as exc:  # noqa: BLE001 - asserted below
                refused.append(exc)

        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(timeout=10)
        return original(approval_id)

    monkeypatch.setattr(store, "load_approval_execution_evidence", observing)

    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="r")

    assert len(refused) == 1
    assert isinstance(refused[0], TimeoutError)


def test_a_settlement_still_records_its_own_outcome_when_it_wins(tmp_path):
    # Guard against fixing the race by hollowing out what settlement writes.
    store = _settleable(tmp_path)

    settle_approval(store, _audit(tmp_path), APPROVAL_ID, reason="handled at next open")

    payload = _resolutions(store)[0]["payload"]
    assert payload["settled_by"] == "operator"
    assert payload["reason"] == "handled at next open"
    assert payload["duplicate_key"] == SETTLED_KEY
    by_symbol = {line["symbol"]: line["outcome"] for line in payload["outcome"]["orders"]}
    assert by_symbol == {"SSO": "filled", "PDBC": "not_sent"}
