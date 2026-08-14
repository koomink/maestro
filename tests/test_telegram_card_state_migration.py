"""telegram_ui_card_state를 먼저 만든 DB를 새 코드가 열 때.

CREATE TABLE IF NOT EXISTS는 이미 있는 테이블에 컬럼을 더하지 않는다. 이 브랜치의
앞선 커밋이 만든 운영 테이블에는 consecutive_failures가 없으므로, 마이그레이션이
없으면 카드 전송과 헬스체크가 'no such column'으로 죽는다.
"""

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
