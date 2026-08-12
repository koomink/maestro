import fcntl
import json
import multiprocessing
import os
import sqlite3
import threading
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


def _hold_live_order_lock(db_path, hold_seconds, ready, done):
    store = StateStore(db_path, 0)
    with store.live_order_lock("live_order_holder"):
        ready.set()
        time.sleep(hold_seconds)
    done.set()


def _hold_writer_then_block_on_live_order(db_path, ready, go, done):
    """Half of a genuine cycle: holds the writer lock, then blocks on live_order."""
    store = StateStore(db_path, 0)
    with store.writer_lock("cycle_writer_holder"):
        ready.set()
        go.wait(timeout=10)
        try:
            with store.live_order_lock("cycle_writer_holder", timeout_seconds=6.0):
                pass
        except TimeoutError:
            pass
    done.set()


def _hold_live_order_then_block_on_writer(db_path, ready, go, done):
    """The other half: holds live_order, then blocks on the writer lock."""
    store = StateStore(db_path, 0)
    with store.live_order_lock("cycle_live_holder"):
        ready.set()
        go.wait(timeout=10)
        try:
            with store.writer_lock("cycle_live_holder", timeout_seconds=6.0):
                pass
        except TimeoutError:
            pass
    done.set()


def _fail_diagnostic_record_io(
    monkeypatch,
    store,
    *,
    fail_write_from_call=None,
    fail_ftruncate_from_call=None,
):
    """Make the unbuffered diagnostic record I/O raise ENOSPC from a given
    1-based call count onward, for the writer lock file's fd only.

    The fault is injected at os.write/os.ftruncate -- the calls the helpers now
    use -- and never touches flock. Restricting it to the lock file's own fd
    matters: pytest's output capture goes through os.write too.
    """
    real_open = Path.open
    real_write = os.write
    real_ftruncate = os.ftruncate
    target_fds = set()
    calls = {"write": 0, "ftruncate": 0}

    def fake_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == store.writer_lock_path:
            target_fds.add(handle.fileno())
        return handle

    def fake_write(fd, data):
        if fd in target_fds:
            calls["write"] += 1
            if fail_write_from_call is not None and calls["write"] >= fail_write_from_call:
                raise OSError(28, "No space left on device")
        return real_write(fd, data)

    def fake_ftruncate(fd, length):
        if fd in target_fds:
            calls["ftruncate"] += 1
            if (
                fail_ftruncate_from_call is not None
                and calls["ftruncate"] >= fail_ftruncate_from_call
            ):
                raise OSError(28, "No space left on device")
        return real_ftruncate(fd, length)

    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(os, "write", fake_write)
    monkeypatch.setattr(os, "ftruncate", fake_ftruncate)
    return calls


class _DirtyBufferLockFile:
    """Mimics a buffered text stream whose diagnostic write left the buffer dirty.

    The real failure this stands in for: on a full disk ``write()`` only fills
    the Python-level buffer, ``flush()`` raises ENOSPC (and is swallowed inside
    the diagnostic helper), the dirty buffer survives, and the *close* performed
    when the lock file goes out of scope -- after flock is released and outside
    every guard -- flushes again and raises ``OSError`` at the caller.

    ``close()`` still closes the real handle before raising, exactly as
    CPython's ``TextIOWrapper.close()`` does when its flush fails.
    """

    def __init__(self, real):
        self._real = real
        self.close_calls = 0

    def write(self, data):
        return len(data)  # accepted into the "buffer", never reaches the disk

    def flush(self):
        raise OSError(28, "No space left on device")

    def close(self):
        self.close_calls += 1
        self._real.close()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _patch_lock_file_with(monkeypatch, store, factory):
    """Make every ``Path.open`` of the writer lock file return ``factory(handle)``."""
    real_open = Path.open
    made = []

    def fake_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == store.writer_lock_path:
            wrapper = factory(handle)
            made.append(wrapper)
            return wrapper
        return handle

    monkeypatch.setattr(Path, "open", fake_open)
    return made


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


def test_live_order_lock_is_reentrant_in_the_same_thread(tmp_path):
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
    calls = _fail_diagnostic_record_io(monkeypatch, store, fail_write_from_call=1)

    with store.writer_lock("victim"):
        pass  # must not raise even though the diagnostic write fails

    assert calls["write"] >= 1  # the fault really was injected
    monkeypatch.undo()

    # The flock must be released cleanly and the depth counter balanced --
    # reacquiring proves both (a leaked flock or a stuck depth would hang or
    # skip acquisition here).
    with store.writer_lock("victim_again"):
        pass


def test_clear_lock_holder_failure_preserves_the_original_exception(tmp_path, monkeypatch):
    """A disk fault while clearing the holder record must not mask the caller's error."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    # Call 1 is the acquire-time _write_lock_holder ftruncate; call 2 onward is
    # the release-time _clear_lock_holder ftruncate -- only the latter should
    # fail, isolating this test to _clear_lock_holder.
    calls = _fail_diagnostic_record_io(monkeypatch, store, fail_ftruncate_from_call=2)

    with pytest.raises(RuntimeError, match="^original business error$"):
        with store.writer_lock("victim"):
            raise RuntimeError("original business error")

    assert calls["ftruncate"] >= 2  # the release-time fault really was injected
    monkeypatch.undo()

    # The lock must still be released cleanly despite the diagnostic-clear failure.
    with store.writer_lock("victim_again"):
        pass


def test_lock_file_close_failure_does_not_escape_the_lock(tmp_path, monkeypatch):
    """H1: a close-time flush of a dirty diagnostic buffer must not raise at the caller.

    The lock must still be acquired (exclusively), still be released, and the
    ``with`` block must return normally even though flush/close both raise.
    """
    store = StateStore(str(tmp_path / "state.db"), 0)
    wrappers = _patch_lock_file_with(monkeypatch, store, _DirtyBufferLockFile)

    body_ran = False
    with store.writer_lock("victim"):
        body_ran = True
        # The flock must really be held while the diagnostic write is failing.
        with open(store.writer_lock_path, "a+", encoding="utf-8") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    assert body_ran
    assert wrappers and wrappers[0].close_calls == 1  # the close really was attempted

    # The flock must have been released: with the fault removed, reacquiring
    # must succeed (a leaked flock or an unbalanced depth counter would not).
    monkeypatch.undo()
    with store.writer_lock("victim_again"):
        pass


def test_lock_file_close_failure_preserves_the_original_exception(tmp_path, monkeypatch):
    """H1: the caller's trading error must reach the caller with its own type and
    message -- callers catch (RuntimeError, TimeoutError, ValueError), never OSError."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    _patch_lock_file_with(monkeypatch, store, _DirtyBufferLockFile)

    with pytest.raises(RuntimeError, match="^original business error$"):
        with store.writer_lock("victim"):
            raise RuntimeError("original business error")

    monkeypatch.undo()
    with store.writer_lock("victim_again"):
        pass


def test_writer_timeout_message_names_the_live_order_holder_and_the_waiter(tmp_path):
    """Hypothesis A vs B: when the writer lock times out, show who holds the
    live_order_lock too, so a single alert can tell a lock-order inversion
    (holder blocked on live_order_lock) from a merely long writer critical
    section."""
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    writer_ready = multiprocessing.Event()
    writer_done = multiprocessing.Event()
    writer_proc = multiprocessing.Process(
        target=_hold_writer_lock, args=(db, 3.0, writer_ready, writer_done)
    )
    live_ready = multiprocessing.Event()
    live_done = multiprocessing.Event()
    live_proc = multiprocessing.Process(
        target=_hold_live_order_lock, args=(db, 3.0, live_ready, live_done)
    )
    writer_proc.start()
    live_proc.start()
    try:
        assert writer_ready.wait(timeout=10)
        assert live_ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.3):
                pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message  # existing prefix preserved
        assert "waiter victim" in message  # who was denied
        assert str(writer_proc.pid) in message  # the writer lock's holder
        assert "live_order_lock" in message
        assert str(live_proc.pid) in message  # the other lock's holder
        assert "waited" in message
    finally:
        writer_proc.join(timeout=10)
        live_proc.join(timeout=10)


def test_timeout_message_reports_a_genuine_wait_for_cycle(tmp_path):
    """M1 (i): X holds the writer lock and blocks on live_order while Y holds
    live_order and blocks on the writer lock -- a real lock-order inversion
    (hypothesis A). The timeout message must name it as a cycle."""
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    writer_ready, live_ready = multiprocessing.Event(), multiprocessing.Event()
    writer_done, live_done = multiprocessing.Event(), multiprocessing.Event()
    go = multiprocessing.Event()
    writer_proc = multiprocessing.Process(
        target=_hold_writer_then_block_on_live_order,
        args=(db, writer_ready, go, writer_done),
    )
    live_proc = multiprocessing.Process(
        target=_hold_live_order_then_block_on_writer,
        args=(db, live_ready, go, live_done),
    )
    writer_proc.start()
    live_proc.start()
    try:
        assert writer_ready.wait(timeout=10)
        assert live_ready.wait(timeout=10)
        go.set()
        time.sleep(0.5)  # let both processes register their wait-for edges
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.5):
                pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message  # existing prefix preserved
        # The live_order_lock holder is itself waiting for the writer lock.
        assert "WAIT-FOR CYCLE" in message
        assert str(live_proc.pid) in message
        assert str(writer_proc.pid) in message
    finally:
        go.set()
        writer_proc.join(timeout=15)
        live_proc.join(timeout=15)


def test_timeout_message_does_not_claim_a_cycle_for_independent_holders(tmp_path):
    """M1 (ii): two independent holders, neither waiting on the other, must not
    be reported as a cycle -- otherwise the diagnostic cannot separate
    hypothesis A from a merely long critical section."""
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    writer_ready, writer_done = multiprocessing.Event(), multiprocessing.Event()
    live_ready, live_done = multiprocessing.Event(), multiprocessing.Event()
    writer_proc = multiprocessing.Process(
        target=_hold_writer_lock, args=(db, 3.0, writer_ready, writer_done)
    )
    live_proc = multiprocessing.Process(
        target=_hold_live_order_lock, args=(db, 3.0, live_ready, live_done)
    )
    writer_proc.start()
    live_proc.start()
    try:
        assert writer_ready.wait(timeout=10)
        assert live_ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.5):
                pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message
        assert "WAIT-FOR CYCLE" not in message
        assert "no wait-for cycle" in message
        assert str(live_proc.pid) in message  # the other lock's holder is still named
    finally:
        writer_proc.join(timeout=15)
        live_proc.join(timeout=15)


def test_timeout_message_carries_hold_and_pressure_signals(tmp_path):
    """Hypothesis B vs C: the message must show how long the holder has held the
    lock next to how long the waiter waited, plus a cheap host-pressure read."""
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 3.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.5):
                pass
        message = str(exc_info.value)
        assert "held " in message  # how long the holder has held it
        assert "waited " in message  # how long this waiter waited
        assert "load1=" in message  # /proc-derived host pressure
        assert "mem_avail=" in message
    finally:
        proc.join(timeout=10)


def test_waiter_record_is_removed_once_the_lock_is_acquired(tmp_path):
    """A wait-for edge must not survive acquisition, or every slow-but-healthy
    acquisition would later read as a cycle."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with store.writer_lock("holder"):
            ready.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        waiter_path = store.waiter_record_path(store.writer_lock_path, os.getpid())

        def wait_for_the_lock():
            with store.writer_lock("waiter", timeout_seconds=3.0):
                pass

        waiter_thread = threading.Thread(target=wait_for_the_lock)
        waiter_thread.start()
        time.sleep(0.4)
        assert waiter_path.exists()  # registered while blocking
        record = store.read_waiter_record(store.writer_lock_path, os.getpid())
        assert record is not None
        assert record["owner"] == "waiter"
        release.set()
        waiter_thread.join(timeout=5)
        assert not waiter_path.exists()  # deregistered on acquire
    finally:
        release.set()
        thread.join(timeout=5)


def test_waiter_record_of_a_dead_process_is_ignored(tmp_path):
    """A SIGKILLed waiter leaves its file behind; a stale edge must not be
    reported as a live cycle."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    dead_pid = 2**22 - 1  # above every plausible pid_max, so certainly not running
    path = store.waiter_record_path(store.writer_lock_path, dead_pid)
    path.write_text(
        json.dumps({"owner": "ghost", "pid": dead_pid, "since": "2026-08-12T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert store.read_waiter_record(store.writer_lock_path, dead_pid) is None


def test_waiter_record_failures_never_affect_the_lock(tmp_path, monkeypatch):
    """Waiter bookkeeping is best-effort: a write/unlink fault must not change
    acquisition, release, or the exception raised."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    real_os_open, real_unlink = os.open, os.unlink

    def fake_os_open(path, *args, **kwargs):
        if ".waiter." in str(path):
            raise OSError(28, "No space left on device")
        return real_os_open(path, *args, **kwargs)

    def fake_unlink(path, *args, **kwargs):
        if ".waiter." in str(path):
            raise OSError(5, "I/O error")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_os_open)
    monkeypatch.setattr(os, "unlink", fake_unlink)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with store.writer_lock("holder"):
            ready.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        # Timing out must still raise TimeoutError, not the injected OSError.
        with pytest.raises(TimeoutError, match="State writer lock is busy"):
            with store.writer_lock("victim", timeout_seconds=0.4):
                pass
    finally:
        release.set()
        thread.join(timeout=5)
    # And an uncontended acquisition must still work.
    with store.writer_lock("after"):
        pass


def test_account_refresh_lock_records_its_holder(tmp_path):
    """depth_attr=None is the only path through _write_lock_holder; cover it."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    lock_path = store.path.with_suffix(store.path.suffix + ".refresh-kis_ps.lock")
    with store.account_refresh_lock("kis_ps"):
        holder = store.read_lock_holder(lock_path)
        assert holder is not None
        assert holder["owner"] == "account_refresh:kis_ps"


def test_lock_file_is_cleared_after_the_guarded_body_raises(tmp_path):
    """The finally path must clear the holder record even when the body raises --
    the same path F1 hardens against a diagnostic-write failure."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    with pytest.raises(ValueError, match="boom"):
        with store.writer_lock("approve_signal"):
            raise ValueError("boom")
    assert store.read_lock_holder(store.writer_lock_path) is None
