import json
import sqlite3

from maestro.state.store import StateStore


def _payload(
    order_id: str,
    *,
    month: str = "2026-05",
    sleeve: str = "KRW",
    execution_sleeve: str | None = None,
    account_id: str | None = None,
    mode: str = "buy_only_contribution",
) -> dict:
    metadata = {
        "order_generation_mode": mode,
        "contribution_month": month,
        "contribution_sleeve": sleeve,
    }
    if execution_sleeve is not None:
        metadata["execution_sleeve"] = execution_sleeve
    if account_id is not None:
        metadata["account_id"] = account_id
    return {"order_id": order_id, "metadata": metadata}


def test_save_order_populates_contribution_columns(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))

    store.save_order("run-1", "ord-1", _payload("ord-1"))
    store.save_order(
        "run-1",
        "ord-2",
        _payload("ord-2", execution_sleeve="sleeve-a", account_id="acct-1"),
    )
    store.save_order("run-1", "ord-3", {"order_id": "ord-3", "metadata": {}})

    with sqlite3.connect(tmp_path / "state.db") as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT order_id, contribution_month, contribution_sleeve, "
                "execution_sleeve, account_id FROM orders"
            ).fetchall()
        }

    assert rows["ord-1"] == ("2026-05", "KRW", None, None)
    assert rows["ord-2"] == ("2026-05", "KRW", "sleeve-a", "acct-1")
    assert rows["ord-3"] == (None, None, None, None)


def test_save_order_does_not_populate_contribution_fields_for_other_modes(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))

    store.save_order(
        "run-1",
        "ord-rebalance",
        _payload("ord-rebalance", mode="target_rebalance", execution_sleeve="sleeve-a"),
    )

    with sqlite3.connect(tmp_path / "state.db") as conn:
        row = conn.execute(
            "SELECT contribution_month, contribution_sleeve, execution_sleeve "
            "FROM orders WHERE order_id = 'ord-rebalance'"
        ).fetchone()

    assert row == (None, None, "sleeve-a")


def test_monthly_contribution_order_exists_uses_indexed_columns(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order("run-1", "ord-1", _payload("ord-1"))

    assert store.monthly_contribution_order_exists("2026-05", "KRW") is True
    assert store.monthly_contribution_order_exists("2026-06", "KRW") is False
    assert store.monthly_contribution_order_exists("2026-05", "USD") is False


def test_scope_filter_none_ignores_execution_sleeve_and_account_id(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order(
        "run-1",
        "ord-1",
        _payload("ord-1", execution_sleeve="sleeve-a", account_id="acct-1"),
    )

    assert store.monthly_contribution_order_exists("2026-05", "KRW") is True
    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", execution_sleeve="sleeve-a"
        )
        is True
    )
    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", account_id="acct-1"
        )
        is True
    )


def test_scope_filter_value_requires_exact_match(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order(
        "run-1",
        "ord-1",
        _payload("ord-1", execution_sleeve="sleeve-a", account_id="acct-1"),
    )

    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", execution_sleeve="sleeve-b"
        )
        is False
    )
    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", account_id="acct-2"
        )
        is False
    )
    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", execution_sleeve="sleeve-a", account_id="acct-1"
        )
        is True
    )


def test_scope_filter_value_excludes_orders_missing_the_field(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order("run-1", "ord-1", _payload("ord-1"))

    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", execution_sleeve="sleeve-a"
        )
        is False
    )
    assert (
        store.monthly_contribution_order_exists("2026-05", "KRW", account_id="acct-1")
        is False
    )


def test_monthly_contribution_order_ids_respects_scope(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_order(
        "run-1", "ord-1", _payload("ord-1", execution_sleeve="sleeve-a", account_id="acct-1")
    )
    store.save_order(
        "run-1", "ord-2", _payload("ord-2", execution_sleeve="sleeve-b", account_id="acct-2")
    )

    assert store._monthly_contribution_order_ids("2026-05", "KRW") == {"ord-1", "ord-2"}
    assert store._monthly_contribution_order_ids(
        "2026-05", "KRW", execution_sleeve="sleeve-a"
    ) == {"ord-1"}
    assert store._monthly_contribution_order_ids("2026-05", "KRW", account_id="acct-2") == {
        "ord-2"
    }


def test_legacy_schema_without_contribution_columns_is_backfilled_on_open(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE orders "
            "("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT, "
            "order_id TEXT, "
            "payload TEXT NOT NULL, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.execute(
            "INSERT INTO orders (run_id, order_id, payload) VALUES (?, ?, ?)",
            (
                "run-1",
                "ord-legacy",
                json.dumps(
                    _payload("ord-legacy", execution_sleeve="sleeve-a", account_id="acct-1")
                ),
            ),
        )
        conn.execute(
            "INSERT INTO orders (run_id, order_id, payload) VALUES (?, ?, ?)",
            (
                "run-1",
                "ord-legacy-other-month",
                json.dumps(_payload("ord-legacy-other-month", month="2026-04")),
            ),
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
        assert "contribution_month" not in columns

    store = StateStore(str(db_path))

    assert store.monthly_contribution_order_exists("2026-05", "KRW") is True
    assert (
        store.monthly_contribution_order_exists(
            "2026-05", "KRW", execution_sleeve="sleeve-a"
        )
        is True
    )
    assert store.monthly_contribution_order_exists("2026-04", "KRW") is True
    assert store.monthly_contribution_order_exists("2026-06", "KRW") is False

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
        assert "contribution_month" in columns
        assert "contribution_sleeve" in columns
        assert "execution_sleeve" in columns
        assert "account_id" in columns


def test_backfill_runs_once_and_is_stable_on_subsequent_opens(tmp_path):
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))
    store.save_order("run-1", "ord-1", _payload("ord-1"))

    reopened = StateStore(str(db_path))

    assert reopened.monthly_contribution_order_exists("2026-05", "KRW") is True
