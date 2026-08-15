import sqlite3
import threading

from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.db"))


def test_a_first_write_is_created_and_returned(tmp_path):
    store = _store(tmp_path)

    payload, created = store.insert_or_load_system_event(
        "run-1", "telegram_approval_pending", {"approval_id": "a1"}, "dispatch-group:g1"
    )

    assert created is True
    assert payload == {"approval_id": "a1"}


def test_a_second_write_returns_the_stored_payload_and_does_not_overwrite(tmp_path):
    store = _store(tmp_path)
    store.insert_or_load_system_event(
        "run-1", "telegram_approval_pending", {"approval_id": "a1"}, "dispatch-group:g1"
    )

    payload, created = store.insert_or_load_system_event(
        "run-2", "telegram_approval_pending", {"approval_id": "a2"}, "dispatch-group:g1"
    )

    # The winner's payload comes back, not the one just submitted. A resume
    # that adopted its own freshly minted approval_id would send a second
    # button-bearing card for orders that already have one.
    assert created is False
    assert payload == {"approval_id": "a1"}


def test_a_differing_payload_does_not_raise(tmp_path):
    # save_system_events_atomic refuses a key whose stored content differs.
    # That is right for a multi-event transition and wrong here: the envelope
    # carries approval_id, timestamps and rendered text, so a resume's
    # recomputation practically always differs, and raising would strand the
    # signal run this whole change exists to unstick.
    store = _store(tmp_path)
    store.insert_or_load_system_event(
        "run-1", "telegram_approval_pending", {"approval_id": "a1", "message": "old"}, "k"
    )

    payload, created = store.insert_or_load_system_event(
        "run-1", "telegram_approval_pending", {"approval_id": "a1", "message": "new"}, "k"
    )

    assert created is False
    assert payload["message"] == "old"


def test_only_one_row_is_ever_written_for_a_key(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        store.insert_or_load_system_event(
            "run-1", "telegram_approval_pending", {"approval_id": f"a{index}"}, "k"
        )

    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE duplicate_key = ?", ("k",)
        ).fetchone()[0]
    assert count == 1


def test_concurrent_writers_all_receive_the_same_winner(tmp_path):
    # The advisory writer_lock does not cover a recovery script, a different
    # release, or the sqlite3 CLI, so the race has to resolve inside one
    # transaction rather than be assumed away.
    store = _store(tmp_path)
    barrier = threading.Barrier(8)
    results: list[tuple[dict, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            outcome = store.insert_or_load_system_event(
                f"run-{index}",
                "telegram_approval_pending",
                {"approval_id": f"a{index}"},
                "dispatch-group:contended",
            )
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 8
    assert sum(1 for _, created in results if created) == 1
    approval_ids = {payload["approval_id"] for payload, _ in results}
    assert len(approval_ids) == 1


def test_the_stored_row_keeps_the_creating_run_id(tmp_path):
    store = _store(tmp_path)
    store.insert_or_load_system_event("run-1", "telegram_approval_pending", {"a": 1}, "k")
    store.insert_or_load_system_event("run-2", "telegram_approval_pending", {"a": 2}, "k")

    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        run_id = conn.execute(
            "SELECT run_id FROM system_events WHERE duplicate_key = ?", ("k",)
        ).fetchone()[0]
    assert run_id == "run-1"


def test_load_by_duplicate_key_returns_none_when_absent(tmp_path):
    store = _store(tmp_path)

    assert store.load_system_event_payload_by_duplicate_key("nope") is None


def test_load_by_duplicate_key_returns_the_stored_payload(tmp_path):
    store = _store(tmp_path)
    store.insert_or_load_system_event("run-1", "telegram_approval_pending", {"a": 1}, "k")

    assert store.load_system_event_payload_by_duplicate_key("k") == {"a": 1}
