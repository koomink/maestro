import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from maestro.config.identity import ConfigIdentity
from maestro.state.models import PortfolioState


class StateStore:
    def __init__(
        self,
        path: str,
        initial_cash: float | None = None,
        initial_cash_by_currency: dict[str, float] | None = None,
        config_identity: ConfigIdentity | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._writer_lock_depth = 0
        self.initial_cash = float(initial_cash or 0.0)
        self.initial_cash_by_currency = dict(initial_cash_by_currency or {})
        self._init_db()
        if config_identity is not None:
            self.validate_config_identity(config_identity)

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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS strategy_book_snapshots "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "strategy_id TEXT, "
                "book_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS operator_metadata "
                "("
                "key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )

    @contextmanager
    def writer_lock(
        self,
        owner: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Any:
        del owner
        if self._writer_lock_depth > 0:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"State writer lock is busy: {self.lock_path}") from exc
                    time.sleep(0.1)
            self._writer_lock_depth += 1
            try:
                yield
            finally:
                self._writer_lock_depth -= 1
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def validate_config_identity(self, identity: ConfigIdentity) -> None:
        payload = identity.model_dump()
        existing = self.load_operator_config_identity()
        if existing is not None and existing != payload:
            raise ValueError(
                "State DB config identity mismatch: "
                f"state_db={self.path} existing_path={existing.get('path')} "
                f"existing_fingerprint={existing.get('fingerprint')} "
                f"current_path={payload['path']} current_fingerprint={payload['fingerprint']}"
            )
        if existing is None:
            self._set_metadata("operator_config_identity", payload)

    def load_operator_config_identity(self) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM operator_metadata WHERE key = ?",
                ("operator_config_identity",),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def load_latest_portfolio_state(self) -> PortfolioState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return PortfolioState(
                cash=self.initial_cash,
                cash_by_currency=self.initial_cash_by_currency,
                positions={},
            )
        return PortfolioState.model_validate_json(row[0])

    def has_portfolio_snapshot(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM portfolio_snapshots LIMIT 1").fetchone()
        return row is not None

    def save_portfolio_snapshot(self, run_id: str, state: PortfolioState) -> None:
        self._insert("portfolio_snapshots", run_id, None, state.model_dump(mode="json"))

    def save_strategy_run(self, run_id: str, strategy_id: str, payload: dict[str, Any]) -> None:
        self._insert("strategy_runs", run_id, strategy_id, payload)

    def save_order(self, run_id: str, order_id: str, payload: dict[str, Any]) -> None:
        self._insert("orders", run_id, order_id, payload)

    def save_system_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._insert("system_events", run_id, event_type, payload)

    def save_approval(self, run_id: str, approval_id: str, payload: dict[str, Any]) -> None:
        if self.approval_exists(approval_id):
            raise ValueError(f"Approval decision already exists: {approval_id}")
        self._insert("approvals", run_id, approval_id, payload)

    def approval_exists(self, approval_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM approvals WHERE approval_id = ? LIMIT 1",
                (approval_id,),
            ).fetchone()
        return row is not None

    def save_broker_account_snapshot(
        self, run_id: str, account_id: str, payload: dict[str, Any]
    ) -> None:
        self._insert("broker_account_snapshots", run_id, account_id, payload)

    def save_risk_decision(self, run_id: str, approved: bool, payload: dict[str, Any]) -> None:
        payload_with_approved = dict(payload)
        payload_with_approved["approved"] = approved
        self._insert("risk_decisions", run_id, str(int(approved)), payload_with_approved)

    def save_strategy_book_snapshots(
        self,
        run_id: str,
        snapshots: list[dict[str, Any]],
    ) -> None:
        payloads = [
            (
                run_id,
                str(snapshot.get("strategy_id") or ""),
                str(snapshot.get("book_id") or ""),
                json.dumps(snapshot, default=str),
            )
            for snapshot in snapshots
        ]
        if not payloads:
            return
        with self.writer_lock("save_strategy_book_snapshots"):
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO strategy_book_snapshots "
                    "(run_id, strategy_id, book_id, payload) VALUES (?, ?, ?, ?)",
                    payloads,
                )

    def list_portfolio_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("portfolio_snapshots", limit)

    def list_strategy_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("strategy_runs", limit)

    def list_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("orders", limit)

    def monthly_contribution_order_exists(self, month_key: str, sleeve: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM orders ORDER BY id DESC").fetchall()
        for row in rows:
            payload = json.loads(row[0])
            metadata = payload.get("metadata", {})
            if (
                metadata.get("order_generation_mode") == "buy_only_contribution"
                and metadata.get("contribution_month") == month_key
                and metadata.get("contribution_sleeve") == sleeve
            ):
                return True
        return False

    def list_system_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("system_events", limit)

    def list_system_events_by_type(
        self,
        event_type: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM system_events WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def load_latest_system_event(self, event_type: str) -> dict[str, Any] | None:
        rows = self.list_system_events_by_type(event_type, limit=1)
        return rows[0] if rows else None

    def list_approvals(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("approvals", limit)

    def list_broker_account_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("broker_account_snapshots", limit)

    def list_risk_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("risk_decisions", limit)

    def list_strategy_book_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_rows("strategy_book_snapshots", limit)

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
                "strategy_book_snapshots",
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
            "operator_config": self.load_operator_config_identity(),
        }

    def _insert(
        self,
        table: str,
        run_id: str,
        secondary: str | None,
        payload: dict[str, Any],
    ) -> None:
        payload_json = json.dumps(payload, default=str)
        with self.writer_lock(f"insert:{table}"):
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

    def _set_metadata(self, key: str, value: dict[str, Any]) -> None:
        payload_json = json.dumps(value, sort_keys=True)
        with self.writer_lock(f"metadata:{key}"):
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO operator_metadata (key, value, updated_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (key, payload_json),
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
            "strategy_book_snapshots",
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
