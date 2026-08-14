"""telegram_ui_card_state를 먼저 만든 DB를 새 코드가 열 때.

CREATE TABLE IF NOT EXISTS는 이미 있는 테이블에 컬럼을 더하지 않는다. 이 브랜치의
앞선 커밋이 만든 운영 테이블에는 consecutive_failures가 없으므로, 마이그레이션이
없으면 카드 전송과 헬스체크가 'no such column'으로 죽는다.
"""

import multiprocessing
import sqlite3

from maestro.integrations.telegram.ui.card_state import card_failure_event
from maestro.state.store import StateStore

#: consecutive_failures가 없던 시절의 스키마 그대로.
_OLD_SCHEMA = (
    "CREATE TABLE telegram_ui_card_state ("
    "card_key TEXT NOT NULL, "
    "chat_id INTEGER NOT NULL, "
    "message_id INTEGER, "
    "stage TEXT NOT NULL, "
    "render_hash TEXT NOT NULL, "
    "delivery TEXT NOT NULL, "
    "operation_id TEXT NOT NULL, "
    "updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
    "PRIMARY KEY (card_key, chat_id))"
)


def _old_schema_db(tmp_path, *, rows=()):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute(_OLD_SCHEMA)
        for row in rows:
            conn.execute(
                "INSERT INTO telegram_ui_card_state "
                "(card_key, chat_id, message_id, stage, render_hash, delivery, operation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    return path


def _open_legacy_store_together(path, start, results):
    start.wait(timeout=10)
    try:
        StateStore(path, 0.0)
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put((type(exc).__name__, str(exc)))
    else:
        results.put(None)


def _columns(path):
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(telegram_ui_card_state)")}


def test_an_existing_card_table_gains_the_failure_counter(tmp_path):
    """운영 DB에 실제로 있던 상태다 — 컬럼 없이 테이블만 존재한다."""
    path = _old_schema_db(tmp_path)
    assert "consecutive_failures" not in _columns(path)

    StateStore(str(path))

    assert "consecutive_failures" in _columns(path)


def test_rows_written_before_the_upgrade_survive_and_start_at_zero(tmp_path):
    """이미 전달된 카드의 message_id를 잃으면 그 카드는 다시 갱신되지 않는다."""
    path = _old_schema_db(
        tmp_path,
        rows=[("approval:appr_old", 100, 5001, "pending", "h1", "confirmed", "op1")],
    )

    store = StateStore(str(path))

    copies = store.load_card_delivery_state("approval:appr_old")
    assert copies == [
        {
            "card_key": "approval:appr_old",
            "chat_id": 100,
            "message_id": 5001,
            "stage": "pending",
            "render_hash": "h1",
            "delivery": "confirmed",
            "operation_id": "op1",
            "consecutive_failures": 0,
        }
    ]


def test_the_upgraded_table_still_counts_failures(tmp_path):
    """마이그레이션이 컬럼만 붙이고 기본값을 놓치면 카운터가 NULL로 깨진다."""
    path = _old_schema_db(
        tmp_path,
        rows=[("approval:appr_old", 100, 5001, "pending", "h1", "confirmed", "op1")],
    )
    store = StateStore(str(path))

    for attempt in range(3):
        store.record_card_event(
            "run_1",
            card_failure_event(
                "approval:appr_old", 100, "pending", "h1", f"op{attempt}", "refused"
            ),
        )

    assert store.load_card_delivery_state("approval:appr_old")[0]["consecutive_failures"] == 3
    assert store.list_failing_card_copies(3)


def test_upgrading_twice_is_harmless(tmp_path):
    """StateStore는 프로세스마다 여러 번 열린다."""
    path = _old_schema_db(tmp_path)
    StateStore(str(path))

    StateStore(str(path))

    assert "consecutive_failures" in _columns(path)


def test_the_database_stays_intact_through_the_upgrade(tmp_path):
    path = _old_schema_db(
        tmp_path,
        rows=[("approval:appr_old", 100, 5001, "pending", "h1", "confirmed", "op1")],
    )

    StateStore(str(path))

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_constructors_migrate_one_legacy_database(tmp_path):
    path = _old_schema_db(tmp_path)
    start = multiprocessing.Event()
    results = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_open_legacy_store_together, args=(path, start, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    assert [results.get(timeout=2) for _ in processes] == [None, None, None, None]
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_xinfo(system_events)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(system_events)")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    assert {"approval_id", "signal_run_id"} <= columns
    assert {"idx_system_events_type_approval", "idx_system_events_type_signal_run"} <= indexes
    assert integrity == "ok"


def test_a_database_that_predates_the_terminal_index_gains_it(tmp_path):
    """종결 표시 테이블도 앞선 커밋이 만든 DB에는 없다.

    없으면 sweep의 첫 조회가 'no such table'로 죽고, 그 예외는 poll_once가
    삼키므로 카드 갱신이 조용히 멈춘다.
    """
    path = _old_schema_db(tmp_path)
    store = StateStore(path, 0.0)

    store.mark_signal_run_cards_settled(
        "signal_1", approval_ids=["appr_1"], order_ids=["ord_1"], max_event_id=7
    )

    assert store.reopen_settled_signal_runs(["ord_1"]) == 1


def test_an_unknown_recovery_does_not_disturb_settled_runs(tmp_path):
    """되살리는 것은 이 run에 실제로 걸린 건뿐이다."""
    store = StateStore(tmp_path / "state.db", 0.0)
    store.mark_signal_run_cards_settled(
        "signal_1", approval_ids=["appr_1"], order_ids=["ord_1"], max_event_id=7
    )

    assert store.reopen_settled_signal_runs(["ord_other"]) == 0
    assert store.reopen_settled_signal_runs([]) == 0
    assert store.reopen_settled_signal_runs(["ord_1"]) == 1


def test_an_unresolved_resolution_failure_reopens_its_run(tmp_path):
    """실패 쪽 판정은 SQL 안에 있다 — 완료가 없는 실패만 되살린다."""
    store = StateStore(tmp_path / "state.db", 0.0)
    store.mark_signal_run_cards_settled(
        "signal_1", approval_ids=["appr_1"], order_ids=["ord_1"], max_event_id=7
    )
    store.save_system_event(
        "run_1", "telegram_approval_resolution_failed", {"approval_id": "appr_1"}
    )

    assert store.reopen_settled_signal_runs([]) == 1


def test_a_resolution_failure_that_completed_does_not_reopen_its_run(tmp_path):
    """재개가 성공한 run까지 되살리면 매 poll 표시를 지웠다 다시 쓰게 된다."""
    store = StateStore(tmp_path / "state.db", 0.0)
    store.mark_signal_run_cards_settled(
        "signal_1", approval_ids=["appr_1"], order_ids=["ord_1"], max_event_id=7
    )
    store.save_system_event(
        "run_1", "telegram_approval_resolution_failed", {"approval_id": "appr_1"}
    )
    store.save_system_event("run_1", "signal_approval_completed", {"approval_id": "appr_1"})

    assert store.reopen_settled_signal_runs([]) == 0


def test_a_database_that_predates_the_audience_table_gains_it(tmp_path):
    """수신자 테이블도 앞선 커밋이 만든 DB에는 없다.

    없으면 카드 전송의 첫 쓰기가 'no such table'로 죽는다 — dispatch가 카드를
    보내는 경로이므로 승인 요청 자체가 나가지 못한다.
    """
    path = _old_schema_db(tmp_path)
    store = StateStore(path, 0.0)

    store.record_card_audience("approval:appr_1", [100, 200])

    assert store.load_card_audience("approval:appr_1") == [100, 200]


def test_recording_the_audience_again_keeps_the_chats_already_there(tmp_path):
    """이미 적힌 채팅은 지워지지 않는다.

    수신자는 카드가 태어날 때의 사실이므로, 나중 설정이 그것을 덮어쓰면
    "이 카드는 저 채팅에 갔었다"는 기록이 사라진다.
    """
    store = StateStore(tmp_path / "state.db", 0.0)
    store.record_card_audience("approval:appr_1", [100, 200])

    store.record_card_audience("approval:appr_1", [100, 300])

    assert store.load_card_audience("approval:appr_1") == [100, 200, 300]


def _trace(store, call):
    """실제로 실행된 SELECT를 잡아낸다.

    테스트가 손으로 쓴 SQL의 계획을 보면, 메서드가 인덱스를 안 타는 형태로
    돌아가도 테스트는 계속 통과한다 — 비용 계약이 아니라 테스트 문자열을
    검증하게 된다.
    """
    statements: list[str] = []
    real_connect = store._connect

    def traced():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    store._connect = traced
    try:
        call()
    finally:
        store._connect = real_connect
    return [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]


def _plan(store, sql):
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    return " | ".join(str(row[3]) for row in rows)


def test_the_approval_lookup_seeks_an_index_instead_of_scanning(tmp_path):
    """비용 계약은 동작 테스트로 보이지 않으므로 쿼리 계획으로 고정한다.

    인덱스가 없으면 SQLite는 그 event type의 모든 행을 훑으며 json_extract를
    돌린다 — poll 하나의 비용이 지금까지 받은 승인 수에 비례하게 된다.
    """
    store = StateStore(tmp_path / "state.db", 0.0)

    executed = _trace(
        store,
        lambda: store.latest_payloads_by_approval_id("signal_approval_completed", ["appr_1"]),
    )

    assert len(executed) == 1
    plan = _plan(store, executed[0])
    assert "idx_system_events_type_approval" in plan
    assert "SCAN" not in plan


def test_the_settled_scan_seeks_the_signal_run_index(tmp_path):
    store = StateStore(tmp_path / "state.db", 0.0)
    store.mark_signal_run_cards_settled(
        "signal_1", approval_ids=["appr_1"], order_ids=["ord_1"], max_event_id=1
    )

    executed = _trace(store, store.list_unsettled_pending_approvals)

    assert len(executed) == 1
    plan = _plan(store, executed[0])
    assert "idx_system_events_type_signal_run" in plan


def test_the_projected_columns_follow_the_payload_whatever_wrote_it(tmp_path):
    """생성 컬럼이라 INSERT 경로마다 채워 줄 필요가 없다.

    system_events에 넣는 자리는 일곱 군데인데 order_id 투영을 채우는 곳은 셋뿐이다.
    한 군데만 빠뜨려도 그 승인은 sweep에서 조용히 사라진다 — 느려지는 것이 아니라
    틀려진다.
    """
    store = StateStore(tmp_path / "state.db", 0.0)
    store.save_system_event(
        "run_1", "telegram_approval_pending", {"approval_id": "appr_1", "signal_run_id": "sig_1"}
    )

    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT approval_id, signal_run_id FROM system_events WHERE event_type = ?",
            ("telegram_approval_pending",),
        ).fetchone()

    assert row == ("appr_1", "sig_1")


def test_a_database_that_predates_the_projected_columns_gains_them(tmp_path):
    """PRAGMA table_info는 VIRTUAL 생성 컬럼을 보여주지 않는다.

    그것으로 존재 여부를 판단하면 두 번째 열 때 "duplicate column name"으로 죽는다.
    """
    path = _old_schema_db(tmp_path)
    StateStore(path, 0.0).save_system_event(
        "run_1", "telegram_approval_pending", {"approval_id": "appr_1", "signal_run_id": "sig_1"}
    )

    reopened = StateStore(path, 0.0)

    assert set(
        reopened.latest_payloads_by_approval_id("telegram_approval_pending", ["appr_1"])
    ) == {"appr_1"}
