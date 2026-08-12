import json
import multiprocessing
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maestro.state.store import StateStore


def _hold_writer_lock(db_path, hold_seconds, ready, done):
    store = StateStore(db_path, 0)
    with store.writer_lock("holder"):
        ready.set()
        time.sleep(hold_seconds)
    done.set()


class _DiagnosticWriteFailingFile:
    """Wraps a real lock file, making write()/truncate() raise OSError from a
    given 1-based call count onward, without touching the flock itself.

    Used to simulate a full disk (ENOSPC) hitting only the lock-holder
    diagnostic write/clear helpers -- not the flock acquire/release path.
    """

    def __init__(self, real, *, fail_write_from_call=None, fail_truncate_from_call=None):
        self._real = real
        self._fail_write_from_call = fail_write_from_call
        self._fail_truncate_from_call = fail_truncate_from_call
        self._write_calls = 0
        self._truncate_calls = 0

    def write(self, data):
        self._write_calls += 1
        if (
            self._fail_write_from_call is not None
            and self._write_calls >= self._fail_write_from_call
        ):
            raise OSError(28, "No space left on device")
        return self._real.write(data)

    def truncate(self, *args):
        self._truncate_calls += 1
        if (
            self._fail_truncate_from_call is not None
            and self._truncate_calls >= self._fail_truncate_from_call
        ):
            raise OSError(28, "No space left on device")
        return self._real.truncate(*args)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


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


def test_timeout_message_names_the_holder(tmp_path):
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 3.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.3):
                pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message  # existing prefix preserved
        assert "holder" in message
        assert str(proc.pid) in message  # the actual holder's PID
        assert "waited" in message
    finally:
        proc.join(timeout=10)


def test_timeout_message_says_unknown_when_the_record_is_missing(tmp_path, monkeypatch):
    """Even without a record, the exception type and prefix stay the same."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    monkeypatch.setattr(StateStore, "_write_lock_holder", staticmethod(lambda *a, **k: None))
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    # Contend with another thread in the same process -- _lock_depths is
    # thread-local, so this actually blocks.
    import threading

    def hold():
        with store.writer_lock("holder"):
            ready.set()
            done.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.3):
                pass
        assert "State writer lock is busy" in str(exc_info.value)
        assert "unknown" in str(exc_info.value)
    finally:
        done.set()
        thread.join(timeout=5)


def test_read_lock_holder_tolerates_truncated_multibyte_utf8(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    record = {"owner": "보유자", "pid": 123, "acquired_at": "2026-08-12T00:00:00+00:00"}
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    # Cut one byte into the first multi-byte character so the tail is an
    # incomplete UTF-8 sequence -- the mid-truncate race the brief describes.
    non_ascii_index = next(i for i, byte in enumerate(payload) if byte >= 0x80)
    truncated = payload[: non_ascii_index + 1]
    store.writer_lock_path.write_bytes(truncated)
    assert store.read_lock_holder(store.writer_lock_path) is None


def test_write_lock_holder_failure_does_not_prevent_lock_acquisition_or_release(
    tmp_path, monkeypatch
):
    """A disk fault during the diagnostic write must never affect the flock itself."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == store.writer_lock_path:
            return _DiagnosticWriteFailingFile(handle, fail_write_from_call=1)
        return handle

    monkeypatch.setattr(Path, "open", fake_open)

    with store.writer_lock("victim"):
        pass  # must not raise even though the diagnostic write fails

    # The flock must be released cleanly and the depth counter balanced --
    # reacquiring proves both (a leaked flock or a stuck depth would hang or
    # skip acquisition here).
    with store.writer_lock("victim_again"):
        pass


def test_clear_lock_holder_failure_preserves_the_original_exception(tmp_path, monkeypatch):
    """A disk fault while clearing the holder record must not mask the caller's error."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == store.writer_lock_path:
            # Call 1 is the acquire-time _write_lock_holder truncate; call 2
            # onward is the release-time _clear_lock_holder truncate -- only
            # the latter should fail, isolating this test to _clear_lock_holder.
            return _DiagnosticWriteFailingFile(handle, fail_truncate_from_call=2)
        return handle

    monkeypatch.setattr(Path, "open", fake_open)

    with pytest.raises(RuntimeError, match="^original business error$"):
        with store.writer_lock("victim"):
            raise RuntimeError("original business error")

    # The lock must still be released cleanly despite the diagnostic-clear failure.
    with store.writer_lock("victim_again"):
        pass
