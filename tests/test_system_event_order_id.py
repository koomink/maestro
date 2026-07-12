import json
import sqlite3

from maestro.state.store import StateStore


def test_save_system_event_populates_order_id_column(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))

    store.save_system_event(
        "run-1",
        "live_order_lifecycle",
        {"run_id": "run-1", "order_id": "ord-top-level", "final_status": "filled"},
    )
    store.save_system_event(
        "run-1",
        "live_order_result",
        {"request": {"order_id": "ord-nested-request"}, "result": {"status": "filled"}},
    )
    store.save_system_event(
        "run-1",
        "signal_package",
        {"signal_run_id": "run-1"},
    )

    with sqlite3.connect(tmp_path / "state.db") as conn:
        rows = dict(
            conn.execute(
                "SELECT event_type, order_id FROM system_events"
            ).fetchall()
        )

    assert rows["live_order_lifecycle"] == "ord-top-level"
    assert rows["live_order_result"] == "ord-nested-request"
    assert rows["signal_package"] is None


def test_system_event_exists_and_list_order_ids_for_event_type(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_system_event(
        "run-1",
        "live_order_lifecycle",
        {"run_id": "run-1", "order_id": "ord-1", "final_status": "filled"},
    )
    store.save_system_event(
        "run-1",
        "live_order_lifecycle",
        {"run_id": "run-1", "order_id": "ord-2", "final_status": "open"},
    )

    assert store.system_event_exists("live_order_lifecycle", "ord-1") is True
    assert store.system_event_exists("live_order_lifecycle", "ord-missing") is False
    assert store.list_order_ids_for_event_type("live_order_lifecycle") == {"ord-1", "ord-2"}
    assert store.list_order_ids_for_event_type("live_order_result") == set()


def test_list_system_events_by_broker_order_id(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_system_event(
        "run-1",
        "live_order_result",
        {
            "request": {"account_id": "acct-1", "sleeve": "sleeve-a"},
            "result": {"broker_order": {"broker_order_id": "BROKER-1"}},
        },
    )
    store.save_system_event(
        "run-1",
        "live_order_result",
        {
            "request": {"account_id": "acct-2", "sleeve": "sleeve-b"},
            "result": {"broker_order": {"broker_order_id": "BROKER-2"}},
        },
    )

    rows = store.list_system_events_by_broker_order_id("BROKER-1", "live_order_result")

    assert len(rows) == 1
    assert rows[0]["payload"]["request"]["account_id"] == "acct-1"
    assert store.list_system_events_by_broker_order_id("BROKER-missing", "live_order_result") == []


def test_legacy_schema_without_order_id_column_is_backfilled_on_open(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE system_events "
            "("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT, "
            "event_type TEXT, "
            "payload TEXT NOT NULL, "
            "duplicate_key TEXT, "
            "broker_order_id TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.execute(
            "INSERT INTO system_events (run_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "run-1",
                "live_order_lifecycle",
                json.dumps(
                    {"run_id": "run-1", "order_id": "ord-legacy-top", "final_status": "filled"}
                ),
            ),
        )
        conn.execute(
            "INSERT INTO system_events (run_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "run-1",
                "live_order_result",
                json.dumps(
                    {"request": {"order_id": "ord-legacy-nested"}, "result": {"status": "filled"}}
                ),
            ),
        )
        conn.execute(
            "INSERT INTO system_events (run_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "run-1",
                "signal_package",
                json.dumps({"signal_run_id": "run-1"}),
            ),
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(system_events)").fetchall()}
        assert "order_id" not in columns

    store = StateStore(str(db_path))

    assert store.system_event_exists("live_order_lifecycle", "ord-legacy-top") is True
    assert store.system_event_exists("live_order_result", "ord-legacy-nested") is True
    assert store.list_order_ids_for_event_type("signal_package") == set()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(system_events)").fetchall()}
        assert "order_id" in columns
        signal_order_id = conn.execute(
            "SELECT order_id FROM system_events WHERE event_type = 'signal_package'"
        ).fetchone()[0]
        assert signal_order_id is None


def test_backfill_runs_once_and_is_stable_on_subsequent_opens(tmp_path):
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))
    store.save_system_event(
        "run-1",
        "live_order_lifecycle",
        {"run_id": "run-1", "order_id": "ord-1", "final_status": "filled"},
    )

    reopened = StateStore(str(db_path))

    assert reopened.system_event_exists("live_order_lifecycle", "ord-1") is True


def test_monthly_live_contribution_order_exists_uses_order_id_column(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order(
        "run-1",
        "ord-contribution",
        {
            "metadata": {
                "order_generation_mode": "buy_only_contribution",
                "contribution_month": "2026-05",
                "contribution_sleeve": "KRW",
            }
        },
    )

    assert store.monthly_live_contribution_order_exists("2026-05", "KRW") is False

    store.save_system_event(
        "run-1",
        "live_order_lifecycle",
        {
            "run_id": "run-1",
            "order_id": "ord-contribution",
            "final_status": "open",
            "applied_fills": [],
        },
    )

    assert store.monthly_live_contribution_order_exists("2026-05", "KRW") is True
