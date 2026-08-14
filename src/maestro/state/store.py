import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from maestro.config.identity import ConfigIdentity
from maestro.state.models import PortfolioState

# Lock re-entrancy depth, per thread, keyed by resolved lock-file path rather
# than by StateStore instance. flock is owned by the open file description, so
# two StateStore objects pointing at one database hold genuinely separate
# handles: instance-scoped depths would let the same thread take a lock it
# already holds (a self-deadlock) and would let the ordering rule below be
# sidestepped by simply constructing a second store. Several call paths do
# build their own StateStore, so that boundary is real, not hypothetical.
_LOCK_DEPTHS = threading.local()


def _lock_depths() -> dict[str, int]:
    depths = getattr(_LOCK_DEPTHS, "by_path", None)
    if depths is None:
        depths = {}
        _LOCK_DEPTHS.by_path = depths
    return depths


def _lock_key(lock_path: Path) -> str:
    """Normalize so two spellings of one lock file share a depth entry."""
    try:
        return str(lock_path.resolve())
    except OSError:
        return str(lock_path.absolute())


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
        self.writer_lock_path = self.lock_path
        self.live_order_lock_path = self.path.with_suffix(self.path.suffix + ".live.lock")
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
                "account_id TEXT, "
                "payload TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
            }
            if "account_id" not in columns:
                conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN account_id TEXT")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cash_suspense "
                "(account_id TEXT NOT NULL, currency TEXT NOT NULL, amount REAL NOT NULL, "
                "first_snapshot_id INTEGER, last_snapshot_id INTEGER, "
                "first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL, "
                "candidate_label TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', "
                "incident_id TEXT, resolved_at TEXT, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (account_id, currency))"
            )
            cash_suspense_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(cash_suspense)").fetchall()
            }
            if "incident_id" not in cash_suspense_columns:
                conn.execute("ALTER TABLE cash_suspense ADD COLUMN incident_id TEXT")
            if "resolved_at" not in cash_suspense_columns:
                conn.execute("ALTER TABLE cash_suspense ADD COLUMN resolved_at TEXT")
            conn.execute(
                "UPDATE cash_suspense SET incident_id = "
                "account_id || ':' || currency || ':' || COALESCE(first_snapshot_id, 0) "
                "WHERE incident_id IS NULL"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fill_watermarks "
                "("
                "broker_order_id TEXT PRIMARY KEY, "
                "cumulative_quantity REAL NOT NULL, "
                "cumulative_notional REAL NOT NULL, "
                "cumulative_commission REAL NOT NULL DEFAULT 0, "
                "cumulative_tax REAL NOT NULL DEFAULT 0, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            fill_watermark_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(fill_watermarks)").fetchall()
            }
            if "cumulative_commission" not in fill_watermark_columns:
                conn.execute(
                    "ALTER TABLE fill_watermarks ADD COLUMN "
                    "cumulative_commission REAL NOT NULL DEFAULT 0"
                )
            if "cumulative_tax" not in fill_watermark_columns:
                conn.execute(
                    "ALTER TABLE fill_watermarks ADD COLUMN cumulative_tax REAL NOT NULL DEFAULT 0"
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
            order_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
            orders_contribution_columns_are_new = "contribution_month" not in order_columns
            if "contribution_month" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN contribution_month TEXT")
            if "contribution_sleeve" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN contribution_sleeve TEXT")
            if "execution_sleeve" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN execution_sleeve TEXT")
            if "account_id" not in order_columns:
                conn.execute("ALTER TABLE orders ADD COLUMN account_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_contribution "
                "ON orders(contribution_month, contribution_sleeve) "
                "WHERE contribution_month IS NOT NULL"
            )
            if orders_contribution_columns_are_new:
                self._backfill_orders_contribution_columns(conn)
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
            system_event_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(system_events)").fetchall()
            }
            if "duplicate_key" not in system_event_columns:
                conn.execute("ALTER TABLE system_events ADD COLUMN duplicate_key TEXT")
            if "broker_order_id" not in system_event_columns:
                conn.execute("ALTER TABLE system_events ADD COLUMN broker_order_id TEXT")
            order_id_column_is_new = "order_id" not in system_event_columns
            if order_id_column_is_new:
                conn.execute("ALTER TABLE system_events ADD COLUMN order_id TEXT")
            if "batch_fingerprint" not in system_event_columns:
                # Rows written by save_system_events_atomic carry a stable
                # hash of the batch they were committed as part of (see that
                # method's docstring). Existing rows -- written before this
                # column existed, or by any path other than that method --
                # come back NULL, which is exactly the "no batch provenance"
                # signal the replay check below relies on.
                conn.execute("ALTER TABLE system_events ADD COLUMN batch_fingerprint TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_events_type_created "
                "ON system_events(event_type, created_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_system_events_duplicate_key "
                "ON system_events(duplicate_key) WHERE duplicate_key IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_events_broker_order_id "
                "ON system_events(broker_order_id) WHERE broker_order_id IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_events_order_id "
                "ON system_events(order_id) WHERE order_id IS NOT NULL"
            )
            if order_id_column_is_new:
                self._backfill_system_event_order_ids(conn)
            self._migrate_legacy_baseline_fill_watermarks(conn)
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
                "CREATE TABLE IF NOT EXISTS telegram_ui_card_state "
                "("
                "card_key TEXT NOT NULL, "
                "chat_id INTEGER NOT NULL, "
                "message_id INTEGER, "
                "stage TEXT NOT NULL, "
                "render_hash TEXT NOT NULL, "
                "delivery TEXT NOT NULL, "
                "operation_id TEXT NOT NULL, "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (card_key, chat_id)"
                ")"
            )
            card_state_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(telegram_ui_card_state)").fetchall()
            }
            if "consecutive_failures" not in card_state_columns:
                # CREATE TABLE IF NOT EXISTS leaves an existing table alone, and
                # an earlier commit on this branch already created this one
                # without the counter -- on the operator's database included.
                # Without this the first card write and every telegram_ui health
                # check die with "no such column".
                conn.execute(
                    "ALTER TABLE telegram_ui_card_state "
                    "ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                # Who a card is addressed to, written before its first send.
                # Derived from the copies that exist otherwise, and a copy only
                # exists once a chat has been attempted -- so a process that
                # died halfway through the chat list would leave the untried
                # chats indistinguishable from chats added later, and they
                # would never be sent to.
                "CREATE TABLE IF NOT EXISTS telegram_ui_card_audience "
                "("
                "card_key TEXT NOT NULL, "
                "chat_id INTEGER NOT NULL, "
                "recorded_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (card_key, chat_id)"
                ")"
            )
            conn.execute(
                # The terminal index for card sweeping. A signal run whose cards
                # have all reached a confirmed "done" is recorded here so the
                # sweep can exclude it in SQL instead of loading and re-deciding
                # every approval Maestro has ever dispatched on every poll.
                #
                # Keyed by signal run rather than approval so a daily parent card
                # never sees a partial group. The order and approval ids are kept
                # so a recovery or resolution failure that lands later can find
                # the run again -- see reopen_settled_signal_runs.
                "CREATE TABLE IF NOT EXISTS telegram_ui_settled_run "
                "("
                "signal_run_id TEXT PRIMARY KEY, "
                "approval_ids TEXT NOT NULL, "
                "order_ids TEXT NOT NULL, "
                "max_event_id INTEGER NOT NULL, "
                "settled_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_approval_id "
                "ON approvals(approval_id)"
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
                "CREATE INDEX IF NOT EXISTS idx_broker_snapshots_created_at "
                "ON broker_account_snapshots(created_at)"
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
                "CREATE TABLE IF NOT EXISTS account_attribution_snapshots "
                "("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id TEXT, "
                "account_id TEXT, "
                "symbol TEXT, "
                "bucket_id TEXT, "
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

    def _backfill_orders_contribution_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, payload FROM orders").fetchall()
        updates = []
        for row_id, raw_payload in rows:
            columns = _order_contribution_columns(json.loads(raw_payload))
            if any(value is not None for value in columns):
                updates.append((*columns, row_id))
        if updates:
            conn.executemany(
                "UPDATE orders SET contribution_month = ?, contribution_sleeve = ?, "
                "execution_sleeve = ?, account_id = ? WHERE id = ?",
                updates,
            )

    def _backfill_system_event_order_ids(self, conn: sqlite3.Connection) -> None:
        placeholders = ",".join("?" for _ in _ORDER_ID_BACKFILL_EVENT_TYPES)
        rows = conn.execute(
            "SELECT id, payload FROM system_events "
            f"WHERE event_type IN ({placeholders}) AND order_id IS NULL",
            _ORDER_ID_BACKFILL_EVENT_TYPES,
        ).fetchall()
        updates = []
        for row_id, raw_payload in rows:
            order_id = _system_event_order_id(json.loads(raw_payload))
            if order_id is not None:
                updates.append((order_id, row_id))
        if updates:
            conn.executemany(
                "UPDATE system_events SET order_id = ? WHERE id = ?",
                updates,
            )

    @contextmanager
    def _file_lock(
        self,
        lock_path: Path,
        *,
        owner: str,
        timeout_seconds: float,
        reentrant: bool,
        busy_message: str,
        other_lock: tuple[str, Path] | None = None,
    ) -> Any:
        depths = _lock_depths()
        depth_key = _lock_key(lock_path) if reentrant else None
        if depth_key is not None and depths.get(depth_key, 0) > 0:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        started = time.monotonic()
        # Not a ``with`` block: closing this file must never raise into the
        # caller nor replace a propagating exception (see _close_lock_file).
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            self._busy_diagnostics(
                                busy_message,
                                lock_path=lock_path,
                                owner=owner,
                                other_lock=other_lock,
                                waited_seconds=time.monotonic() - started,
                            )
                        ) from exc
                    time.sleep(0.1)
            self._write_lock_holder(lock_file, owner)
            if depth_key is not None:
                depths[depth_key] = depths.get(depth_key, 0) + 1
            try:
                yield
            finally:
                if depth_key is not None:
                    depths[depth_key] -= 1
                    if depths[depth_key] <= 0:
                        del depths[depth_key]
                self._clear_lock_holder(lock_file)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._close_lock_file(lock_file)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        """Unbuffered write of the whole payload; ``os.write`` may write partially."""
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                break
            view = view[written:]

    @classmethod
    def _write_lock_holder(cls, lock_file: Any, owner: str) -> None:
        """Called only while the exclusive flock is held — no write race.

        This is a diagnostic write. A failure here (e.g. ENOSPC) must never
        prevent the caller from acquiring the lock or change what exception
        acquisition raises, so every exception is swallowed.

        The record is written with unbuffered file-descriptor calls
        (``os.ftruncate``/``os.write``) rather than through the buffered text
        stream: a buffered write that fails at ``flush()`` leaves the buffer
        dirty, and the later ``close()`` — which happens after flock release,
        outside this guard — would flush again and raise ``OSError`` at the
        caller. With no Python-level buffer involved, every I/O error surfaces
        here, where it is swallowed.
        """
        try:
            record = {
                "owner": owner,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            fd = lock_file.fileno()
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            cls._write_all(fd, json.dumps(record, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass

    @staticmethod
    def _clear_lock_holder(lock_file: Any) -> None:
        """Clear before release so the next waiter doesn't see a stale holder.

        This runs in the ``finally`` of the guarded block, so a failure here
        must never replace or mask whatever exception (if any) is already
        propagating out of the caller's business logic. Unbuffered for the same
        reason as _write_lock_holder.
        """
        try:
            os.ftruncate(lock_file.fileno(), 0)
        except Exception:
            pass

    @staticmethod
    def _close_lock_file(lock_file: Any) -> None:
        """Close the lock file so that the close can never disturb the outcome.

        The flock is already released by the time this runs. A ``with`` block
        here would let a close-time error (a dirty buffer flushed at close, or
        EIO on close) escape from outside every guard — replacing a propagating
        trading exception with an ``OSError`` that no caller catches.
        """
        try:
            lock_file.close()
        except Exception:
            pass

    @classmethod
    def _busy_diagnostics(
        cls,
        busy_message: str,
        *,
        lock_path: Path,
        owner: str,
        other_lock: tuple[str, Path] | None,
        waited_seconds: float,
    ) -> str:
        """Build the timeout message. Prefixes are fixed; everything after is diagnostic.

        Every component is independently failure-isolated, so a missing or
        unreadable record only shrinks the message.
        """
        return (
            f"{busy_message} (waiter {owner} pid={os.getpid()}, "
            f"{cls._describe_lock_holder(lock_path)}"
            f"{cls._describe_other_lock(other_lock)}, "
            f"waited {waited_seconds:.1f}s"
            f"{cls._describe_host_pressure()})"
        )

    @classmethod
    def _describe_lock_holder(cls, lock_path: Path) -> str:
        holder = cls.read_lock_holder(lock_path)
        if not holder:
            return "holder unknown"
        return (
            f"holder {holder.get('owner', 'unknown')} "
            f"pid={holder.get('pid', 'unknown')} "
            f"since {holder.get('acquired_at', 'unknown')}"
            f"{cls._describe_hold_age(holder.get('acquired_at'))}"
        )

    @staticmethod
    def _describe_hold_age(acquired_at: Any) -> str:
        """How long the holder has held it — read against the waiter's own wait,
        this separates "one process is doing long work" (hypothesis B: a long
        hold, a short wait behind it) from "the whole box is slow" (hypothesis C:
        waits piling up against a hold that is not unusually long)."""
        try:
            started = datetime.fromisoformat(str(acquired_at))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return f" held {(datetime.now(UTC) - started).total_seconds():.1f}s"
        except Exception:
            return ""

    @staticmethod
    def _describe_host_pressure() -> str:
        """Cheap /proc-only pressure read for hypothesis C. No subprocesses, and
        every read is isolated: an unreadable field just drops out."""
        parts: list[str] = []
        try:
            fields = Path("/proc/loadavg").read_text(encoding="utf-8").split()
            parts.append(f"load1={float(fields[0]):.2f}")
            if len(fields) >= 4 and "/" in fields[3]:
                parts.append(f"runnable={fields[3]}")
        except Exception:
            pass
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    parts.append(f"mem_avail={float(line.split()[1]) / 1024:.1f}MB")
                    break
        except Exception:
            pass
        if not parts:
            return ""
        return ", host " + " ".join(parts)

    @classmethod
    def _describe_other_lock(cls, other_lock: tuple[str, Path] | None) -> str:
        """Describe the paired lock's holder, e.g. for hypothesis A vs B:

        was the writer-lock holder itself blocked on the live-order lock
        (an inversion), or just holding the writer lock a long time? Returns
        "" when there is no meaningful other lock (account_refresh_lock).
        """
        if other_lock is None:
            return ""
        label, lock_path = other_lock
        return f", {label} {cls._describe_lock_holder(lock_path)}"

    @staticmethod
    def read_lock_holder(lock_path: Path) -> dict[str, Any] | None:
        """Read without taking the lock. Diagnostic only — every failure absorbs to None.

        Reads with no lock held, so this can observe a file mid-truncate: a
        multi-byte UTF-8 character can be cut in half, which raises
        ``UnicodeDecodeError`` (a ``ValueError`` subclass) rather than
        ``OSError``. Catching ``(OSError, ValueError)`` around both the read
        and the JSON parse absorbs that alongside ``json.JSONDecodeError``
        (also a ``ValueError`` subclass), so no content-shaped failure can
        escape as an exception.
        """
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    @contextmanager
    def writer_lock(
        self,
        owner: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Any:
        with self._file_lock(
            self.lock_path,
            owner=owner,
            timeout_seconds=timeout_seconds,
            reentrant=True,
            busy_message=f"State writer lock is busy: {self.lock_path}",
            other_lock=("live_order_lock", self.live_order_lock_path),
        ):
            yield

    @contextmanager
    def account_refresh_lock(
        self,
        account_id: str,
        *,
        timeout_seconds: float = 0.0,
    ) -> Any:
        safe_account_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in account_id
        )
        lock_path = self.path.with_suffix(self.path.suffix + f".refresh-{safe_account_id}.lock")
        with self._file_lock(
            lock_path,
            owner=f"account_refresh:{account_id}",
            timeout_seconds=timeout_seconds,
            reentrant=False,
            busy_message=f"Account refresh is already running: {account_id}",
        ):
            yield

    def holds_writer_lock(self) -> bool:
        """Whether this thread currently holds this database's writer lock."""
        return _lock_depths().get(_lock_key(self.lock_path), 0) > 0

    def _assert_live_order_lock_order(self, owner: str) -> None:
        """live_order_lock is the outer lock; writer_lock is only taken under it.

        A thread already holding writer_lock must not take live_order_lock. That
        inversion is what deadlocked the 2026-08-11 and 2026-08-12 US rotations
        against a concurrent process holding the two locks in the agreed order.
        flock is per-process, so such a bug cannot be caught by single-process
        tests and only surfaces as a cross-process hang under production timing.
        Raising here converts it into a loud, local, immediately attributable
        failure at the exact call site that broke the rule.
        """
        depths = _lock_depths()
        if depths.get(_lock_key(self.live_order_lock_path), 0) > 0:
            # Re-entrant: the outermost acquisition already established the order.
            return
        if depths.get(_lock_key(self.lock_path), 0) > 0:
            raise RuntimeError(
                f"Lock order violation: live_order_lock ({owner}) was requested "
                "while this thread already holds writer_lock. Acquire "
                "live_order_lock first, then writer_lock under it."
            )

    @contextmanager
    def live_order_lock(
        self,
        owner: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> Any:
        self._assert_live_order_lock_order(owner)
        with self._file_lock(
            self.live_order_lock_path,
            owner=owner,
            timeout_seconds=timeout_seconds,
            reentrant=True,
            busy_message=f"Live order lock is busy: {self.live_order_lock_path}",
            other_lock=("writer_lock", self.lock_path),
        ):
            yield

    def validate_config_identity(self, identity: ConfigIdentity) -> None:
        payload = identity.model_dump()
        existing = self.load_operator_config_identity()
        if existing is not None and not _same_state_config_identity(existing, payload):
            raise ValueError(
                "State DB config identity mismatch: "
                f"state_db={self.path} existing_path={existing.get('path')} "
                f"existing_fingerprint={existing.get('fingerprint')} "
                f"existing_state_fingerprint={existing.get('state_fingerprint')} "
                f"current_path={payload['path']} current_fingerprint={payload['fingerprint']} "
                f"current_state_fingerprint={payload['state_fingerprint']}"
            )
        if existing != payload:
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
                "SELECT payload FROM portfolio_snapshots "
                "WHERE account_id IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return PortfolioState(
                cash=self.initial_cash,
                cash_by_currency=self.initial_cash_by_currency,
                positions={},
            )
        return PortfolioState.model_validate_json(row[0])

    def load_latest_account_portfolio_state(self, account_id: str) -> PortfolioState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM portfolio_snapshots "
                "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return PortfolioState.model_validate_json(row[0])

    def has_portfolio_snapshot(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM portfolio_snapshots LIMIT 1").fetchone()
        return row is not None

    def save_portfolio_snapshot(
        self,
        run_id: str,
        state: PortfolioState,
        *,
        account_id: str | None = None,
    ) -> None:
        self._insert("portfolio_snapshots", run_id, account_id, state.model_dump(mode="json"))

    def save_portfolio_snapshot_with_event(
        self,
        run_id: str,
        state: PortfolioState,
        *,
        account_id: str,
        event_type: str,
        event_payload: dict[str, Any],
        save_global: bool = False,
    ) -> None:
        """Persist an adopted ledger snapshot and its provenance atomically."""
        state_json = json.dumps(state.model_dump(mode="json"), default=str)
        event_json = json.dumps(event_payload, default=str)
        with self.writer_lock("save_portfolio_snapshot_with_event"):
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO portfolio_snapshots (run_id, account_id, payload) "
                    "VALUES (?, ?, ?)",
                    (run_id, account_id, state_json),
                )
                if save_global:
                    conn.execute(
                        "INSERT INTO portfolio_snapshots (run_id, account_id, payload) "
                        "VALUES (?, NULL, ?)",
                        (run_id, state_json),
                    )
                conn.execute(
                    "INSERT INTO system_events "
                    "(run_id, event_type, payload, duplicate_key, broker_order_id, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        event_type,
                        event_json,
                        _system_event_duplicate_key(event_payload),
                        _system_event_broker_order_id(event_payload),
                        _system_event_order_id(event_payload),
                    ),
                )

    def apply_ledger_bookkeeping_correction(
        self,
        run_id: str,
        *,
        account_id: str,
        currency: str,
        amount: float,
        event_payload: dict[str, Any],
    ) -> bool:
        """Apply an audited non-flow correction to account and global ledgers."""
        duplicate_key = _system_event_duplicate_key(event_payload)
        if not duplicate_key:
            raise ValueError("bookkeeping correction requires a duplicate key")
        normalized_currency = str(currency).upper()
        correction_amount = float(amount)
        if abs(correction_amount) < 1e-12:
            raise ValueError("bookkeeping correction amount must be non-zero")
        with self.writer_lock("apply_ledger_bookkeeping_correction"):
            with self._connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM system_events WHERE duplicate_key = ?",
                    (duplicate_key,),
                ).fetchone():
                    return False
                account_row = conn.execute(
                    "SELECT payload FROM portfolio_snapshots "
                    "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if account_row is None:
                    raise ValueError(f"ledger is not established for account_id={account_id}")

                def corrected(raw_state: str) -> PortfolioState:
                    state = PortfolioState.model_validate(json.loads(raw_state))
                    cash_by_currency = dict(state.cash_by_currency)
                    if not cash_by_currency:
                        cash_by_currency = {normalized_currency: float(state.cash)}
                    cash_by_currency[normalized_currency] = (
                        float(cash_by_currency.get(normalized_currency, 0.0)) + correction_amount
                    )
                    cash = (
                        float(cash_by_currency["KRW"])
                        if "KRW" in cash_by_currency
                        else float(sum(cash_by_currency.values()))
                    )
                    return state.model_copy(
                        update={"cash": cash, "cash_by_currency": cash_by_currency},
                        deep=True,
                    )

                for target_account_id, row in (
                    (account_id, account_row),
                    (
                        None,
                        conn.execute(
                            "SELECT payload FROM portfolio_snapshots "
                            "WHERE account_id IS NULL ORDER BY id DESC LIMIT 1"
                        ).fetchone(),
                    ),
                ):
                    if row is None:
                        continue
                    next_state = corrected(row[0])
                    conn.execute(
                        "INSERT INTO portfolio_snapshots (run_id, account_id, payload) "
                        "VALUES (?, ?, ?)",
                        (
                            run_id,
                            target_account_id,
                            json.dumps(next_state.model_dump(mode="json"), default=str),
                        ),
                    )
                conn.execute(
                    "INSERT INTO system_events "
                    "(run_id, event_type, payload, duplicate_key) VALUES (?, ?, ?, ?)",
                    (
                        run_id,
                        "ledger_bookkeeping_correction",
                        json.dumps(event_payload, default=str),
                        duplicate_key,
                    ),
                )
                return True

    def apply_account_cash_flows(
        self,
        run_id: str,
        legs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply linked cash-flow legs to their account ledgers in one transaction.

        Broker snapshots are never used as a cash source here.  The method
        advances the latest account-scoped ledger state and leaves the global
        portfolio snapshot untouched; fills continue to update both views via
        ``apply_fill_reconciliation``.

        A conversion or an account-to-account transfer only means anything as a
        set: money leaves one side and arrives on the other.  Writing the legs
        separately permits a state where one side moved and the other did not,
        which reads as money appearing or vanishing.  Ledger rows and the events
        that explain them are therefore all written together, so a crash leaves
        the ledger exactly where it started and the retry is safe.

        Each leg is ``{account_id, amount, currency, event_payload}`` with a
        signed amount.  Legs are grouped per account so an account advances
        through one ledger state carrying every currency delta applied to it.
        """
        if not legs:
            raise ValueError("at least one cash-flow leg is required")
        prepared = [
            {
                "account_id": str(leg["account_id"]),
                "amount": float(leg["amount"]),
                "currency": str(leg["currency"]).upper(),
                "payload": leg["event_payload"],
                "duplicate_key": _system_event_duplicate_key(leg["event_payload"]),
            }
            for leg in legs
        ]
        duplicate_keys = [leg["duplicate_key"] for leg in prepared if leg["duplicate_key"]]
        if len(prepared) > 1 and len(duplicate_keys) != len(prepared):
            raise ValueError("every linked cash-flow leg needs a duplicate key")
        if len(set(duplicate_keys)) != len(duplicate_keys):
            raise ValueError("linked cash-flow leg duplicate keys must be unique")
        with self.writer_lock("apply_account_cash_flows"):
            with self._connect() as conn:
                states: dict[str, PortfolioState] = {}
                for account_id in dict.fromkeys(leg["account_id"] for leg in prepared):
                    row = conn.execute(
                        "SELECT payload FROM portfolio_snapshots "
                        "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                        (account_id,),
                    ).fetchone()
                    if row is None:
                        return {
                            "ledger_established": False,
                            "missing_account_id": account_id,
                            "created": False,
                            "run_id": "",
                        }
                    states[account_id] = PortfolioState.model_validate(json.loads(row[0]))
                if duplicate_keys:
                    existing = conn.execute(
                        "SELECT duplicate_key, run_id FROM system_events "
                        f"WHERE duplicate_key IN ({','.join('?' * len(duplicate_keys))})",
                        duplicate_keys,
                    ).fetchall()
                    if len(existing) == len(duplicate_keys):
                        return {
                            "ledger_established": True,
                            "missing_account_id": None,
                            "created": False,
                            "run_id": str(existing[0][1] or ""),
                        }
                    if existing:
                        # Half of this set is already on the ledger.  Applying the
                        # rest would complete a transfer the operator never asked
                        # for twice over, so refuse rather than guess.
                        raise ValueError(
                            "cash-flow legs conflict with an existing partial record: "
                            f"{sorted(str(row[0]) for row in existing)}"
                        )
                for account_id, state in states.items():
                    cash_by_currency = dict(state.cash_by_currency)
                    for leg in prepared:
                        if leg["account_id"] != account_id:
                            continue
                        currency = leg["currency"]
                        if not cash_by_currency:
                            cash_by_currency = {currency: float(state.cash)}
                        cash_by_currency[currency] = (
                            float(cash_by_currency.get(currency, 0.0)) + leg["amount"]
                        )
                    if "KRW" in cash_by_currency:
                        cash = float(cash_by_currency["KRW"])
                    else:
                        cash = float(sum(cash_by_currency.values()))
                    next_state = state.model_copy(
                        update={"cash": cash, "cash_by_currency": cash_by_currency},
                        deep=True,
                    )
                    conn.execute(
                        "INSERT INTO portfolio_snapshots (run_id, account_id, payload) "
                        "VALUES (?, ?, ?)",
                        (
                            run_id,
                            account_id,
                            json.dumps(next_state.model_dump(mode="json"), default=str),
                        ),
                    )
                for leg in prepared:
                    conn.execute(
                        "INSERT INTO system_events "
                        "(run_id, event_type, payload, duplicate_key) VALUES (?, ?, ?, ?)",
                        (
                            run_id,
                            "account_cash_flow",
                            json.dumps(leg["payload"], default=str),
                            leg["duplicate_key"],
                        ),
                    )
                for account_id, currency in {
                    (leg["account_id"], leg["currency"])
                    for leg in prepared
                    if str(leg["payload"].get("flow_class") or "") == "fx_conversion"
                }:
                    conn.execute(
                        "UPDATE cash_suspense SET candidate_label = 'fx_conversion', "
                        "status = 'classified', updated_at = CURRENT_TIMESTAMP "
                        "WHERE account_id = ? AND currency = ?",
                        (account_id, currency),
                    )
                return {
                    "ledger_established": True,
                    "missing_account_id": None,
                    "created": True,
                    "run_id": run_id,
                }

    def save_system_events_atomic(
        self,
        run_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        require_duplicate_keys: Sequence[str] = (),
        forbid_duplicate_keys: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Commit several system events in one transaction, or none of them.

        A state transition that spans more than one event — a new request, the
        previous one superseded, the workflow head that points at the new one —
        only means anything as a set.  Writing them separately permits a crash
        that leaves a request nothing points to, or a head pointing at nothing.

        Every event must carry a ``duplicate_key``.  The keys are what make a
        retry safe: if all of them are already on record this call is a replay
        of a batch that already landed, and it returns without writing.  The
        key identifies this transition, not the event's role in one: a head
        write needs a fresh key each time it moves (``head:wf:v1``, then
        ``head:wf:v2``, ...) — reusing a stable ``head:wf`` key across
        transitions makes every transition after the first look like a
        conflicting partial overlap with the one before it.

        ``require_duplicate_keys`` and ``forbid_duplicate_keys`` are evaluated
        inside the same transaction as the inserts, which is the whole point:
        reading the workflow head and then writing a claim in a separate
        statement leaves a window for another run to replace the head in
        between.  This is why the transaction is opened with ``BEGIN
        IMMEDIATE`` before the very first ``SELECT``: the default sqlite3
        behavior only opens a transaction implicitly before the first write,
        which would let every check run in autocommit mode and release before
        the insert loop even starts — a window a manual recovery script, a
        different-version process, or the sqlite3 CLI could write into, since
        ``writer_lock`` is only an advisory lock that other tools have no
        reason to know about.  Holding the write lock from the first
        statement closes that window; a non-cooperative writer contending for
        it waits up to the connection's ``busy_timeout`` and then raises
        ``sqlite3.OperationalError``, which is the correct loud failure here.
        A batch whose own keys are all present is a replay and is reported as
        such before anything else is consulted.

        Do not call this method while already holding an open write
        transaction on another connection against the same database — the
        ``BEGIN IMMEDIATE`` above would then block against that other
        transaction until ``busy_timeout`` expires, including a self-deadlock
        if it's the same logical caller.  ``writer_lock`` is re-entrant within
        a thread, but that re-entrancy only covers the advisory file lock; it
        does not extend to a second, already-open SQLite transaction.

        A replay is verified by content, not just by key: every one of a
        batch's ``duplicate_key``s existing is not proof this exact batch
        landed, since ``duplicate_key`` is one global namespace shared with
        other write paths (``save_system_event``, ``_insert``, and several
        bespoke methods).  Each key's stored ``event_type`` and payload are
        therefore compared against what's being submitted now (excluding the
        stored ``run_id``, since a legitimate retry after a crash may carry a
        different one).  Stored payloads round-tripped through
        ``json.dumps(..., default=str)`` are compared as parsed structures,
        never as raw text, and the submitted payload is round-tripped the
        same way before comparing, so a datetime stored as a string still
        matches itself.  If every key matches, this is a genuine replay.  If
        any key's stored content differs, this is an unexplained collision
        and raises, in the same spirit as the partial-overlap refusal below —
        which also means a payload with a nondeterministic field (a fresh
        timestamp, a random id) will fail hard on retry instead of silently
        "succeeding": a key that doesn't identify its content isn't a usable
        idempotency key.

        Matching content is still not proof this batch was ever committed as
        one call, though: two events with identical content can land under
        the same keys via two separate calls into that same shared
        namespace — e.g. two ``save_system_event`` calls in two different
        transactions, under two different ``run_id``s — and each would pass
        the content check above despite never having been committed
        together.  To catch that, every call computes a ``batch_fingerprint``
        — a sha256 hash of the batch's own ``(event_type, normalized
        payload)`` pairs, sorted by ``duplicate_key`` so the caller's listing
        order never changes it — and stamps it on every row the call writes.
        Once every key is confirmed present with matching content, every one
        of those rows' stored ``batch_fingerprint`` must equal the one this
        call just computed for its own batch.  A row with a ``NULL``
        fingerprint (written before this column existed, or by a path that
        doesn't set it) or a different one (written by a different atomic
        batch that happened to reuse this key's content) is not a replay of
        *this* batch, and raises — even though its content matched.  Only
        when every row agrees on both content and batch identity is
        ``already_committed`` correct.

        Preconditions are checked next, before this batch's own keys are
        checked for a partial overlap with what is already on record.  A
        declared precondition is the caller stating what "someone else got
        here first" looks like for this transition — when a race is lost, the
        losing batch's own key set legitimately overlaps the winner's (e.g.
        both raced to write the same CAS target key), and that overlap must
        be reported as ``precondition_present``/``precondition_missing``, not
        raised as an unexplained collision.  Only once preconditions clear
        does an own-key partial overlap fall back to the hard ``ValueError``:
        for a caller that declared no preconditions, that overlap really is
        an unexplained key collision.
        """
        prepared = _prepare_atomic_system_events(events)
        keys = [item["duplicate_key"] for item in prepared]
        batch_fingerprint = _compute_batch_fingerprint(prepared)
        with self.writer_lock("save_system_events_atomic"):
            with self._connect() as conn:
                # Manual transaction control: take the write lock (BEGIN
                # IMMEDIATE) before the first SELECT so every check below and
                # the insert loop run inside one held lock. isolation_level
                # must be set to None first -- otherwise pysqlite's own
                # autobegin would try to open a second, nested transaction of
                # its own ahead of the first INSERT/UPDATE/DELETE, which
                # raises. ``with self._connect() as conn:`` still commits on
                # a clean return and rolls back on any exception, including
                # an early return on a conflict path below.
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                existing = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT duplicate_key FROM system_events "
                        f"WHERE duplicate_key IN ({','.join('?' * len(keys))})",
                        keys,
                    ).fetchall()
                }
                if len(existing) == len(keys):
                    mismatched = _find_replay_content_mismatches(conn, prepared)
                    if mismatched:
                        raise ValueError(
                            "atomic system events conflict with an existing record "
                            f"with different content: {sorted(mismatched)}"
                        )
                    provenance_mismatched = _find_replay_provenance_mismatches(
                        conn, keys, batch_fingerprint
                    )
                    if provenance_mismatched:
                        raise ValueError(
                            "atomic system events conflict with an existing record "
                            "not committed as part of this atomic batch (missing or "
                            f"different batch provenance): {sorted(provenance_mismatched)}"
                        )
                    return {
                        "committed": False,
                        "conflict": "already_committed",
                        "conflicting_keys": tuple(sorted(existing)),
                    }
                required = [str(key) for key in require_duplicate_keys]
                if required:
                    present = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT duplicate_key FROM system_events "
                            f"WHERE duplicate_key IN ({','.join('?' * len(required))})",
                            required,
                        ).fetchall()
                    }
                    missing = [key for key in required if key not in present]
                    if missing:
                        return {
                            "committed": False,
                            "conflict": "precondition_missing",
                            "conflicting_keys": tuple(sorted(set(missing))),
                        }
                forbidden = [str(key) for key in forbid_duplicate_keys]
                if forbidden:
                    blocking = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT duplicate_key FROM system_events "
                            f"WHERE duplicate_key IN ({','.join('?' * len(forbidden))})",
                            forbidden,
                        ).fetchall()
                    ]
                    if blocking:
                        return {
                            "committed": False,
                            "conflict": "precondition_present",
                            "conflicting_keys": tuple(sorted(blocking)),
                        }
                if existing:
                    # Half of this transition is already on record, and no
                    # declared precondition explains it.  Committing the rest
                    # would finish a transition nobody asked for, so refuse
                    # rather than guess which half is authoritative.
                    raise ValueError(
                        "atomic system events conflict with an existing partial "
                        f"record: {sorted(existing)}"
                    )
                for item in prepared:
                    conn.execute(
                        "INSERT INTO system_events "
                        "(run_id, event_type, payload, duplicate_key, "
                        "broker_order_id, order_id, batch_fingerprint) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            item["event_type"],
                            json.dumps(item["payload"], default=str),
                            item["duplicate_key"],
                            item["broker_order_id"],
                            item["order_id"],
                            batch_fingerprint,
                        ),
                    )
                return {"committed": True, "conflict": None, "conflicting_keys": ()}

    def upsert_cash_suspense(
        self,
        *,
        account_id: str,
        currency: str,
        amount: float,
        snapshot_id: int | None,
        observed_at: str,
        candidate_label: str = "unexplained",
    ) -> dict[str, Any]:
        normalized_currency = str(currency).upper()
        incident_id = f"{account_id}:{normalized_currency}:{snapshot_id or 0}:{observed_at}"
        with self.writer_lock("upsert_cash_suspense"):
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    "SELECT * FROM cash_suspense WHERE account_id = ? AND currency = ?",
                    (account_id, normalized_currency),
                ).fetchone()
                starts_new_incident = existing is None or existing["status"] == "resolved"
                if existing is not None and existing["status"] == "classified":
                    previous = float(existing["amount"])
                    material_change = abs(float(amount) - previous) > max(
                        0.01 if normalized_currency != "KRW" else 1.0,
                        abs(previous) * 0.1,
                    )
                    starts_new_incident = material_change
                if starts_new_incident:
                    conn.execute(
                        "INSERT INTO cash_suspense "
                        "(account_id, currency, amount, first_snapshot_id, last_snapshot_id, "
                        "first_observed_at, last_observed_at, candidate_label, status, "
                        "incident_id, resolved_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL, CURRENT_TIMESTAMP) "
                        "ON CONFLICT(account_id, currency) DO UPDATE SET "
                        "amount = excluded.amount, "
                        "first_snapshot_id = excluded.first_snapshot_id, "
                        "last_snapshot_id = excluded.last_snapshot_id, "
                        "first_observed_at = excluded.first_observed_at, "
                        "last_observed_at = excluded.last_observed_at, "
                        "candidate_label = excluded.candidate_label, "
                        "status = 'open', incident_id = excluded.incident_id, "
                        "resolved_at = NULL, updated_at = CURRENT_TIMESTAMP",
                        (
                            account_id,
                            normalized_currency,
                            float(amount),
                            snapshot_id,
                            snapshot_id,
                            observed_at,
                            observed_at,
                            candidate_label,
                            incident_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE cash_suspense SET amount = ?, last_snapshot_id = ?, "
                        "last_observed_at = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE account_id = ? AND currency = ?",
                        (
                            float(amount),
                            snapshot_id,
                            observed_at,
                            account_id,
                            normalized_currency,
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM cash_suspense WHERE account_id = ? AND currency = ?",
                    (account_id, normalized_currency),
                ).fetchone()
                return dict(row)

    def resolve_cash_suspense(
        self,
        *,
        account_id: str,
        currency: str,
        snapshot_id: int | None,
        resolved_at: str,
    ) -> dict[str, Any] | None:
        normalized_currency = str(currency).upper()
        with self.writer_lock("resolve_cash_suspense"):
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    "SELECT * FROM cash_suspense WHERE account_id = ? AND currency = ?",
                    (account_id, normalized_currency),
                ).fetchone()
                if existing is None or existing["status"] == "resolved":
                    return None
                conn.execute(
                    "UPDATE cash_suspense SET status = 'resolved', resolved_at = ?, "
                    "last_snapshot_id = COALESCE(?, last_snapshot_id), "
                    "last_observed_at = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE account_id = ? AND currency = ?",
                    (
                        resolved_at,
                        snapshot_id,
                        resolved_at,
                        account_id,
                        normalized_currency,
                    ),
                )
                return dict(
                    conn.execute(
                        "SELECT * FROM cash_suspense WHERE account_id = ? AND currency = ?",
                        (account_id, normalized_currency),
                    ).fetchone()
                )

    def classify_cash_suspense(
        self,
        *,
        account_id: str,
        currency: str,
        classification: str,
    ) -> bool:
        with self.writer_lock("classify_cash_suspense"):
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE cash_suspense SET candidate_label = ?, status = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE account_id = ? AND currency = ?",
                    (
                        classification,
                        "classified",
                        account_id,
                        str(currency).upper(),
                    ),
                )
                return cursor.rowcount > 0

    def list_cash_suspense(self, *, account_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM cash_suspense"
        params: tuple[Any, ...] = ()
        if account_id is not None:
            query += " WHERE account_id = ?"
            params = (account_id,)
        query += " ORDER BY account_id, currency"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def apply_broker_order_history_delta(
        self,
        run_id: str,
        *,
        account_id: str,
        broker_order_id: str,
        symbol: str,
        side: str,
        currency: str,
        cumulative_quantity: float,
        cumulative_notional: float,
        cumulative_commission: float | None = None,
        cumulative_tax: float | None = None,
        quantity_in_baseline: bool = False,
        principal_in_baseline: bool = False,
        costs_in_baseline: bool = False,
    ) -> dict[str, float | bool]:
        """Apply an idempotent broker-history fill/cost delta to the ledger.

        Baseline flags suppress only the first observed cumulative values while
        still seeding their watermarks. Later increases are therefore applied
        normally instead of being hidden by an order timestamp before the
        baseline.
        """
        normalized_side = str(side).lower()
        signed = 1.0 if normalized_side == "buy" else -1.0
        with self.writer_lock("apply_broker_order_history"):
            with self._connect() as conn:
                account_row = conn.execute(
                    "SELECT payload FROM portfolio_snapshots "
                    "WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if account_row is None:
                    return {
                        "applied": False,
                        "quantity_delta": 0.0,
                        "notional_delta": 0.0,
                        "cost_delta": 0.0,
                    }
                watermark = conn.execute(
                    "SELECT cumulative_quantity, cumulative_notional, "
                    "cumulative_commission, cumulative_tax "
                    "FROM fill_watermarks WHERE broker_order_id = ?",
                    (broker_order_id,),
                ).fetchone()
                previous_quantity = float(watermark[0]) if watermark else 0.0
                previous_notional = float(watermark[1]) if watermark else 0.0
                previous_commission = float(watermark[2]) if watermark else 0.0
                previous_tax = float(watermark[3]) if watermark else 0.0
                next_commission = (
                    max(previous_commission, float(cumulative_commission))
                    if cumulative_commission is not None
                    else previous_commission
                )
                next_tax = (
                    max(previous_tax, float(cumulative_tax))
                    if cumulative_tax is not None
                    else previous_tax
                )
                first_observation = watermark is None
                quantity_delta = max(float(cumulative_quantity) - previous_quantity, 0.0)
                notional_delta = max(float(cumulative_notional) - previous_notional, 0.0)
                cost_delta = (next_commission - previous_commission) + (next_tax - previous_tax)
                if first_observation and quantity_in_baseline:
                    quantity_delta = 0.0
                if first_observation and principal_in_baseline:
                    notional_delta = 0.0
                if first_observation and costs_in_baseline:
                    cost_delta = 0.0
                changed = quantity_delta > 1e-12 or notional_delta > 1e-12 or cost_delta > 1e-12
                if changed:
                    normalized_currency = str(currency).upper()

                    def advance_state(raw_state: str) -> PortfolioState:
                        state = PortfolioState.model_validate(json.loads(raw_state))
                        cash_by_currency = dict(state.cash_by_currency)
                        if not cash_by_currency:
                            cash_by_currency = {normalized_currency: float(state.cash)}
                        cash_by_currency[normalized_currency] = (
                            float(cash_by_currency.get(normalized_currency, 0.0))
                            - signed * notional_delta
                            - cost_delta
                        )
                        positions = dict(state.positions)
                        next_quantity = positions.get(symbol, 0.0) + signed * quantity_delta
                        if abs(next_quantity) < 1e-12:
                            positions.pop(symbol, None)
                        else:
                            positions[symbol] = next_quantity
                        cash = (
                            float(cash_by_currency["KRW"])
                            if "KRW" in cash_by_currency
                            else float(sum(cash_by_currency.values()))
                        )
                        return state.model_copy(
                            update={
                                "cash": cash,
                                "cash_by_currency": cash_by_currency,
                                "positions": positions,
                            },
                            deep=True,
                        )

                    next_state = advance_state(account_row[0])
                    for target_account_id, raw_state in [
                        (account_id, account_row[0]),
                        (
                            None,
                            (
                                conn.execute(
                                    "SELECT payload FROM portfolio_snapshots "
                                    "WHERE account_id IS NULL ORDER BY id DESC LIMIT 1"
                                ).fetchone()
                                or [None]
                            )[0],
                        ),
                    ]:
                        if raw_state is None:
                            continue
                        target_state = (
                            next_state
                            if target_account_id == account_id
                            else advance_state(raw_state)
                        )
                        conn.execute(
                            "INSERT INTO portfolio_snapshots (run_id, account_id, payload) "
                            "VALUES (?, ?, ?)",
                            (
                                run_id,
                                target_account_id,
                                json.dumps(target_state.model_dump(mode="json"), default=str),
                            ),
                        )
                conn.execute(
                    "INSERT INTO fill_watermarks "
                    "(broker_order_id, cumulative_quantity, cumulative_notional, "
                    "cumulative_commission, cumulative_tax, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(broker_order_id) DO UPDATE SET "
                    "cumulative_quantity = MAX(fill_watermarks.cumulative_quantity, "
                    "excluded.cumulative_quantity), "
                    "cumulative_notional = MAX(fill_watermarks.cumulative_notional, "
                    "excluded.cumulative_notional), "
                    "cumulative_commission = MAX(fill_watermarks.cumulative_commission, "
                    "excluded.cumulative_commission), "
                    "cumulative_tax = MAX(fill_watermarks.cumulative_tax, "
                    "excluded.cumulative_tax), "
                    "updated_at = CURRENT_TIMESTAMP",
                    (
                        broker_order_id,
                        max(previous_quantity, float(cumulative_quantity)),
                        max(previous_notional, float(cumulative_notional)),
                        next_commission,
                        next_tax,
                    ),
                )
                return {
                    "applied": changed,
                    "quantity_delta": quantity_delta,
                    "notional_delta": notional_delta,
                    "cost_delta": cost_delta,
                }

    def save_strategy_run(self, run_id: str, strategy_id: str, payload: dict[str, Any]) -> None:
        self._insert("strategy_runs", run_id, strategy_id, payload)

    def save_order(self, run_id: str, order_id: str, payload: dict[str, Any]) -> None:
        self._insert("orders", run_id, order_id, payload)

    def save_system_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._insert("system_events", run_id, event_type, payload)

    def record_card_event(self, run_id: str, payload: dict[str, Any]) -> None:
        """Append a Telegram card event and update its projection atomically.

        The event log is the history phase 3a-3 will need to reconstruct
        attempts; the projection is the current state the sweep reads. Writing
        them separately would let a crash leave the projection stale, and a
        stale projection is the duplicate-card bug in another form --
        ``writer_lock`` is an advisory flock and shares no transaction, so
        wrapping two calls in it would not help.

        Reconstructing current state by scanning recent events instead cannot
        be made correct: events accrue on every sweep, so a card that has
        waited long enough always has its last result pushed past any limit.
        """
        payload_json = json.dumps(payload, default=str)
        with self.writer_lock("record_card_event"):
            with self._connect() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO system_events (run_id, event_type, payload, duplicate_key) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, "telegram_ui_card", payload_json, payload.get("duplicate_key")),
                )
                conn.execute(
                    "INSERT INTO telegram_ui_card_state "
                    "(card_key, chat_id, message_id, stage, render_hash, delivery, "
                    "operation_id, consecutive_failures, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(card_key, chat_id) DO UPDATE SET "
                    "message_id=COALESCE("
                    "excluded.message_id, telegram_ui_card_state.message_id), "
                    "stage=excluded.stage, render_hash=excluded.render_hash, "
                    "delivery=excluded.delivery, operation_id=excluded.operation_id, "
                    # Counted here rather than by scanning the log: the run of
                    # failures is state, and reconstructing it from recent
                    # events hits the same window problem as the copy itself.
                    "consecutive_failures=CASE excluded.delivery "
                    "WHEN 'failed' THEN telegram_ui_card_state.consecutive_failures + 1 "
                    "WHEN 'confirmed' THEN 0 "
                    "ELSE telegram_ui_card_state.consecutive_failures END, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (
                        str(payload["card_key"]),
                        int(payload["chat_id"]),
                        payload.get("message_id"),
                        str(payload["stage"]),
                        str(payload["render_hash"]),
                        str(payload["delivery"]),
                        str(payload["operation_id"]),
                        1 if str(payload["delivery"]) == "failed" else 0,
                    ),
                )

    def load_card_delivery_state(self, card_key: str) -> list[dict[str, Any]]:
        """Current state of every delivery copy of one card. No limit, no scan."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT card_key, chat_id, message_id, stage, render_hash, delivery, "
                "operation_id, consecutive_failures FROM telegram_ui_card_state "
                "WHERE card_key = ? ORDER BY chat_id",
                (card_key,),
            ).fetchall()
        return [
            {
                "card_key": str(row[0]),
                "chat_id": int(row[1]),
                "message_id": row[2],
                "stage": str(row[3]),
                "render_hash": str(row[4]),
                "delivery": str(row[5]),
                "operation_id": str(row[6]),
                "consecutive_failures": int(row[7]),
            }
            for row in rows
        ]

    def list_failing_card_copies(
        self,
        min_consecutive_failures: int,
        chat_ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Delivery copies whose sends keep being refused, worst first.

        Read from the projection rather than the event log so it heals itself:
        record_card_event resets the counter on a confirmed send, whereas a
        count over past failure events would leave health degraded forever
        after one bad spell.

        ``chat_ids`` narrows the answer to the chats still being delivered to.
        Self-healing only works for a copy that can still succeed: once the
        operator removes a failing chat from the configuration, nothing will
        ever send to it again, so its counter is frozen and an unfiltered
        count holds telegram_ui in warn for the life of the database.
        """
        sql = (
            "SELECT card_key, chat_id, consecutive_failures FROM telegram_ui_card_state "
            "WHERE consecutive_failures >= ?"
        )
        values: list[Any] = [int(min_consecutive_failures)]
        if chat_ids is not None:
            if not chat_ids:
                return []
            sql += f" AND chat_id IN ({','.join('?' * len(chat_ids))})"
            values.extend(int(chat_id) for chat_id in chat_ids)
        sql += " ORDER BY consecutive_failures DESC, card_key"
        with self._connect() as conn:
            rows = conn.execute(sql, values).fetchall()
        return [
            {
                "card_key": str(row[0]),
                "chat_id": int(row[1]),
                "consecutive_failures": int(row[2]),
            }
            for row in rows
        ]

    def record_card_audience(self, card_key: str, chat_ids: Iterable[int]) -> None:
        """Note the chats a card is addressed to. First writer wins.

        Called before the first send, so a crash partway through the chat list
        leaves a record of who was still owed a copy. Existing rows are left
        alone: the audience is a property of the card at creation, and later
        changes to the configured chats must not rewrite it.
        """
        values = [(str(card_key), int(chat_id)) for chat_id in chat_ids]
        if not values:
            return
        with self.writer_lock("record_card_audience"):
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO telegram_ui_card_audience (card_key, chat_id) "
                    "VALUES (?, ?)",
                    values,
                )

    def load_card_audience(self, card_key: str) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM telegram_ui_card_audience "
                "WHERE card_key = ? ORDER BY chat_id",
                (str(card_key),),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def list_unsettled_pending_approvals(self) -> list[dict[str, Any]]:
        """Pending-approval events whose cards may still need work.

        The card sweep runs on every poll and the event log only grows, so
        deciding which cards are finished in Python means parsing every
        approval ever dispatched and reading one projection row per approval,
        every time -- callback polling gets slower with each operating day.
        Excluding settled runs here keeps the work proportional to what is
        actually open.

        A run comes back on its own when a *later* approval joins it: the new
        event's id is above the ``max_event_id`` recorded when the run
        settled. It comes back **whole** -- returning only the new event would
        leave the sweep seeing a one-group run, and the daily parent card
        needs two, so a run that gained its second group after settling would
        never get one. Recovery and resolution failures carry no such id and
        are handled by ``reopen_settled_signal_runs`` instead.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "WITH reopened AS ("
                "  SELECT settled.signal_run_id AS signal_run_id "
                "  FROM telegram_ui_settled_run AS settled "
                "  JOIN system_events AS newer "
                "    ON json_extract(newer.payload, '$.signal_run_id') = settled.signal_run_id "
                "  WHERE newer.event_type = 'telegram_approval_pending' "
                "    AND newer.id > settled.max_event_id"
                ") "
                "SELECT events.id AS id, events.payload AS payload FROM system_events AS events "
                "LEFT JOIN telegram_ui_settled_run AS settled "
                "ON settled.signal_run_id = json_extract(events.payload, '$.signal_run_id') "
                "WHERE events.event_type = 'telegram_approval_pending' "
                "AND (settled.signal_run_id IS NULL "
                "     OR settled.signal_run_id IN (SELECT signal_run_id FROM reopened)) "
                "ORDER BY events.id DESC"
            ).fetchall()
        return [{"id": int(row["id"]), "payload": json.loads(row["payload"])} for row in rows]

    def mark_signal_run_cards_settled(
        self,
        signal_run_id: str,
        *,
        approval_ids: Sequence[str],
        order_ids: Sequence[str],
        max_event_id: int,
    ) -> None:
        """Record that every card of one signal run is delivered and done."""
        with self.writer_lock("mark_signal_run_cards_settled"):
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO telegram_ui_settled_run "
                    "(signal_run_id, approval_ids, order_ids, max_event_id, settled_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(signal_run_id) DO UPDATE SET "
                    "approval_ids=excluded.approval_ids, order_ids=excluded.order_ids, "
                    "max_event_id=MAX(excluded.max_event_id, "
                    "telegram_ui_settled_run.max_event_id), "
                    "settled_at=CURRENT_TIMESTAMP",
                    (
                        str(signal_run_id),
                        json.dumps(sorted({str(value) for value in approval_ids})),
                        json.dumps(sorted({str(value) for value in order_ids})),
                        int(max_event_id),
                    ),
                )

    def has_settled_signal_runs(self) -> bool:
        """Whether anything could need reopening at all."""
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM telegram_ui_settled_run LIMIT 1").fetchone()
        return row is not None

    def reopen_settled_signal_runs(self, order_ids: Iterable[str]) -> int:
        """Un-settle runs touched by an unresolved recovery or a failed resolution.

        "done" is terminal for progress but not for attention: a recovery
        raised days after a rotation completed has to reach that rotation's
        card. Dropping the row is what puts the run back in the sweep's scan,
        and the card's stage is then recomputed from scratch as always.

        The failed-resolution half is decided here rather than passed in.
        Finding it in Python means deserialising every resolution failure and
        every completion ever recorded, on every poll, to answer a question
        about a handful of settled rows -- which is the cost the terminal index
        exists to remove. A failure that a later completion resolved is not a
        reason to reopen: doing so deletes and rewrites the row on every poll
        forever, for exactly the runs that once went wrong.
        """
        order_id_list = sorted({str(value) for value in order_ids})
        clauses = [
            "EXISTS ("
            "  SELECT 1 FROM json_each(telegram_ui_settled_run.approval_ids) AS wanted "
            "  JOIN system_events AS failure "
            "    ON failure.event_type = 'telegram_approval_resolution_failed' "
            "   AND json_extract(failure.payload, '$.approval_id') = wanted.value "
            "  WHERE NOT EXISTS ("
            "    SELECT 1 FROM system_events AS completion "
            "    WHERE completion.event_type = 'signal_approval_completed' "
            "      AND json_extract(completion.payload, '$.approval_id') = wanted.value"
            "  )"
            ")"
        ]
        values: list[Any] = []
        if order_id_list:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(telegram_ui_settled_run.order_ids) "
                f"WHERE json_each.value IN ({','.join('?' * len(order_id_list))}))"
            )
            values.extend(order_id_list)
        with self.writer_lock("reopen_settled_signal_runs"):
            with self._connect() as conn:
                cursor = conn.execute(
                    f"DELETE FROM telegram_ui_settled_run WHERE {' OR '.join(clauses)}",
                    values,
                )
                return int(cursor.rowcount)

    def latest_payloads_by_approval_id(
        self, event_type: str, approval_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Latest payload per approval, for the approvals asked about.

        Scoped in SQL rather than by folding the whole event type in Python:
        the card sweep only ever needs the approvals still open, and reading
        the rest is work that grows with every operating day.
        """
        wanted = sorted({str(value) for value in approval_ids})
        if not wanted:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM system_events "
                "WHERE event_type = ? "
                f"AND json_extract(payload, '$.approval_id') IN ({','.join('?' * len(wanted))}) "
                "ORDER BY id",
                [event_type, *wanted],
            ).fetchall()
        # Ascending, so the last write for an approval is the one that stands.
        payloads: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row[0])
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, str) and approval_id:
                payloads[approval_id] = payload
        return payloads

    def load_fill_watermarks(self) -> dict[str, tuple[float, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT broker_order_id, cumulative_quantity, cumulative_notional "
                "FROM fill_watermarks"
            ).fetchall()
        return {str(row[0]): (float(row[1]), float(row[2])) for row in rows}

    def load_fill_cost_watermarks(self) -> dict[str, tuple[float, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT broker_order_id, cumulative_commission, cumulative_tax FROM fill_watermarks"
            ).fetchall()
        return {str(row[0]): (float(row[1]), float(row[2])) for row in rows}

    def seed_fill_watermarks_from_events(self) -> None:
        if self.load_fill_watermarks():
            return
        with self.writer_lock("seed_fill_watermarks_from_events"):
            with self._connect() as conn:
                existing = conn.execute("SELECT 1 FROM fill_watermarks LIMIT 1").fetchone()
                if existing is not None:
                    return
                rows = conn.execute(
                    "SELECT payload FROM system_events WHERE event_type = ? ORDER BY id ASC",
                    ("fill_reconciliation",),
                ).fetchall()
                watermarks: dict[str, tuple[float, float]] = {}
                for row in rows:
                    payload = json.loads(row[0])
                    for item in payload.get("applied_fills", []):
                        broker_order_id = str(item.get("broker_order_id") or "")
                        if not broker_order_id:
                            continue
                        watermarks[broker_order_id] = (
                            float(item.get("cumulative_filled_quantity", 0.0)),
                            float(item.get("cumulative_filled_notional", 0.0)),
                        )
                self._upsert_fill_watermarks(conn, watermarks)

    def apply_fill_reconciliation(
        self,
        run_id: str,
        state: PortfolioState,
        watermarks: dict[str, tuple[float, float]],
        event_payload: dict[str, Any],
        *,
        account_states: dict[str, PortfolioState] | None = None,
        cost_watermarks: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        payload_json = json.dumps(event_payload, default=str)
        with self.writer_lock("apply_fill_reconciliation"):
            with self._connect() as conn:
                if event_payload.get("portfolio_updated"):
                    conn.execute(
                        "INSERT INTO portfolio_snapshots "
                        "(run_id, account_id, payload) VALUES (?, ?, ?)",
                        (run_id, None, json.dumps(state.model_dump(mode="json"), default=str)),
                    )
                    for account_id, account_state in (account_states or {}).items():
                        conn.execute(
                            "INSERT INTO portfolio_snapshots "
                            "(run_id, account_id, payload) VALUES (?, ?, ?)",
                            (
                                run_id,
                                account_id,
                                json.dumps(
                                    account_state.model_dump(mode="json"),
                                    default=str,
                                ),
                            ),
                        )
                self._upsert_fill_watermarks(
                    conn,
                    watermarks,
                    cost_watermarks=cost_watermarks,
                )
                conn.execute(
                    "INSERT INTO system_events "
                    "(run_id, event_type, payload, duplicate_key, broker_order_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        run_id,
                        "fill_reconciliation",
                        payload_json,
                        _system_event_duplicate_key(event_payload),
                        _system_event_broker_order_id(event_payload),
                    ),
                )

    def save_signal_package(self, signal_run_id: str, payload: dict[str, Any]) -> None:
        payload_with_id = dict(payload)
        payload_with_id["signal_run_id"] = signal_run_id
        self.save_system_event(signal_run_id, "signal_package", payload_with_id)

    def mark_signal_package_consumed(self, signal_run_id: str, approval_run_id: str) -> None:
        self.save_system_event(
            signal_run_id,
            "signal_package_consumed",
            {
                "signal_run_id": signal_run_id,
                "approval_run_id": approval_run_id,
            },
        )

    def load_signal_package(self, signal_run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            package_row = conn.execute(
                "SELECT payload FROM system_events "
                "WHERE run_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
                (signal_run_id, "signal_package"),
            ).fetchone()
            consumed_row = conn.execute(
                "SELECT payload FROM system_events "
                "WHERE run_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
                (signal_run_id, "signal_package_consumed"),
            ).fetchone()
        if package_row is None:
            return None
        payload = json.loads(package_row[0])
        payload.setdefault("approval_consumed", False)
        if consumed_row is not None:
            consumed = json.loads(consumed_row[0])
            payload["approval_consumed"] = True
            payload["approval_run_id"] = consumed.get("approval_run_id")
        return payload

    def save_approval(self, run_id: str, approval_id: str, payload: dict[str, Any]) -> None:
        if self.approval_exists(approval_id):
            raise ValueError(f"Approval decision already exists: {approval_id}")
        try:
            self._insert("approvals", run_id, approval_id, payload)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Approval decision already exists: {approval_id}") from exc

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

    def save_account_attribution_snapshot(
        self,
        run_id: str,
        positions: list[Any],
    ) -> None:
        payloads = []
        for position in positions:
            payload = (
                position.model_dump(mode="json")
                if hasattr(position, "model_dump")
                else dict(position)
            )
            payloads.append(
                (
                    run_id,
                    str(payload["account_id"]),
                    str(payload["symbol"]),
                    str(payload["bucket_id"]),
                    json.dumps(payload, default=str),
                )
            )
        if not payloads:
            return
        with self.writer_lock("save_account_attribution_snapshot"):
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO account_attribution_snapshots "
                    "(run_id, account_id, symbol, bucket_id, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    payloads,
                )

    def list_portfolio_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("portfolio_snapshots", limit)

    def list_strategy_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("strategy_runs", limit)

    def list_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("orders", limit)

    def monthly_contribution_order_exists(
        self,
        month_key: str,
        sleeve: str,
        *,
        execution_sleeve: str | None = None,
        account_id: str | None = None,
    ) -> bool:
        query = "SELECT 1 FROM orders WHERE contribution_month = ? AND contribution_sleeve = ?"
        params: list[Any] = [month_key, sleeve]
        if execution_sleeve is not None:
            query += " AND execution_sleeve = ?"
            params.append(execution_sleeve)
        if account_id is not None:
            query += " AND account_id = ?"
            params.append(account_id)
        query += " LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return row is not None

    def monthly_live_contribution_order_exists(
        self,
        month_key: str,
        sleeve: str,
        *,
        execution_sleeve: str | None = None,
        account_id: str | None = None,
    ) -> bool:
        retryable_statuses = {"rejected", "canceled", "failed"}
        contribution_order_ids = self._monthly_contribution_order_ids(
            month_key,
            sleeve,
            execution_sleeve=execution_sleeve,
            account_id=account_id,
        )
        contribution_order_ids -= self._approved_recovery_source_order_ids()
        if not contribution_order_ids:
            return False
        seen_in_lifecycle: set[str] = set()
        for order_id in contribution_order_ids:
            lifecycle_rows = self._list_system_events_by_order_id("live_order_lifecycle", order_id)
            if not lifecycle_rows:
                continue
            seen_in_lifecycle.add(order_id)
            for row in lifecycle_rows:
                payload = row["payload"]
                if payload.get("applied_fills"):
                    return True
                final_status = str(payload.get("final_status") or "").lower()
                if final_status in retryable_statuses:
                    continue
                return True
        for order_id in contribution_order_ids - seen_in_lifecycle:
            if self._list_system_events_by_order_id("live_order_result", order_id):
                return True
        return False

    def _approved_recovery_source_order_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM system_events WHERE event_type = ?",
                ("live_order_recovery_ack",),
            ).fetchall()
        source_order_ids = set()
        for (raw_payload,) in rows:
            payload = json.loads(raw_payload)
            if str(payload.get("status") or "").lower() != "approved":
                continue
            source_order_id = payload.get("source_order_id")
            if source_order_id:
                source_order_ids.add(str(source_order_id))
        return source_order_ids

    def _monthly_contribution_order_ids(
        self,
        month_key: str,
        sleeve: str,
        *,
        execution_sleeve: str | None = None,
        account_id: str | None = None,
    ) -> set[str]:
        query = (
            "SELECT order_id, payload FROM orders "
            "WHERE contribution_month = ? AND contribution_sleeve = ?"
        )
        params: list[Any] = [month_key, sleeve]
        if execution_sleeve is not None:
            query += " AND execution_sleeve = ?"
            params.append(execution_sleeve)
        if account_id is not None:
            query += " AND account_id = ?"
            params.append(account_id)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        order_ids = set()
        for order_id, raw_payload in rows:
            payload = json.loads(raw_payload)
            order_ids.add(str(payload.get("order_id") or order_id))
        return order_ids

    def list_system_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("system_events", limit)

    def list_system_events_by_type(
        self,
        event_type: str,
        limit: int | None = 10,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM system_events WHERE event_type = ?"
        values: list[Any] = [event_type]
        if since is not None:
            # idx_system_events_type_created가 (event_type, created_at)이므로
            # 시간 하한은 인덱스를 그대로 탄다.
            # created_at is stored as YYYY-MM-DD HH:MM:SS (no microseconds)
            # by SQLite DEFAULT CURRENT_TIMESTAMP, so format since the same way.
            sql += " AND created_at >= ?"
            values.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, values).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def list_system_events_in_range(
        self,
        event_type: str,
        *,
        start_utc: str,
        end_utc: str,
    ) -> list[dict[str, Any]]:
        """System events of `event_type` with created_at in UTC ``[start, end)``.

        Bounds are compared against the ``created_at`` column, which is stored as a
        UTC ``YYYY-MM-DD HH:MM:SS`` string, so callers must format bounds the same way.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM system_events "
                "WHERE event_type = ? AND created_at >= ? AND created_at < ? "
                "ORDER BY id DESC",
                (event_type, start_utc, end_utc),
            ).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def duplicate_key_exists(self, duplicate_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM system_events WHERE duplicate_key = ? LIMIT 1",
                (duplicate_key,),
            ).fetchone()
        return row is not None

    def broker_order_id_seen(self, broker_order_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM system_events WHERE broker_order_id = ? LIMIT 1",
                (broker_order_id,),
            ).fetchone()
        return row is not None

    def system_event_exists(
        self,
        event_type: str,
        order_id: str,
        *,
        run_id: str | None = None,
    ) -> bool:
        query = "SELECT 1 FROM system_events WHERE event_type = ? AND order_id = ?"
        params: list[Any] = [event_type, order_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return row is not None

    def list_order_ids_for_event_type(self, event_type: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT order_id FROM system_events "
                "WHERE event_type = ? AND order_id IS NOT NULL",
                (event_type,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def list_system_events_by_broker_order_id(
        self,
        broker_order_id: str,
        event_type: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM system_events WHERE broker_order_id = ? AND event_type = ? "
                "ORDER BY id DESC",
                (broker_order_id, event_type),
            ).fetchall()

        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def _list_system_events_by_order_id(
        self,
        event_type: str,
        order_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM system_events WHERE event_type = ? AND order_id = ? "
                "ORDER BY id DESC",
                (event_type, order_id),
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

    def list_broker_account_snapshots(
        self,
        limit: int | None = 10,
        *,
        since: str | None = None,
        before: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        values: list[Any] = []
        if since is not None:
            clauses.append("created_at >= ?")
            values.append(since)
        if before is not None:
            clauses.append("created_at < ?")
            values.append(before)
        if account_id is not None:
            # Filtering after the limit means a caller wanting one account's
            # history silently gets less of it as more accounts are tracked.
            clauses.append("account_id = ?")
            values.append(account_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT * FROM broker_account_snapshots" + where + " ORDER BY created_at DESC, id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, values).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def list_risk_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._list_rows("risk_decisions", limit)

    def list_strategy_book_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_rows("strategy_book_snapshots", limit)

    def list_account_attribution_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._list_rows("account_attribution_snapshots", limit)

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
                "account_attribution_snapshots",
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
                    contribution_columns = _order_contribution_columns(payload)
                    conn.execute(
                        "INSERT INTO orders "
                        "(run_id, order_id, payload, contribution_month, contribution_sleeve, "
                        "execution_sleeve, account_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, secondary, payload_json, *contribution_columns),
                    )
                elif table == "system_events":
                    conn.execute(
                        "INSERT INTO system_events "
                        "(run_id, event_type, payload, duplicate_key, broker_order_id, order_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            secondary,
                            payload_json,
                            _system_event_duplicate_key(payload),
                            _system_event_broker_order_id(payload),
                            _system_event_order_id(payload),
                        ),
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
                        "INSERT INTO portfolio_snapshots "
                        "(run_id, account_id, payload) VALUES (?, ?, ?)",
                        (run_id, secondary, payload_json),
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

    def _upsert_fill_watermarks(
        self,
        conn: sqlite3.Connection,
        watermarks: dict[str, tuple[float, float]],
        *,
        cost_watermarks: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        broker_order_ids = set(watermarks) | set(cost_watermarks or {})
        if not broker_order_ids:
            return
        conn.executemany(
            "INSERT INTO fill_watermarks "
            "(broker_order_id, cumulative_quantity, cumulative_notional, "
            "cumulative_commission, cumulative_tax, updated_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(broker_order_id) DO UPDATE SET "
            "cumulative_quantity = excluded.cumulative_quantity, "
            "cumulative_notional = excluded.cumulative_notional, "
            "cumulative_commission = excluded.cumulative_commission, "
            "cumulative_tax = excluded.cumulative_tax, "
            "updated_at = CURRENT_TIMESTAMP",
            [
                (
                    broker_order_id,
                    watermarks.get(broker_order_id, (0.0, 0.0))[0],
                    watermarks.get(broker_order_id, (0.0, 0.0))[1],
                    (cost_watermarks or {}).get(broker_order_id, (0.0, 0.0))[0],
                    (cost_watermarks or {}).get(broker_order_id, (0.0, 0.0))[1],
                )
                for broker_order_id in broker_order_ids
            ],
        )

    def _migrate_legacy_baseline_fill_watermarks(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        """Upgrade zeroed pre-v2 baseline watermarks from audited history."""
        rows = conn.execute(
            "SELECT payload FROM system_events "
            "WHERE event_type = 'broker_order_history_item' ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            payload = json.loads(row[0])
            broker_order_id = str(payload.get("broker_order_id") or "")
            if not broker_order_id:
                continue
            baseline_quantity = payload.get("quantity_in_adopted_positions") is True
            baseline_principal = payload.get("principal_in_cash_baseline") is True
            baseline_costs = payload.get("cost_in_cash_baseline") is True
            if not (baseline_quantity or baseline_principal or baseline_costs):
                continue
            existing = conn.execute(
                "SELECT cumulative_quantity, cumulative_notional, "
                "cumulative_commission, cumulative_tax FROM fill_watermarks "
                "WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            previous = tuple(float(value) for value in existing) if existing else (0.0,) * 4
            values = (
                max(previous[0], float(payload.get("filled_quantity") or 0.0))
                if baseline_quantity
                else previous[0],
                max(previous[1], float(payload.get("cumulative_notional") or 0.0))
                if baseline_principal
                else previous[1],
                max(previous[2], float(payload.get("cumulative_commission") or 0.0))
                if baseline_costs
                else previous[2],
                max(previous[3], float(payload.get("cumulative_tax") or 0.0))
                if baseline_costs
                else previous[3],
            )
            conn.execute(
                "INSERT INTO fill_watermarks "
                "(broker_order_id, cumulative_quantity, cumulative_notional, "
                "cumulative_commission, cumulative_tax, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(broker_order_id) DO UPDATE SET "
                "cumulative_quantity = excluded.cumulative_quantity, "
                "cumulative_notional = excluded.cumulative_notional, "
                "cumulative_commission = excluded.cumulative_commission, "
                "cumulative_tax = excluded.cumulative_tax, "
                "updated_at = CURRENT_TIMESTAMP",
                (broker_order_id, *values),
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
            "account_attribution_snapshots",
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


def _system_event_duplicate_key(payload: dict[str, Any]) -> str | None:
    value = payload.get("duplicate_key")
    return str(value) if value else None


def _prepare_atomic_system_events(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not events:
        raise ValueError("at least one system event is required")
    prepared: list[dict[str, Any]] = []
    for event in events:
        try:
            event_type = str(event["event_type"])
            payload = dict(event["payload"])
        except (KeyError, TypeError) as exc:
            # A caller that separates "bad batch" from "infrastructure
            # failure" with `except ValueError` should not see a malformed
            # event escape as a KeyError/TypeError instead.
            raise ValueError(f"malformed atomic system event: {exc!r}") from exc
        duplicate_key = _system_event_duplicate_key(payload)
        if not duplicate_key:
            raise ValueError("every atomic system event needs a duplicate key")
        prepared.append(
            {
                "event_type": event_type,
                "payload": payload,
                "duplicate_key": duplicate_key,
                "broker_order_id": _system_event_broker_order_id(payload),
                "order_id": _system_event_order_id(payload),
            }
        )
    keys = [item["duplicate_key"] for item in prepared]
    if len(set(keys)) != len(keys):
        raise ValueError("atomic system event duplicate keys must be unique")
    return prepared


def _compute_batch_fingerprint(prepared: list[dict[str, Any]]) -> str:
    """A stable identity for this batch, derived only from its own content.

    A genuine retry of the same batch must compute the same fingerprint
    without the caller passing anything extra, so it is a hash of the
    batch's own ``(event_type, normalized payload)`` pairs -- nothing
    nondeterministic (a timestamp, a random id, the connection, ``run_id``)
    feeds into it. Sorting by ``duplicate_key`` first (unique within one
    batch -- see the uniqueness check above) makes the fingerprint
    independent of the order the caller lists events in. Payloads are
    round-tripped through the same ``json.dumps(..., default=str)``
    normalization used for the content-replay check, and the final
    ``json.dumps(..., sort_keys=True)`` makes the hash independent of
    dict key order at every nesting level.
    """
    ordered = sorted(prepared, key=lambda item: item["duplicate_key"])
    canonical = json.dumps(
        [
            [item["event_type"], json.loads(json.dumps(item["payload"], default=str))]
            for item in ordered
        ],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_replay_provenance_mismatches(
    conn: sqlite3.Connection, keys: list[str], batch_fingerprint: str
) -> list[str]:
    """Which of ``keys`` are on record without *this* batch's provenance.

    Called only once every one of ``keys`` is confirmed present with
    matching content, to decide whether those rows were actually committed
    together by this method, as opposed to landing under the same keys with
    the same content via some other write path (or a different atomic
    batch that happens to produce the same content).  ``duplicate_key`` is
    one global namespace shared with ``save_system_event``, ``_insert``, and
    several bespoke methods, none of which stamp a ``batch_fingerprint`` --
    so a row from any of them comes back with ``NULL`` here and is reported
    as mismatched, exactly like a row stamped with a different batch's
    fingerprint would be.  Only when every row's stored fingerprint equals
    the one just computed for this call's own batch is this a genuine
    replay.
    """
    rows = conn.execute(
        "SELECT duplicate_key, batch_fingerprint FROM system_events "
        f"WHERE duplicate_key IN ({','.join('?' * len(keys))})",
        keys,
    ).fetchall()
    return sorted(
        str(row[0]) for row in rows if row[1] != batch_fingerprint
    )


def _find_replay_content_mismatches(
    conn: sqlite3.Connection, prepared: list[dict[str, Any]]
) -> list[str]:
    """Which of ``prepared``'s keys are on record under different content.

    Called only once every one of ``prepared``'s keys is already present, to
    decide whether this is a genuine replay of the same batch or an
    unrelated write that happens to share a key.  ``run_id`` is deliberately
    excluded: it is not part of what a key identifies.  Payloads are compared
    as parsed structures after round-tripping the submitted side through the
    same ``json.dumps(..., default=str)`` the stored side went through, so a
    value JSON can't represent natively (e.g. a ``datetime``) still compares
    equal to its own stored, stringified form.
    """
    keys = [item["duplicate_key"] for item in prepared]
    stored_by_key = {
        str(row[0]): (str(row[1]), row[2])
        for row in conn.execute(
            "SELECT duplicate_key, event_type, payload FROM system_events "
            f"WHERE duplicate_key IN ({','.join('?' * len(keys))})",
            keys,
        ).fetchall()
    }
    mismatched = []
    for item in prepared:
        stored = stored_by_key.get(item["duplicate_key"])
        if stored is None:
            continue
        stored_event_type, stored_payload_raw = stored
        submitted_payload = json.loads(json.dumps(item["payload"], default=str))
        if stored_event_type != item["event_type"] or json.loads(
            stored_payload_raw
        ) != submitted_payload:
            mismatched.append(item["duplicate_key"])
    return mismatched


def _system_event_broker_order_id(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    broker_order = result.get("broker_order")
    if not isinstance(broker_order, dict):
        return None
    value = broker_order.get("broker_order_id")
    return str(value) if value else None


_ORDER_ID_BACKFILL_EVENT_TYPES = (
    "live_order_lifecycle",
    "live_order_result",
    "live_order_recovery_required",
    "live_order_submit_intent",
    "live_order_halt",
)


def _system_event_order_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("order_id")
    if value:
        return str(value)
    request = payload.get("request")
    if isinstance(request, dict):
        value = request.get("order_id")
        if value:
            return str(value)
    return None


def _same_state_config_identity(existing: dict[str, str], current: dict[str, str]) -> bool:
    existing_state_fingerprint = existing.get("state_fingerprint")
    if existing_state_fingerprint is None:
        return existing.get("fingerprint") == current.get("fingerprint")
    return existing_state_fingerprint == current.get("state_fingerprint")


def _order_contribution_columns(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None, None, None, None
    contribution_month = None
    contribution_sleeve = None
    if metadata.get("order_generation_mode") == "buy_only_contribution":
        month = metadata.get("contribution_month")
        sleeve = metadata.get("contribution_sleeve")
        contribution_month = str(month) if month is not None else None
        contribution_sleeve = str(sleeve) if sleeve is not None else None
    execution_sleeve = metadata.get("execution_sleeve")
    account_id = metadata.get("account_id")
    return (
        contribution_month,
        contribution_sleeve,
        str(execution_sleeve) if execution_sleeve is not None else None,
        str(account_id) if account_id is not None else None,
    )
