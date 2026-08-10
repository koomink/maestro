import sqlite3
from datetime import UTC, datetime, timedelta

from maestro.state.store import StateStore


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
