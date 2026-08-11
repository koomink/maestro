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
