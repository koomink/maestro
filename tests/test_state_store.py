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


def _race_workflow_head(db_path, attempt, ready, results):
    from maestro.state.store import StateStore

    store = StateStore(db_path)
    ready.wait(5.0)
    outcome = store.save_system_events_atomic(
        f"run-{attempt}",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {"duplicate_key": "head:wf:v2", "attempt": attempt},
            },
            {
                "event_type": "funding_request",
                "payload": {"duplicate_key": f"req:{attempt}"},
            },
        ],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    results.append((attempt, outcome["committed"], outcome["conflict"]))


def _hold_live_order_lock(db_path, hold_seconds, ready, done):
    store = StateStore(db_path, 0)
    with store.live_order_lock("live_order_holder"):
        ready.set()
        time.sleep(hold_seconds)
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


def test_timeout_message_names_both_holders_without_a_cycle_claim(tmp_path):
    """Two independent holders -- neither waiting on the other -- must never be
    described with any cycle wording. Cross-process wait-for inference was
    removed entirely: the message states facts (both holders, hold/wait time,
    host pressure) and leaves interpretation to a human."""
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
        assert "cycle" not in message.lower()
        assert str(writer_proc.pid) in message  # the writer lock's holder
        assert str(live_proc.pid) in message  # the other lock's holder is still named
    finally:
        writer_proc.join(timeout=15)
        live_proc.join(timeout=15)


def test_timeout_message_for_fill_reconciliation_pattern_makes_no_cycle_claim(tmp_path):
    """fill_reconciliation legitimately holds the writer lock while waiting on the
    live-order lock -- a one-directional wait, not a deadlock. Mirrored here as
    the writer lock held by another process while this process already holds
    the live-order lock and is waiting on the writer lock: exactly the pattern
    the removed cycle detector used to mislabel as WAIT-FOR CYCLE."""
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 2.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with store.live_order_lock("victim"):
            with pytest.raises(TimeoutError) as exc_info:
                with store.writer_lock("victim", timeout_seconds=0.3):
                    pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message  # existing prefix preserved
        assert str(proc.pid) in message  # the writer lock's actual holder
        assert "cycle" not in message.lower()
    finally:
        proc.join(timeout=10)


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


def test_atomic_events_commit_together(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    result = store.save_system_events_atomic(
        "run-1",
        [
            {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
        ],
    )
    assert result["committed"] is True
    assert result["conflict"] is None
    assert result["conflicting_keys"] == ()
    assert store.duplicate_key_exists("req:1")
    assert store.duplicate_key_exists("head:wf:v1")


def test_atomic_events_replay_is_an_idempotent_no_op(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    events = [
        {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
        {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
    ]
    store.save_system_events_atomic("run-1", events)
    result = store.save_system_events_atomic("run-1", events)
    assert result["committed"] is False
    assert result["conflict"] == "already_committed"
    assert result["conflicting_keys"] == ("head:wf:v1", "req:1")
    assert len(store.list_system_events_by_type("funding_request", limit=None)) == 1


def test_atomic_events_reject_a_partial_overlap(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}}],
    )
    with pytest.raises(ValueError, match="partial"):
        store.save_system_events_atomic(
            "run-2",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
            ],
        )
    assert not store.duplicate_key_exists("head:wf:v1")


def test_atomic_events_require_a_duplicate_key_on_every_event(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="duplicate key"):
        store.save_system_events_atomic(
            "run-1",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_workflow_head", "payload": {}},
            ],
        )
    assert not store.duplicate_key_exists("req:1")


def test_atomic_events_reject_duplicate_keys_within_one_batch(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="unique"):
        store.save_system_events_atomic(
            "run-1",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
            ],
        )


def test_atomic_events_reject_an_empty_batch(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="at least one"):
        store.save_system_events_atomic("run-1", [])


def test_atomic_events_carry_broker_and_order_ids(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [
            {
                "event_type": "live_order_submit_intent",
                "payload": {
                    "duplicate_key": "intent:o-1",
                    "order_id": "o-1",
                    "result": {"broker_order": {"broker_order_id": "b-1"}},
                },
            }
        ],
    )
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        row = conn.execute(
            "SELECT broker_order_id, order_id FROM system_events WHERE duplicate_key = ?",
            ("intent:o-1",),
        ).fetchone()
    assert row == ("b-1", "o-1")


def test_atomic_events_leave_nothing_behind_when_one_event_fails(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    unserializable = {"duplicate_key": "req:2"}
    unserializable["self"] = unserializable
    with pytest.raises(ValueError):
        store.save_system_events_atomic(
            "run-1",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_workflow_head", "payload": unserializable},
            ],
        )
    assert not store.duplicate_key_exists("req:1")


def test_atomic_events_normalize_malformed_events_to_value_error(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError):
        store.save_system_events_atomic("run-1", [{"event_type": "a"}])
    with pytest.raises(ValueError):
        store.save_system_events_atomic(
            "run-1", [{"event_type": "a", "payload": None}]
        )


def test_atomic_events_require_an_existing_key(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    result = store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_missing"
    assert result["conflicting_keys"] == ("head:wf:v1",)
    assert not store.duplicate_key_exists("claim:1")


def test_atomic_events_commit_when_the_required_key_is_present(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}}],
    )
    result = store.save_system_events_atomic(
        "run-2",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    assert result["committed"] is True
    assert store.duplicate_key_exists("claim:1")


def test_atomic_events_refuse_when_a_forbidden_key_appeared(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}},
        ],
    )
    result = store.save_system_events_atomic(
        "run-2",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_present"
    assert result["conflicting_keys"] == ("head:wf:v2",)
    assert not store.duplicate_key_exists("claim:1")


def test_atomic_events_forbid_precondition_wins_over_a_partial_overlap(tmp_path):
    """Pins the reordering: a forbid-precondition that covers the batch's own
    overlapping key must report precondition_present, not raise. Preconditions
    are checked before an own-key partial overlap falls back to the hard
    ValueError -- a losing CAS race legitimately reuses the winner's target
    key, and a declared precondition is how the caller says that overlap is
    expected, not an unexplained collision.
    """
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}}],
    )
    result = store.save_system_events_atomic(
        "run-2",
        [
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}},
            {"event_type": "funding_request", "payload": {"duplicate_key": "req:2"}},
        ],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_present"
    assert result["conflicting_keys"] == ("head:wf:v2",)
    assert not store.duplicate_key_exists("req:2")


def test_atomic_events_still_raise_when_a_precondition_misses_the_collision(tmp_path):
    """Declaring some precondition does not blanket-excuse every overlap.

    Only the key the caller named is treated as an expected race signal.  An
    overlap on a key no precondition covers is still an unexplained collision
    and must raise, even though the declared preconditions all pass.
    """
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}}],
    )
    with pytest.raises(ValueError, match="partial"):
        store.save_system_events_atomic(
            "run-2",
            [
                {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:2"}},
            ],
            require_duplicate_keys=["head:wf:v1"],
            forbid_duplicate_keys=["head:wf:v9"],
        )
    assert not store.duplicate_key_exists("req:2")


def test_atomic_events_replay_wins_over_a_failing_precondition(tmp_path):
    """A batch that already landed reports success, not a lost race.

    The head this batch wrote is exactly what its own forbid-precondition
    names, so evaluating preconditions first would misreport the replay.
    """
    store = StateStore(str(tmp_path / "s.db"))
    events = [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}}]
    first = store.save_system_events_atomic(
        "run-1", events, forbid_duplicate_keys=["head:wf:v2"]
    )
    assert first["committed"] is True
    second = store.save_system_events_atomic(
        "run-1", events, forbid_duplicate_keys=["head:wf:v2"]
    )
    assert second["conflict"] == "already_committed"


def test_atomic_events_missing_precondition_keys_are_sorted_and_deduplicated(tmp_path):
    """conflicting_keys must be normalized the same way on every branch of the
    contract: already_committed and precondition_present both return
    tuple(sorted(...)); precondition_missing must match, not leak caller
    order or caller-supplied duplicates."""
    store = StateStore(str(tmp_path / "s.db"))
    result = store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["zzz", "aaa", "aaa", "bbb", "bbb"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_missing"
    assert result["conflicting_keys"] == ("aaa", "bbb", "zzz")


def test_atomic_events_precondition_window_is_closed_to_an_outside_writer(tmp_path, monkeypatch):
    """A second, independent sqlite3 connection that skips the advisory writer_lock
    must not be able to write a forbidden key in the window between this method's
    precondition checks and its own INSERT.

    The hook fires the instant the store's connection is about to run its first
    INSERT -- i.e. strictly after every check has already run. Before the fix,
    the store's own SELECT-only checks run in autocommit mode and no lock is
    held yet at that point, so the outside writer's INSERT can land right there,
    undetected. After the fix, ``BEGIN IMMEDIATE`` is issued before the first
    check, so the write lock has already been held since the very start of the
    method, and the outside writer must be blocked out.
    """
    db_path = tmp_path / "s.db"
    store = StateStore(str(db_path))

    hook_fired = {"value": False}
    racer_landed = {"value": False}
    racer_error = {"value": None}

    original_connect = StateStore._connect

    def traced_connect(self):
        conn = original_connect(self)

        def trace(sql: str) -> None:
            if hook_fired["value"]:
                return
            if not sql.strip().upper().startswith("INSERT INTO SYSTEM_EVENTS"):
                return
            hook_fired["value"] = True
            racer = sqlite3.connect(str(db_path), timeout=0.5)
            try:
                racer.execute(
                    "INSERT INTO system_events (run_id, event_type, payload, duplicate_key) "
                    "VALUES (?, ?, ?, ?)",
                    ("racer-run", "racer_forbidden_write", "{}", "forbid:1"),
                )
                racer.commit()
                racer_landed["value"] = True
            except sqlite3.OperationalError as exc:
                racer_error["value"] = exc
            finally:
                racer.close()

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(StateStore, "_connect", traced_connect)

    result = store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}}],
        forbid_duplicate_keys=["forbid:1"],
    )

    assert hook_fired["value"] is True, "the race hook never fired -- test is not exercising it"
    assert racer_landed["value"] is False, (
        "an outside connection wrote a forbidden key inside the CAS window: "
        f"racer_error={racer_error['value']!r}"
    )
    assert isinstance(racer_error["value"], sqlite3.OperationalError)
    assert result["committed"] is True
    assert store.duplicate_key_exists("req:1")
    assert not store.duplicate_key_exists("forbid:1")


def test_atomic_events_conflict_path_leaves_no_dangling_transaction(tmp_path):
    """An early return on a conflict path (already_committed here) must still
    leave the database writable afterward -- i.e. it must not leave the
    BEGIN IMMEDIATE transaction open/dangling on the connection.
    """
    store = StateStore(str(tmp_path / "s.db"))
    events = [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}}]
    store.save_system_events_atomic("run-1", events)
    result = store.save_system_events_atomic("run-1", events)
    assert result["conflict"] == "already_committed"
    # A follow-up write must succeed promptly -- if the prior call left its
    # BEGIN IMMEDIATE transaction dangling, this would hang until busy_timeout
    # (10s) and then raise sqlite3.OperationalError.
    other = store.save_system_events_atomic(
        "run-2",
        [{"event_type": "funding_request", "payload": {"duplicate_key": "req:2"}}],
    )
    assert other["committed"] is True
    assert store.duplicate_key_exists("req:2")


def test_atomic_events_replay_detects_a_key_reused_with_different_content(tmp_path):
    """All of a batch's keys existing is not proof the batch itself landed --
    other code paths write into the same duplicate_key namespace. If the stored
    content under a key differs from what's being submitted now, this is an
    unexplained collision and must raise, not be reported as a replay.
    """
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1", "amount": 100}}],
    )
    with pytest.raises(ValueError, match="content"):
        store.save_system_events_atomic(
            "run-2",
            [
                {
                    "event_type": "funding_request",
                    "payload": {"duplicate_key": "req:1", "amount": 999},
                }
            ],
        )
    # Nothing about the stored record must have changed.
    stored = store.list_system_events_by_type("funding_request", limit=None)
    assert len(stored) == 1
    assert stored[0]["payload"]["amount"] == 100


def test_atomic_events_replay_ignores_run_id(tmp_path):
    """A legitimate retry after a crash may carry a different run id than the
    original attempt. The run id must never be part of the replay comparison.
    """
    store = StateStore(str(tmp_path / "s.db"))
    events = [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}}]
    store.save_system_events_atomic("run-1", events)
    result = store.save_system_events_atomic("run-2", events)
    assert result["committed"] is False
    assert result["conflict"] == "already_committed"


def test_atomic_events_replay_normalizes_payload_before_comparing(tmp_path):
    """Stored payloads went through ``json.dumps(payload, default=str)``, which
    stringifies values JSON cannot represent natively (e.g. datetimes). A
    retried payload carrying the same datetime value as a real ``datetime``
    object (not yet stringified) must still compare equal to what's stored --
    comparing raw, un-normalized structures would falsely flag this as a
    content mismatch.
    """
    store = StateStore(str(tmp_path / "s.db"))
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        {
            "event_type": "funding_request",
            "payload": {"duplicate_key": "req:1", "observed_at": moment},
        }
    ]
    store.save_system_events_atomic("run-1", events)
    result = store.save_system_events_atomic("run-1", events)
    assert result["committed"] is False
    assert result["conflict"] == "already_committed"


def test_only_one_process_wins_the_head_transition(tmp_path):
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    store.save_system_events_atomic(
        "run-0",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}}],
    )
    ready = multiprocessing.Event()
    with multiprocessing.Manager() as manager:
        results = manager.list()
        procs = [
            multiprocessing.Process(target=_race_workflow_head, args=(db, n, ready, results))
            for n in (1, 2, 3)
        ]
        for proc in procs:
            proc.start()
        ready.set()
        for proc in procs:
            proc.join(30.0)
            assert proc.exitcode == 0
        outcomes = list(results)

    winners = [item for item in outcomes if item[1] is True]
    assert len(winners) == 1
    assert {item[2] for item in outcomes if item[1] is False} == {"precondition_present"}
    # The loser's request event must not exist: its batch was all-or-nothing.
    winning_attempt = winners[0][0]
    for attempt in (1, 2, 3):
        assert store.duplicate_key_exists(f"req:{attempt}") is (attempt == winning_attempt)
