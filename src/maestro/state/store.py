import json
import sqlite3
from pathlib import Path
from typing import Any

from maestro.state.models import PortfolioState


class StateStore:
    def __init__(self, path: str, initial_cash: float) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_cash = initial_cash
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS portfolio_snapshots "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS strategy_runs "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "strategy_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orders "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "order_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS system_events "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "event_type TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS approvals "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "approval_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS broker_account_snapshots "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "account_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS risk_decisions "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "approved INTEGER, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )

    def load_latest_portfolio_state(self) -> PortfolioState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return PortfolioState(cash=self.initial_cash, positions={})
        return PortfolioState.model_validate_json(row[0])

    def save_portfolio_snapshot(self, run_id: str, state: PortfolioState) -> None:
        self._insert("portfolio_snapshots", run_id, None, state.model_dump(mode="json"))

    def save_strategy_run(self, run_id: str, strategy_id: str, payload: dict[str, Any]) -> None:
        self._insert("strategy_runs", run_id, strategy_id, payload)

    def save_order(self, run_id: str, order_id: str, payload: dict[str, Any]) -> None:
        self._insert("orders", run_id, order_id, payload)

    def save_system_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._insert("system_events", run_id, event_type, payload)

    def save_approval(self, run_id: str, approval_id: str, payload: dict[str, Any]) -> None:
        self._insert("approvals", run_id, approval_id, payload)

    def save_broker_account_snapshot(
        self, run_id: str, account_id: str, payload: dict[str, Any]
    ) -> None:
        self._insert("broker_account_snapshots", run_id, account_id, payload)

    def save_risk_decision(self, run_id: str, approved: bool, payload: dict[str, Any]) -> None:
        payload_with_approved = dict(payload)
        payload_with_approved["approved"] = approved
        self._insert("risk_decisions", run_id, str(int(approved)), payload_with_approved)

    def list_portfolio_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("portfolio_snapshots", limit)

    def list_strategy_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("strategy_runs", limit)

    def list_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("orders", limit)

    def list_system_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("system_events", limit)

    def list_approvals(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("approvals", limit)

    def list_broker_account_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("broker_account_snapshots", limit)

    def list_risk_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("risk_decisions", limit)

    def load_latest_broker_account_snapshot(self) -> dict[str, Any] | None:
        rows = self.list_broker_account_snapshots(limit=1)
        return rows[0] if rows else None

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            tables = [
                "portfolio_snapshots",
                "strategy_runs",
                "orders",
                "system_events",
                "approvals",
                "broker_account_snapshots",
                "risk_decisions",
            ]
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }
            latest_snapshot = conn.execute(
                "SELECT run_id, created_at FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "counts": counts,
            "latest_snapshot": {
                "run_id": latest_snapshot[0],
                "created_at": latest_snapshot[1],
            }
            if latest_snapshot
            else None,
        }

    def _insert(
        self,
        table: str,
        run_id: str,
        secondary: str | None,
        payload: dict[str, Any],
    ) -> None:
        payload_json = json.dumps(payload, default=str)
        with self._connect() as conn:
            if table == "strategy_runs":
                conn.execute(
                    "INSERT INTO strategy_runs (run_id, strategy_id, payload) VALUES (?, ?, ?)",
                    (run_id, secondary, payload_json),
                )
            elif table == "orders":
                conn.execute(
                    "INSERT INTO orders (run_id, order_id, payload) VALUES (?, ?, ?)",
                    (run_id, secondary, payload_json),
                )
            elif table == "system_events":
                conn.execute(
                    "INSERT INTO system_events (run_id, event_type, payload) VALUES (?, ?, ?)",
                    (run_id, secondary, payload_json),
                )
            elif table == "approvals":
                conn.execute(
                    "INSERT INTO approvals (run_id, approval_id, payload) VALUES (?, ?, ?)",
                    (run_id, secondary, payload_json),
                )
            elif table == "broker_account_snapshots":
                conn.execute(
                    "INSERT INTO broker_account_snapshots "
                    "(run_id, account_id, payload) VALUES (?, ?, ?)",
                    (run_id, secondary, payload_json),
                )
            elif table == "risk_decisions":
                conn.execute(
                    "INSERT INTO risk_decisions (run_id, approved, payload) VALUES (?, ?, ?)",
                    (run_id, int(secondary or "0"), payload_json),
                )
            else:
                conn.execute(
                    "INSERT INTO portfolio_snapshots (run_id, payload) VALUES (?, ?)",
                    (run_id, payload_json),
                )

    def _list_rows(self, table: str, limit: int) -> list[dict[str, Any]]:
        allowed_tables = {
            "portfolio_snapshots",
            "strategy_runs",
            "orders",
            "system_events",
            "approvals",
            "broker_account_snapshots",
            "risk_decisions",
        }
        if table not in allowed_tables:
            raise ValueError(f"Unsupported table: {table}")

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output
