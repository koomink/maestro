import multiprocessing
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from maestro.state.store import StateStore


def _hold_writer_lock(db_path, hold_seconds, ready, done):
    store = StateStore(db_path, 0)
    with store.writer_lock("holder"):
        ready.set()
        time.sleep(hold_seconds)
    done.set()


def test_list_system_events_by_type_filters_by_since(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_system_event("run_1", "telegram_approval_ack", {"approval_id": "old"})
    store.save_system_event("run_2", "telegram_approval_ack", {"approval_id": "new"})
    # 첫 이벤트만 과거로 밀어 넣는다
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        conn.execute(
            "UPDATE system_events SET created_at = ? "
            "WHERE json_extract(payload, '$.approval_id') = 'old'",
            ((datetime.now(UTC) - timedelta(days=200)).isoformat(sep=" "),),
        )

    rows = store.list_system_events_by_type(
        "telegram_approval_ack",
        limit=None,
        since=datetime.now(UTC) - timedelta(days=90),
    )

    assert [row["payload"]["approval_id"] for row in rows] == ["new"]


def test_list_system_events_by_type_includes_boundary_second(tmp_path):
    """
    Verify that a row whose created_at equals since (to the second) is included.
    This tests the format boundary: created_at is stored as YYYY-MM-DD HH:MM:SS
    (no microseconds), so since must be formatted the same way to match via >=.
    """
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)

    # Save the first event through the normal path to get real SQLite format
    store.save_system_event("run_boundary", "telegram_approval_ack", {"approval_id": "boundary"})

    # Fetch the row to get its exact created_at timestamp in SQLite format
    rows = store.list_system_events_by_type("telegram_approval_ack", limit=None)
    assert len(rows) == 1
    created_at_str = rows[0]["created_at"]

    # Parse it back to datetime (SQLite format: YYYY-MM-DD HH:MM:SS)
    boundary_dt = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    # Query with since set to exactly the boundary second (no microseconds)
    # The row should be included because created_at >= boundary_dt
    result = store.list_system_events_by_type(
        "telegram_approval_ack",
        limit=None,
        since=boundary_dt,
    )

    assert len(result) == 1
    assert result[0]["payload"]["approval_id"] == "boundary"


def test_writer_lock_is_exclusive_across_processes(tmp_path):
    db = str(tmp_path / "state.db")
    StateStore(db, 0)  # create schema
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 2.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError, match="State writer lock is busy"):
            with store.writer_lock("waiter", timeout_seconds=0.3):
                pass
    finally:
        proc.join(timeout=10)


def test_writer_lock_is_reentrant_in_the_same_thread(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("outer"):
        with store.writer_lock("inner", timeout_seconds=0.1):
            pass  # a TimeoutError here would mean reentrancy is broken


def test_live_order_lock_is_exclusive_and_reentrant(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.live_order_lock("outer"):
        with store.live_order_lock("inner", timeout_seconds=0.1):
            pass


def test_account_refresh_lock_rejects_a_second_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.account_refresh_lock("kis_ps"):
        with pytest.raises(TimeoutError, match="Account refresh is already running"):
            with store.account_refresh_lock("kis_ps"):
                pass


def test_lock_file_records_the_holder_while_held(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("approve_signal"):
        holder = store.read_lock_holder(store.writer_lock_path)
        assert holder is not None
        assert holder["owner"] == "approve_signal"
        assert holder["pid"] == os.getpid()
        assert holder["acquired_at"]  # ISO 문자열


def test_lock_file_is_cleared_after_release(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("approve_signal"):
        pass
    assert store.read_lock_holder(store.writer_lock_path) is None


def test_reentrant_acquisition_keeps_the_outer_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("outer"):
        with store.writer_lock("inner"):
            holder = store.read_lock_holder(store.writer_lock_path)
            assert holder["owner"] == "outer"


def test_read_lock_holder_tolerates_a_corrupt_file(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    store.writer_lock_path.write_text("깨진 내용 not json", encoding="utf-8")
    assert store.read_lock_holder(store.writer_lock_path) is None


def test_live_order_lock_records_its_own_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.live_order_lock("resolve_pending_signal_approval"):
        holder = store.read_lock_holder(store.live_order_lock_path)
        assert holder["owner"] == "resolve_pending_signal_approval"
