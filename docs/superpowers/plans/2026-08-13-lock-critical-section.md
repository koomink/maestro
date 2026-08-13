# 잠금 임계구역 재설계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 승인 간 직렬화를 장시간 보유하는 flock에서 내구적 리스로 옮겨, 체결 폴링을 잠금 임계구역 밖으로 뺀다.

**Architecture:** `execution_leases` 테이블이 `(account_id, currency)`별 배타적 집행권을 갖는다. 리스 획득은 approval 기록과 같은 단일 SQLite 트랜잭션(`BEGIN IMMEDIATE`)에서 all-or-nothing으로 일어난다. 해제는 DB가 terminal 조건을 직접 검증한 뒤에만 수행한다. 만료는 회수 권한을 주지 않는다(fail-closed) — 대신 sweeper가 알리고 운영자 명령이 복구한다. `live_order_lock`은 주문 한 건의 제출 왕복으로 되돌아간다.

**Tech Stack:** Python 3, SQLite (`sqlite3`, WAL), Typer CLI, pytest, ruff, systemd

**Spec:** `docs/superpowers/specs/2026-08-13-lock-critical-section-design.md`

## Global Constraints

- 테스트 기준선: `.venv/bin/python -m pytest tests/ -q` → **1352 passed, 9 skipped**. 각 태스크는 이 수를 줄이지 않는다.
- 린트: `.venv/bin/python -m ruff check src tests --output-format=concise` → `All checks passed!`
- **모든 하중 테스트는 뮤테이션으로 비공허성을 증명한다.** 구현을 되돌려 테스트가 실패하는 것을 확인하고 복원한다. 잠금·동시성 결함은 단일 프로세스 테스트에서 조용히 통과하므로, 실제 다중 프로세스로 재현하지 않은 테스트는 아무것도 증명하지 못한다.
- **잠금 순서는 항상 `live_order_lock` → `writer_lock`.** `StateStore.live_order_lock`이 위반 시 `RuntimeError`를 던진다.
- **`writer_lock`은 advisory flock이며 SQLite 트랜잭션이 아니다.** 원자성이 필요하면 단일 연결에서 `conn.isolation_level = None` + `BEGIN IMMEDIATE`를 쓴다.
- 커밋 trailer:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK
  ```
- **Task 2와 Task 3은 함께 배포된다.** 획득만 배포하면 해제할 수 없는 리스가 생긴다. 둘 중 하나만 머지하지 않는다.
- 운영 DB(`/root/maestro-operator/var/symphony_state.db`)는 읽기 전용으로만 접근한다. 마이그레이션 검증은 사본으로 한다.

## File Structure

| 파일 | 책임 |
|---|---|
| `src/maestro/state/store.py` | `execution_leases` 스키마, `acquire_execution_lease`, `release_execution_lease`, `list_execution_leases`, canonical 경로 |
| `src/maestro/state/lease.py` (신규) | 리스 값 객체와 terminal 판정 순수 함수 — store에서 분리해 테스트 가능하게 |
| `src/maestro/orchestration/orchestrator.py` | 리스 획득/해제로 바깥 `live_order_lock` 대체 |
| `src/maestro/execution/live_order_safety.py` | 잠금 아래 브로커 I/O 허용 표식 |
| `src/maestro/state/lock_guard.py` (신규) | 잠금 아래 금지 동작 가드와 명시적 허용 컨텍스트 |
| `src/maestro/ops/lease_sweeper.py` (신규) | stale 리스 탐지 + outbox enqueue |
| `src/maestro/cli.py` | `lease-status`, `release-lease` 명령 |
| `deploy/scripts/deploy.sh` (신규) | mask → 확인 → 갱신 → unmask 배포 절차 |

---

### Task 1: 잠금 신원을 canonical DB 경로에서 파생

**Files:**
- Modify: `src/maestro/state/store.py:26-30`
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: 없음
- Produces: `StateStore.path`가 항상 canonical(심링크 해석 완료) `Path`. `lock_path` / `live_order_lock_path`가 그로부터 파생된다.

- [ ] **Step 1: Write the failing test**

`tests/test_state_store.py` 끝에 추가:

```python
def test_symlinked_database_paths_share_one_lock(tmp_path):
    """A symlink alias must not split one database across two lock files.

    Lock paths are derived from the database path, so /real.db and a symlink
    /alias.db would otherwise take /real.db.lock and /alias.db.lock — two
    different files that exclude nothing. The approval serialization this
    system depends on would silently stop working.
    """
    real = tmp_path / "real.db"
    StateStore(str(real))
    alias = tmp_path / "alias.db"
    alias.symlink_to(real)

    store_real = StateStore(str(real))
    store_alias = StateStore(str(alias))

    assert store_real.lock_path == store_alias.lock_path
    assert store_real.live_order_lock_path == store_alias.live_order_lock_path
    with store_real.writer_lock("real_writer"):
        with pytest.raises(RuntimeError, match="Lock order violation"):
            with store_alias.live_order_lock("alias_live"):
                pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_state_store.py::test_symlinked_database_paths_share_one_lock -v`
Expected: FAIL — `assert PosixPath('.../alias.db.lock') == PosixPath('.../real.db.lock')`

- [ ] **Step 3: Canonicalize in the constructor**

`src/maestro/state/store.py`, `self.path = Path(path)` 를 교체:

```python
        # Canonicalize before deriving anything: every lock path and depth key
        # below is derived from this, and a symlink alias would otherwise give
        # two StateStores over one database two disjoint sets of locks.
        # strict=False so a not-yet-created database still resolves.
        self.path = Path(path).resolve()
```

- [ ] **Step 4: Run the test and the full suite**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q && .venv/bin/python -m pytest tests/ -q`
Expected: 신규 테스트 PASS, 전체 **1353 passed, 9 skipped**

- [ ] **Step 5: Mutation-verify**

`self.path = Path(path).resolve()` 를 `self.path = Path(path)` 로 되돌리고 신규 테스트를 실행해 FAIL을 확인한 뒤 복원한다.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/state/store.py tests/test_state_store.py
git commit -m "fix(state): derive lock identity from the canonical database path"
```

---

### Task 2: `execution_leases` 테이블과 원자적 획득

**Files:**
- Create: `src/maestro/state/lease.py`
- Modify: `src/maestro/state/store.py` (`_init_db`, 신규 메서드)
- Test: `tests/test_execution_lease.py` (신규)

**Interfaces:**
- Consumes: Task 1의 canonical `self.path`
- Produces:
  - `maestro.state.lease.LeaseKey` — `NamedTuple(account_id: str, currency: str)`
  - `maestro.state.lease.HeldLease` — `NamedTuple(account_id, currency, run_id, approval_id, acquired_at: str, stale_after: str)`
  - `StateStore.acquire_execution_lease(run_id: str, approval_id: str, approval_payload: dict[str, Any], keys: Sequence[LeaseKey], *, stale_after_seconds: float) -> dict[str, Any]` — 반환 `{"acquired": bool, "blocking": tuple[HeldLease, ...]}`
  - `StateStore.list_execution_leases() -> list[HeldLease]`

- [ ] **Step 1: Create the value objects**

`src/maestro/state/lease.py`:

```python
"""Execution lease value objects.

A lease is exclusive execution rights over one account's funds in one
currency. It is held across the whole rotation -- sells, the fill poll that
funds the buys, and the buys -- which is far too long for a file lock but
costs nothing as a database row.
"""

from typing import NamedTuple


class LeaseKey(NamedTuple):
    account_id: str
    currency: str


class HeldLease(NamedTuple):
    account_id: str
    currency: str
    run_id: str
    approval_id: str
    acquired_at: str
    stale_after: str

    def describe(self) -> str:
        return (
            f"{self.account_id}/{self.currency} held by run {self.run_id} "
            f"(approval {self.approval_id}) since {self.acquired_at}"
        )
```

- [ ] **Step 2: Write the failing tests**

`tests/test_execution_lease.py`:

```python
import multiprocessing
import sqlite3

import pytest

from maestro.state.lease import LeaseKey
from maestro.state.store import StateStore

KEY_USD = LeaseKey("toss_brokerage", "USD")
KEY_KRW = LeaseKey("toss_brokerage", "KRW")
PAYLOAD = {"signal_run_id": "sig_1", "decision": {"status": "approved"}}


def test_acquiring_a_lease_records_the_approval_in_the_same_transaction(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))

    result = store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    assert result["acquired"] is True
    assert result["blocking"] == ()
    assert store.approval_exists("appr_1") is True
    held = store.list_execution_leases()
    assert [(item.account_id, item.currency, item.run_id) for item in held] == [
        ("toss_brokerage", "USD", "run_1")
    ]


def test_a_second_approval_cannot_take_a_held_lease(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    result = store.acquire_execution_lease(
        "run_2", "appr_2", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    assert result["acquired"] is False
    assert [item.run_id for item in result["blocking"]] == ["run_1"]
    # All or nothing: the losing approval must not be recorded either.
    assert store.approval_exists("appr_2") is False


def test_a_lease_on_another_account_does_not_block(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    result = store.acquire_execution_lease(
        "run_2", "appr_2", PAYLOAD, [KEY_KRW], stale_after_seconds=600.0
    )

    assert result["acquired"] is True


def test_a_partial_key_collision_writes_nothing(tmp_path):
    """One held key must block the whole set, including the other keys."""
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    result = store.acquire_execution_lease(
        "run_2", "appr_2", PAYLOAD, [KEY_USD, KEY_KRW], stale_after_seconds=600.0
    )

    assert result["acquired"] is False
    assert store.approval_exists("appr_2") is False
    assert [(item.account_id, item.currency) for item in store.list_execution_leases()] == [
        ("toss_brokerage", "USD")
    ]


def test_a_stale_lease_is_still_not_reclaimable(tmp_path):
    """stale_after is a diagnostic, never authority to take the lease.

    Reclaiming without generation fencing lets a stalled owner wake up and
    submit buys while the new owner spends the same cash. Part 2 adds fencing;
    until then expiry grants nothing.
    """
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=-1.0
    )

    result = store.acquire_execution_lease(
        "run_2", "appr_2", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    assert result["acquired"] is False


def test_a_duplicate_approval_id_is_refused(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )
    store.release_execution_lease_for_test_setup("run_1")  # defined in Task 3

    with pytest.raises(ValueError, match="Approval decision already exists"):
        store.acquire_execution_lease(
            "run_2", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
        )
```

> Task 3 이전에는 마지막 테스트가 `release_execution_lease_for_test_setup`을 찾지 못한다. 그 테스트는 Task 3에서 활성화한다 — 지금은 `@pytest.mark.skip(reason="needs Task 3 release API")`를 붙여 둔다.

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_execution_lease.py -v`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'acquire_execution_lease'`

- [ ] **Step 4: Add the schema**

`src/maestro/state/store.py`의 `_init_db` 안, `approvals` 테이블 생성 뒤에 추가:

```python
            conn.execute(
                "CREATE TABLE IF NOT EXISTS execution_leases "
                "("
                "account_id TEXT NOT NULL, "
                "currency TEXT NOT NULL, "
                "run_id TEXT NOT NULL, "
                "approval_id TEXT NOT NULL, "
                "acquired_at TEXT NOT NULL, "
                "stale_after TEXT NOT NULL, "
                "PRIMARY KEY (account_id, currency)"
                ")"
            )
```

- [ ] **Step 5: Implement acquisition**

`src/maestro/state/store.py`에 추가. 파일 상단에 `from maestro.state.lease import HeldLease, LeaseKey` 를 import 한다.

```python
    def acquire_execution_lease(
        self,
        run_id: str,
        approval_id: str,
        approval_payload: dict[str, Any],
        keys: Sequence[LeaseKey],
        *,
        stale_after_seconds: float,
    ) -> dict[str, Any]:
        """Take exclusive execution rights for every key, and record the approval.

        One connection, one BEGIN IMMEDIATE: writer_lock is an advisory flock
        and shares no transaction, so wrapping two store calls in it would
        still let a crash leave a lease without its approval. The lease rows
        are written before the approval so that a torn write, if the database
        ever allowed one, fails closed.

        A held key blocks the whole set. Expiry is not consulted: stale_after
        is a diagnostic, and reclaiming it without generation fencing would
        let two executions overlap.
        """
        if not keys:
            raise ValueError("acquire_execution_lease requires at least one key")
        now = utc_now()
        stale_after = (now + timedelta(seconds=stale_after_seconds)).isoformat()
        payload_json = json.dumps(approval_payload, default=str)
        with self.writer_lock("acquire_execution_lease"):
            with self._connect() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                blocking = [
                    HeldLease(*row)
                    for row in conn.execute(
                        "SELECT account_id, currency, run_id, approval_id, acquired_at, "
                        "stale_after FROM execution_leases WHERE (account_id, currency) IN "
                        f"({','.join('(?, ?)' for _ in keys)})",
                        [field for key in keys for field in key],
                    )
                ]
                if blocking:
                    return {"acquired": False, "blocking": tuple(blocking)}
                if conn.execute(
                    "SELECT 1 FROM approvals WHERE approval_id = ? LIMIT 1", (approval_id,)
                ).fetchone():
                    raise ValueError(f"Approval decision already exists: {approval_id}")
                for key in keys:
                    conn.execute(
                        "INSERT INTO execution_leases "
                        "(account_id, currency, run_id, approval_id, acquired_at, stale_after) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            key.account_id,
                            key.currency,
                            run_id,
                            approval_id,
                            now.isoformat(),
                            stale_after,
                        ),
                    )
                conn.execute(
                    "INSERT INTO approvals (run_id, approval_id, payload) VALUES (?, ?, ?)",
                    (run_id, approval_id, payload_json),
                )
                return {"acquired": True, "blocking": ()}

    def list_execution_leases(self) -> list[HeldLease]:
        with self._connect() as conn:
            return [
                HeldLease(*row)
                for row in conn.execute(
                    "SELECT account_id, currency, run_id, approval_id, acquired_at, stale_after "
                    "FROM execution_leases ORDER BY account_id, currency"
                )
            ]
```

`timedelta` 가 import 되어 있지 않으면 `from datetime import timedelta` 를 추가한다. `Sequence` 는 이미 `collections.abc` 에서 import 되어 있다.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_execution_lease.py -q`
Expected: 마지막 하나는 skip, 나머지 PASS

- [ ] **Step 7: Mutation-verify all-or-nothing**

`if blocking:` 앞에 `blocking = []` 를 삽입해 차단을 무력화한 뒤 `test_a_second_approval_cannot_take_a_held_lease`, `test_a_partial_key_collision_writes_nothing`, `test_a_stale_lease_is_still_not_reclaimable` 이 모두 FAIL하는 것을 확인하고 복원한다. 세 개 다 실패해야 한다 — 하나라도 통과하면 그 테스트는 아무것도 증명하지 않는다.

- [ ] **Step 8: Verify the migration against a production copy**

```bash
cp /root/maestro-operator/var/symphony_state.db /tmp/claude-0/*/scratchpad/lease_migration_check.db
.venv/bin/python -c "
from maestro.state.store import StateStore
import sqlite3, glob
p = glob.glob('/tmp/claude-0/*/scratchpad/lease_migration_check.db')[0]
before = sqlite3.connect(p).execute('SELECT COUNT(*) FROM system_events').fetchone()[0]
StateStore(p); StateStore(p)   # idempotent across reopens
conn = sqlite3.connect(p)
after = conn.execute('SELECT COUNT(*) FROM system_events').fetchone()[0]
print('rows:', before, '->', after)
print('leases:', conn.execute('SELECT COUNT(*) FROM execution_leases').fetchone()[0])
print('integrity:', conn.execute('PRAGMA integrity_check').fetchone()[0])
"
```
Expected: 행 수 보존, `leases: 0`, `integrity: ok`

- [ ] **Step 9: Commit**

```bash
git add src/maestro/state/lease.py src/maestro/state/store.py tests/test_execution_lease.py
git commit -m "feat(state): take execution leases and the approval in one transaction"
```

---

### Task 3: terminal 조건을 DB가 검증하는 해제

**Files:**
- Modify: `src/maestro/state/store.py`
- Test: `tests/test_execution_lease.py`

**Interfaces:**
- Consumes: Task 2의 `acquire_execution_lease`, `HeldLease`
- Produces: `StateStore.release_execution_lease(run_id: str, approval_id: str) -> dict[str, Any]` — 반환 `{"released": bool, "unresolved": tuple[str, ...]}`

- [ ] **Step 1: Write the failing tests**

`tests/test_execution_lease.py`에 추가. 앞 태스크의 skip 마커는 제거하고, 헬퍼 이름을 실제 API로 바꾼다(`store.release_execution_lease("run_1", "appr_1")`).

```python
def _intent(store, run_id, order_id):
    store.save_system_event(
        run_id,
        "live_order_submit_intent",
        {"request": {"order_id": order_id}, "duplicate_key": f"intent:{order_id}"},
    )


def _terminal_lifecycle(store, run_id, order_id, status="filled"):
    store.save_system_event(
        run_id,
        "live_order_lifecycle",
        {
            "run_id": run_id,
            "order_id": order_id,
            "final_status": status,
            "broker_order_id": f"B-{order_id}",
        },
    )


def test_a_lease_with_no_submissions_is_released(tmp_path):
    """Aborting before anything reached the broker is safe to release."""
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    result = store.release_execution_lease("run_1", "appr_1")

    assert result == {"released": True, "unresolved": ()}
    assert store.list_execution_leases() == []


def test_a_fully_terminal_execution_is_released(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )
    _intent(store, "run_1", "ord_a")
    _terminal_lifecycle(store, "run_1", "ord_a")

    assert store.release_execution_lease("run_1", "appr_1")["released"] is True


def test_a_sell_filled_but_buy_unsubmitted_abort_keeps_the_lease(tmp_path):
    """The 2026-08-12 state. Releasing here hands the sell proceeds away.

    A `finally: release()` implementation passes every other test in this file
    and fails only this one, which is the whole reason it exists.
    """
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )
    _intent(store, "run_1", "ord_sell")
    _terminal_lifecycle(store, "run_1", "ord_sell")
    _intent(store, "run_1", "ord_buy")  # submitted, never resolved

    result = store.release_execution_lease("run_1", "appr_1")

    assert result["released"] is False
    assert result["unresolved"] == ("ord_buy",)
    assert len(store.list_execution_leases()) == 1


def test_an_ambiguous_submit_keeps_the_lease(tmp_path):
    """Intent persisted, no outcome recorded: the order may or may not exist."""
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )
    _intent(store, "run_1", "ord_ambiguous")

    result = store.release_execution_lease("run_1", "appr_1")

    assert result["released"] is False
    assert result["unresolved"] == ("ord_ambiguous",)


def test_a_non_terminal_lifecycle_keeps_the_lease(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )
    _intent(store, "run_1", "ord_open")
    _terminal_lifecycle(store, "run_1", "ord_open", status="accepted_by_broker")

    assert store.release_execution_lease("run_1", "appr_1")["released"] is False


def test_release_is_owner_conditional(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1", "appr_1", PAYLOAD, [KEY_USD], stale_after_seconds=600.0
    )

    result = store.release_execution_lease("run_impostor", "appr_impostor")

    assert result["released"] is False
    assert len(store.list_execution_leases()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_execution_lease.py -v`
Expected: FAIL — `AttributeError: ... 'release_execution_lease'`

- [ ] **Step 3: Add the terminal predicate**

`src/maestro/state/lease.py`에 추가:

```python
# A lifecycle that reached one of these is done moving money. Anything else --
# including an order that was accepted and never resolved -- leaves the funds
# unaccounted for, so the lease must stay.
TERMINAL_FINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired", "failed"})


def unresolved_order_ids(
    intent_order_ids: list[str],
    terminal_order_ids: set[str],
) -> tuple[str, ...]:
    """Intents with no terminal outcome, in submission order."""
    seen: set[str] = set()
    unresolved: list[str] = []
    for order_id in intent_order_ids:
        if order_id in terminal_order_ids or order_id in seen:
            continue
        seen.add(order_id)
        unresolved.append(order_id)
    return tuple(unresolved)
```

- [ ] **Step 4: Implement release**

`src/maestro/state/store.py`:

```python
    def release_execution_lease(self, run_id: str, approval_id: str) -> dict[str, Any]:
        """Delete this run's leases, but only once its orders are settled.

        Owner-conditional deletion is not enough on its own: the rightful owner
        can also ask to release while it still holds open orders. The terminal
        condition is therefore checked against the database inside the same
        transaction as the delete, so no caller can talk its way past it.
        """
        with self.writer_lock("release_execution_lease"):
            with self._connect() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                intent_order_ids = [
                    order_id
                    for (payload,) in conn.execute(
                        "SELECT payload FROM system_events "
                        "WHERE run_id = ? AND event_type = 'live_order_submit_intent' "
                        "ORDER BY id",
                        (run_id,),
                    )
                    if (order_id := _lease_intent_order_id(payload)) is not None
                ]
                terminal_order_ids = {
                    order_id
                    for (payload,) in conn.execute(
                        "SELECT payload FROM system_events "
                        "WHERE run_id = ? AND event_type = 'live_order_lifecycle'",
                        (run_id,),
                    )
                    if (order_id := _lease_terminal_order_id(payload)) is not None
                }
                unresolved = unresolved_order_ids(intent_order_ids, terminal_order_ids)
                if unresolved:
                    return {"released": False, "unresolved": unresolved}
                deleted = conn.execute(
                    "DELETE FROM execution_leases WHERE run_id = ? AND approval_id = ?",
                    (run_id, approval_id),
                ).rowcount
                return {"released": deleted > 0, "unresolved": ()}
```

모듈 하단 헬퍼:

```python
def _lease_intent_order_id(payload: str) -> str | None:
    try:
        request = json.loads(payload).get("request") or {}
    except ValueError:
        return None
    order_id = request.get("order_id")
    return str(order_id) if order_id else None


def _lease_terminal_order_id(payload: str) -> str | None:
    try:
        record = json.loads(payload)
    except ValueError:
        return None
    if str(record.get("final_status") or "") not in TERMINAL_FINAL_STATUSES:
        return None
    order_id = record.get("order_id")
    return str(order_id) if order_id else None
```

`from maestro.state.lease import TERMINAL_FINAL_STATUSES, HeldLease, LeaseKey, unresolved_order_ids` 로 import 를 확장한다.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_execution_lease.py -q && .venv/bin/python -m pytest tests/ -q`
Expected: 전부 PASS, 전체 스위트 통과

- [ ] **Step 6: Mutation-verify the terminal gate**

`if unresolved:` 블록을 삭제해 무조건 해제하도록 만든 뒤, `test_a_sell_filled_but_buy_unsubmitted_abort_keeps_the_lease`, `test_an_ambiguous_submit_keeps_the_lease`, `test_a_non_terminal_lifecycle_keeps_the_lease` 세 개가 모두 FAIL 하는 것을 확인하고 복원한다.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/state/lease.py src/maestro/state/store.py tests/test_execution_lease.py
git commit -m "feat(state): release a lease only when the database says it is settled"
```

---

### Task 4: `resolve_pending_signal_approval`을 리스로 전환

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:304-360`
- Test: `tests/test_signal_approval_handoff.py`, `tests/test_execution_lease_integration.py` (신규)

**Interfaces:**
- Consumes: `acquire_execution_lease`, `release_execution_lease`, `LeaseKey`
- Produces: `MaestroOrchestrator._lease_keys_for(orders) -> list[LeaseKey]`

- [ ] **Step 1: Write the failing test**

`tests/test_execution_lease_integration.py`:

```python
"""The lock must be gone from the polling window, and the lease must not be."""

import pytest

from maestro.state.lease import LeaseKey
from maestro.state.store import StateStore


def test_another_process_can_take_both_locks_while_a_lease_is_held(tmp_path):
    """The whole point: holding execution rights must not hold the file locks.

    resume-order-tracking needs live_order_lock and writer_lock every two
    minutes. Under the old design an execution held live_order_lock for its
    entire ten-minute poll and starved it; the lease must not reproduce that.
    """
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_1",
        "appr_1",
        {"signal_run_id": "sig_1"},
        [LeaseKey("toss_brokerage", "USD")],
        stale_after_seconds=600.0,
    )

    with store.live_order_lock("background_job", timeout_seconds=1.0):
        with store.writer_lock("background_job", timeout_seconds=1.0):
            store.save_system_event("run_bg", "maestro_heartbeat", {})

    assert len(store.list_execution_leases()) == 1
    assert len(store.list_system_events_by_type("maestro_heartbeat")) == 1
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_execution_lease_integration.py -q`
Expected: PASS — 리스는 잠금이 아니므로 이미 통과한다. 이 테스트는 앞으로의 회귀를 막는 고정점이다.

- [ ] **Step 3: Replace the outer lock with a lease**

`src/maestro/orchestration/orchestrator.py`의 `resolve_pending_signal_approval`에서 `with self.state_store.live_order_lock("resolve_pending_signal_approval"):` 를 제거하고 본문을 아래 구조로 바꾼다. `save_approval` 호출은 리스 획득이 대신하므로 삭제한다.

```python
    def resolve_pending_signal_approval(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
    ) -> SignalApprovalSummary:
        """Apply one terminal decision loaded by the long-running Telegram operator.

        Serialization against other approvals is a lease, not a held lock:
        the fill poll between the sells and the buys runs for minutes, and a
        file lock held across it is what deadlocked 2026-08-11 and 08-12.
        """
        package = self.state_store.load_signal_package(envelope.signal_run_id)
        if package is None:
            raise ValueError(f"Unknown signal_run_id: {envelope.signal_run_id}")
        orders = [OrderIntent.model_validate(item) for item in envelope.orders]
        if decision.status == "approved":
            self._validate_signal_package_for_approval(package)
            self._validate_signal_approval_preconditions(envelope.run_id, package)
            orders, capacity_blocks = self._partition_orders_by_capacity(
                envelope.run_id,
                orders,
                signal_run_id=envelope.signal_run_id,
                package=package,
            )
            if capacity_blocks or not orders:
                raise ValueError("Pending approval is blocked by current broker capacity")
            self._validate_signal_approval_gates(envelope.run_id, orders, package)

        approval_payload = {
            "signal_run_id": envelope.signal_run_id,
            "source_strategy_ids": envelope.source_strategy_ids,
            "request": envelope.request.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "message": envelope.message,
            "account_ids": envelope.account_ids,
        }
        if len(envelope.account_ids) == 1:
            approval_payload["account_id"] = envelope.account_ids[0]
        lease = self.state_store.acquire_execution_lease(
            envelope.run_id,
            envelope.approval_id,
            approval_payload,
            self._lease_keys_for(orders),
            stale_after_seconds=self._execution_lease_stale_after_seconds(),
        )
        if not lease["acquired"]:
            blocked = "; ".join(item.describe() for item in lease["blocking"])
            self._record_event(
                envelope.run_id,
                "execution_lease_blocked",
                {
                    "approval_id": envelope.approval_id,
                    "blocking": [item._asdict() for item in lease["blocking"]],
                },
            )
            raise ValueError(f"Another execution holds these funds: {blocked}")
        self.audit.log(envelope.run_id, "approval_decision", approval_payload)

        try:
            return self._execute_resolved_approval(envelope, decision, orders)
        finally:
            # Not an unconditional release: the store refuses while orders are
            # unresolved, and the lease is meant to survive that case.
            outcome = self.state_store.release_execution_lease(
                envelope.run_id, envelope.approval_id
            )
            if not outcome["released"] and outcome["unresolved"]:
                self._record_event(
                    envelope.run_id,
                    "execution_lease_retained",
                    {
                        "approval_id": envelope.approval_id,
                        "unresolved_order_ids": list(outcome["unresolved"]),
                    },
                )
```

기존 본문의 나머지(주문 집행, `signal_approval_completed` 기록 등)는 `_execute_resolved_approval` 로 그대로 옮긴다.

- [ ] **Step 4: Add the helpers**

```python
    def _lease_keys_for(self, orders: list[OrderIntent]) -> list[LeaseKey]:
        """Cash is fungible per account and per currency, so the lease is too."""
        keys = {
            LeaseKey(
                str(order.account_id or "default"),
                resolve_order_currency(self.config, order).value,
            )
            for order in orders
        }
        return sorted(keys)

    def _execution_lease_stale_after_seconds(self) -> float:
        """Only a diagnostic threshold -- expiry grants no authority."""
        polls = float(self.config.execution.order_status_max_polls)
        interval = float(self.config.execution.order_status_poll_interval_seconds)
        return max(600.0, polls * interval * 4.0)
```

- [ ] **Step 5: Run the suites**

Run: `.venv/bin/python -m pytest tests/test_signal_approval_handoff.py tests/test_telegram_approval_resume.py tests/test_execution_lease_integration.py -q`
Expected: PASS. 실패하면 `save_approval` 을 여전히 기대하는 테스트다 — 리스 획득이 approval 을 쓰므로 단정을 `approval_exists` 로 옮긴다.

- [ ] **Step 6: Full suite and lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 7: Mutation-verify the finally block**

`release_execution_lease` 호출을 `conn.execute("DELETE FROM execution_leases ...")` 상당의 무조건 삭제로 바꾸는 대신, 더 간단히 Task 3의 `if unresolved:` 게이트를 제거하고 `test_a_sell_filled_but_buy_unsubmitted_abort_keeps_the_lease` 가 FAIL 하는지 확인한 뒤 복원한다.

- [ ] **Step 8: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/
git commit -m "refactor(orchestration): hold a lease across execution, not a file lock"
```

---

### Task 5: `approve_signal`과 `run_once`를 같은 구조로

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:274-296`
- Test: `tests/test_live_approval_run_once.py`

**Interfaces:**
- Consumes: Task 4의 `_lease_keys_for`, `_execution_lease_stale_after_seconds`
- Produces: 없음 (내부 정리)

- [ ] **Step 1: Update the lock-order test to the new shape**

`tests/test_live_approval_run_once.py::test_live_execution_entry_points_take_the_live_order_lock_outermost` 를 교체한다. 두 진입점은 이제 바깥 잠금을 잡지 않는다.

```python
def test_live_execution_entry_points_take_no_outer_locks(tmp_path, monkeypatch):
    """The outer live_order_lock is gone; serialization is the lease now.

    Holding it here was correct while the inner path needed it, but it spans
    the fill poll, which is exactly the hold this redesign removes.
    """
    orchestrator = MaestroOrchestrator(load_config(_overseas_live_approval_config(tmp_path)))
    lock_order: list[tuple[str, str]] = []
    original_writer_lock = orchestrator.state_store.writer_lock
    original_live_order_lock = orchestrator.state_store.live_order_lock

    @contextmanager
    def recording_writer_lock(owner: str, **kwargs):
        lock_order.append(("writer", owner))
        with original_writer_lock(owner, **kwargs):
            yield

    @contextmanager
    def recording_live_order_lock(owner: str, **kwargs):
        lock_order.append(("live_order", owner))
        with original_live_order_lock(owner, **kwargs):
            yield

    orchestrator.state_store.writer_lock = recording_writer_lock
    orchestrator.state_store.live_order_lock = recording_live_order_lock
    monkeypatch.setattr(MaestroOrchestrator, "_run_once_locked", lambda self: None)
    monkeypatch.setattr(
        MaestroOrchestrator, "_approve_signal_locked", lambda self, signal_run_id: None
    )

    orchestrator.run_once()
    orchestrator.approve_signal("signal_lock_order")

    assert [owner for _, owner in lock_order if owner in {"run_once", "approve_signal"}] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_live_approval_run_once.py::test_live_execution_entry_points_take_no_outer_locks -v`
Expected: FAIL — `assert ['run_once', 'run_once', 'approve_signal', 'approve_signal'] == []`

- [ ] **Step 3: Drop the outer locks**

```python
    def run_once(self) -> RunOnceSummary:
        # No outer lock: _execute_live_approval_orders serializes against other
        # approvals with an execution lease, and holding a lock across its fill
        # poll is the hold this redesign exists to remove.
        return self._run_once_locked()

    def approve_signal(self, signal_run_id: str) -> SignalApprovalSummary:
        return self._approve_signal_locked(signal_run_id)
```

- [ ] **Step 4: Route both through the lease**

`_approve_signal_locked` 와 `_run_once_locked` 가 `_execute_live_approval_orders` 를 호출하기 직전에 Task 4와 동일한 획득/해제 구조를 적용한다. 두 곳에 같은 코드를 붙여넣지 말고, Task 4에서 만든 획득·해제 쌍을 `_with_execution_lease(run_id, approval_id, payload, orders)` 컨텍스트 매니저로 추출해 세 진입점이 공유하게 한다.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 6: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_live_approval_run_once.py
git commit -m "refactor(orchestration): route every live entry point through the lease"
```

---

### Task 6: 잠금 아래 장기 동작을 금지하는 가드

**Files:**
- Create: `src/maestro/state/lock_guard.py`
- Modify: `src/maestro/state/store.py`, `src/maestro/execution/live_order_safety.py`
- Test: `tests/test_lock_guard.py` (신규)

**Interfaces:**
- Consumes: `StateStore.holds_writer_lock`
- Produces:
  - `maestro.state.lock_guard.assert_no_lock_held(operation: str) -> None`
  - `maestro.state.lock_guard.allow_broker_io_under_lock(reason: str)` — 컨텍스트 매니저
  - `StateStore.holds_any_lock() -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/test_lock_guard.py`:

```python
import pytest

from maestro.state.lock_guard import allow_broker_io_under_lock, assert_no_lock_held
from maestro.state.store import StateStore


def test_a_long_operation_under_a_lock_raises(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    with store.live_order_lock("holder"):
        with pytest.raises(RuntimeError, match="order status poll"):
            assert_no_lock_held("order status poll")


def test_the_same_operation_outside_a_lock_is_fine(tmp_path):
    StateStore(str(tmp_path / "state.db"))
    assert_no_lock_held("order status poll") is None


def test_an_explicit_allowance_permits_the_submit_round_trip(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    with store.live_order_lock("holder"):
        with allow_broker_io_under_lock("order submit"):
            assert_no_lock_held("order submit") is None


def test_the_allowance_does_not_leak_past_its_block(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    with store.live_order_lock("holder"):
        with allow_broker_io_under_lock("order submit"):
            pass
        with pytest.raises(RuntimeError, match="order status poll"):
            assert_no_lock_held("order status poll")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_lock_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.lock_guard'`

- [ ] **Step 3: Implement the guard**

`src/maestro/state/lock_guard.py`:

```python
"""Forbid waiting for an external system while holding a lock.

The ordering guard made lock-order inversion impossible; this one does the
same for hold time. A poll loop or an approval wait under a lock cannot be
caught by a single-process test -- it only appears as a cross-process stall
under production timing -- so it has to raise at the call site instead.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_ALLOWANCE = threading.local()


@contextmanager
def allow_broker_io_under_lock(reason: str) -> Iterator[None]:
    """Permit one bounded broker round-trip inside a lock.

    The submit critical section is deliberately locked: the daily-cap check,
    the submit and the write must be atomic against concurrent submissions.
    That exception is spelled out here so it stays visible and auditable
    rather than becoming an implicit habit.
    """
    previous = getattr(_ALLOWANCE, "reason", None)
    _ALLOWANCE.reason = reason
    try:
        yield
    finally:
        _ALLOWANCE.reason = previous


def assert_no_lock_held(operation: str) -> None:
    from maestro.state.store import any_lock_held_by_this_thread

    if getattr(_ALLOWANCE, "reason", None) is not None:
        return
    if any_lock_held_by_this_thread():
        raise RuntimeError(
            f"{operation} must not run while holding a state lock. Locks protect "
            "state transitions; waiting on an external system inside one is what "
            "starved the 2026-08-11 and 08-12 rotations."
        )
```

`src/maestro/state/store.py` 모듈 하단에 추가:

```python
def any_lock_held_by_this_thread() -> bool:
    return any(depth > 0 for depth in _lock_depths().values())
```

- [ ] **Step 4: Wire the guard into the long operations**

`src/maestro/execution/live_order_lifecycle.py` 폴링 루프 진입 직전과 승인 대기 진입 직전에 `assert_no_lock_held("order status poll")` / `assert_no_lock_held("approval wait")` 를 넣는다. `src/maestro/execution/live_order_safety.py:60` 의 `submit_limit_order` 호출을 감싼다:

```python
        with allow_broker_io_under_lock("order submit"):
            result = self.broker_client.submit_limit_order(request)
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 전체 통과. 실패하는 테스트가 있으면 그 경로가 아직 잠금 아래에서 폴링한다는 뜻이므로 **테스트가 아니라 코드를 고친다.**

- [ ] **Step 6: Mutation-verify**

`assert_no_lock_held` 본문을 `return` 으로 비우고 `tests/test_lock_guard.py` 의 두 raise 테스트가 FAIL 하는지 확인한 뒤 복원한다.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/state/lock_guard.py src/maestro/state/store.py src/maestro/execution/ tests/test_lock_guard.py
git commit -m "feat(state): refuse to wait on a broker while holding a lock"
```

---

### Task 7: stale 리스 sweeper와 알림 outbox

**Files:**
- Create: `src/maestro/ops/lease_sweeper.py`
- Modify: `src/maestro/state/store.py` (outbox 테이블), `src/maestro/cli.py` (heartbeat 에 sweeper 연결)
- Test: `tests/test_lease_sweeper.py` (신규)

**Interfaces:**
- Consumes: `list_execution_leases`
- Produces:
  - `StateStore.enqueue_lease_alerts(rows: Sequence[tuple[str, str, str, str]]) -> int` — `(account_id, currency, period_bucket, recipient)`
  - `StateStore.list_pending_lease_alerts() -> list[dict[str, Any]]`
  - `StateStore.mark_lease_alert_sent(account_id, currency, period_bucket, recipient) -> None`
  - `maestro.ops.lease_sweeper.sweep_stale_leases(store, recipients, *, now, period_seconds=900) -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

`tests/test_lease_sweeper.py`:

```python
from datetime import UTC, datetime, timedelta

from maestro.ops.lease_sweeper import sweep_stale_leases
from maestro.state.lease import LeaseKey
from maestro.state.store import StateStore

NOW = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


def _stale_lease(store):
    store.acquire_execution_lease(
        "run_dead",
        "appr_dead",
        {"signal_run_id": "sig"},
        [LeaseKey("toss_brokerage", "USD")],
        stale_after_seconds=-1.0,
    )


def test_a_stale_lease_is_reported_without_any_later_approval(tmp_path):
    """A dead lease that nobody bumps into must still surface.

    Alerting only when the next approval is blocked means an overnight failure
    stays invisible until the following night's run.
    """
    store = StateStore(str(tmp_path / "state.db"))
    _stale_lease(store)

    result = sweep_stale_leases(store, ["chat_a"], now=NOW)

    assert result["stale"] == 1
    pending = store.list_pending_lease_alerts()
    assert [item["recipient"] for item in pending] == ["chat_a"]


def test_a_healthy_lease_is_not_reported(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.acquire_execution_lease(
        "run_ok",
        "appr_ok",
        {"signal_run_id": "sig"},
        [LeaseKey("toss_brokerage", "USD")],
        stale_after_seconds=3600.0,
    )

    assert sweep_stale_leases(store, ["chat_a"], now=NOW)["stale"] == 0


def test_the_same_period_does_not_enqueue_twice(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _stale_lease(store)

    sweep_stale_leases(store, ["chat_a"], now=NOW)
    sweep_stale_leases(store, ["chat_a"], now=NOW + timedelta(seconds=60))

    assert len(store.list_pending_lease_alerts()) == 1


def test_a_later_period_enqueues_again(tmp_path):
    """Suppression must not become silence."""
    store = StateStore(str(tmp_path / "state.db"))
    _stale_lease(store)

    sweep_stale_leases(store, ["chat_a"], now=NOW)
    sweep_stale_leases(store, ["chat_a"], now=NOW + timedelta(seconds=1800))

    assert len(store.list_pending_lease_alerts()) == 2


def test_one_failed_recipient_does_not_complete_the_others(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _stale_lease(store)
    sweep_stale_leases(store, ["chat_a", "chat_b"], now=NOW)

    store.mark_lease_alert_sent("toss_brokerage", "USD", _bucket(NOW), "chat_a")

    assert [item["recipient"] for item in store.list_pending_lease_alerts()] == ["chat_b"]


def _bucket(moment):
    return str(int(moment.timestamp()) // 900)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_lease_sweeper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.ops.lease_sweeper'`

- [ ] **Step 3: Add the outbox table**

`_init_db` 에 추가:

```python
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lease_alert_outbox "
                "("
                "account_id TEXT NOT NULL, "
                "currency TEXT NOT NULL, "
                "period_bucket TEXT NOT NULL, "
                "recipient TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (account_id, currency, period_bucket, recipient)"
                ")"
            )
```

- [ ] **Step 4: Implement the store methods and the sweeper**

`src/maestro/ops/lease_sweeper.py`:

```python
"""Surface leases that outlived their diagnostic threshold.

Delivery cannot join the detection transaction, so the sweeper only writes:
it enqueues one pending row per recipient per period bucket. A separate
sender transitions the rows it actually delivered, which is why a single
failing chat cannot mark the alert done for everyone else.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from maestro.state.store import StateStore

PERIOD_SECONDS = 900


def period_bucket(now: datetime, period_seconds: int = PERIOD_SECONDS) -> str:
    return str(int(now.timestamp()) // period_seconds)


def sweep_stale_leases(
    store: StateStore,
    recipients: Sequence[str],
    *,
    now: datetime,
    period_seconds: int = PERIOD_SECONDS,
) -> dict[str, Any]:
    bucket = period_bucket(now, period_seconds)
    stale = [lease for lease in store.list_execution_leases() if lease.stale_after <= now.isoformat()]
    rows = [
        (lease.account_id, lease.currency, bucket, recipient)
        for lease in stale
        for recipient in recipients
    ]
    enqueued = store.enqueue_lease_alerts(rows) if rows else 0
    return {"stale": len(stale), "enqueued": enqueued}
```

`StateStore` 에 `enqueue_lease_alerts`(`INSERT OR IGNORE`), `list_pending_lease_alerts`(`status='pending'`), `mark_lease_alert_sent`(해당 행만 `status='sent'`)를 `writer_lock` 안에서 구현한다.

- [ ] **Step 5: Hook the sweeper into heartbeat**

`src/maestro/cli.py` 의 `heartbeat` 명령에서 이벤트 기록 뒤에 `sweep_stale_leases(store, _alert_recipients(maestro_config), now=utc_now())` 를 호출하고 결과를 `typer.echo` 로 남긴다. heartbeat 타이머는 이미 15분 주기다.

- [ ] **Step 6: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 7: Mutation-verify**

`period_bucket` 이 항상 `"0"` 을 반환하게 만들고 `test_a_later_period_enqueues_again` 이 FAIL 하는지 확인한 뒤 복원한다.

- [ ] **Step 8: Commit**

```bash
git add src/maestro/ops/lease_sweeper.py src/maestro/state/store.py src/maestro/cli.py tests/test_lease_sweeper.py
git commit -m "feat(ops): sweep stale leases into a per-recipient alert outbox"
```

---

### Task 8: 운영자 복구 명령

**Files:**
- Modify: `src/maestro/cli.py`
- Test: `tests/test_lease_cli.py` (신규)

**Interfaces:**
- Consumes: `list_execution_leases`, `release_execution_lease`
- Produces: `maestro lease-status`, `maestro release-lease`

- [ ] **Step 1: Write the failing tests**

`tests/test_lease_cli.py`:

```python
from typer.testing import CliRunner

from maestro.cli import app
from maestro.state.lease import LeaseKey
from maestro.state.store import StateStore


def test_release_lease_refuses_and_names_what_is_unresolved(tmp_path, monkeypatch):
    """Refusal must be actionable: say which orders are still in the air."""
    config_path, store = _live_config(tmp_path, monkeypatch)
    store.acquire_execution_lease(
        "run_1", "appr_1", {"signal_run_id": "sig"},
        [LeaseKey("toss_brokerage", "USD")], stale_after_seconds=-1.0,
    )
    store.save_system_event(
        "run_1", "live_order_submit_intent",
        {"request": {"order_id": "ord_open"}, "duplicate_key": "intent:ord_open"},
    )

    result = CliRunner().invoke(
        app, ["release-lease", "--config", str(config_path),
              "--account", "toss_brokerage", "--currency", "USD", "--confirm"],
    )

    assert result.exit_code != 0
    assert "ord_open" in result.output
    assert len(store.list_execution_leases()) == 1


def test_force_release_requires_a_reason_and_records_an_audit_event(tmp_path, monkeypatch):
    config_path, store = _live_config(tmp_path, monkeypatch)
    store.acquire_execution_lease(
        "run_1", "appr_1", {"signal_run_id": "sig"},
        [LeaseKey("toss_brokerage", "USD")], stale_after_seconds=-1.0,
    )
    store.save_system_event(
        "run_1", "live_order_submit_intent",
        {"request": {"order_id": "ord_open"}, "duplicate_key": "intent:ord_open"},
    )

    result = CliRunner().invoke(
        app, ["release-lease", "--config", str(config_path),
              "--account", "toss_brokerage", "--currency", "USD",
              "--confirm", "--force", "--reason", "broker confirmed no order"],
    )

    assert result.exit_code == 0
    assert store.list_execution_leases() == []
    events = store.list_system_events_by_type("execution_lease_force_released")
    assert events[0]["payload"]["reason"] == "broker confirmed no order"
```

`_live_config` 는 `tests/test_live_fill_reconciliation.py::test_reconcile_fills_cli_outputs_result` 의 설정 작성 방식을 그대로 따른다.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_lease_cli.py -v`
Expected: FAIL — `No such command 'release-lease'`

- [ ] **Step 3: Implement the commands**

```python
@app.command("lease-status")
def lease_status(config: Path | None = CONFIG_OPTION) -> None:
    """Everything needed to judge a stuck lease, gathered in one place."""
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    for lease in store.list_execution_leases():
        outcome = store.release_execution_lease_dry_run(lease.run_id, lease.approval_id)
        typer.echo(
            f"lease account={lease.account_id} currency={lease.currency} "
            f"run_id={lease.run_id} approval_id={lease.approval_id} "
            f"acquired_at={lease.acquired_at} stale_after={lease.stale_after} "
            f"unresolved={','.join(outcome['unresolved']) or 'none'}"
        )


@app.command("release-lease")
def release_lease(
    config: Path | None = CONFIG_OPTION,
    account: str = typer.Option(..., "--account"),
    currency: str = typer.Option(..., "--currency"),
    confirm: bool = typer.Option(False, "--confirm"),
    force: bool = typer.Option(False, "--force"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    if not confirm:
        raise typer.BadParameter("release-lease requires --confirm")
    if force and not reason:
        raise typer.BadParameter("--force requires --reason")
    held = [
        lease
        for lease in store.list_execution_leases()
        if lease.account_id == account and lease.currency == currency
    ]
    if not held:
        typer.echo(f"release_lease status=absent account={account} currency={currency}")
        return
    lease = held[0]
    outcome = store.release_execution_lease(lease.run_id, lease.approval_id)
    if outcome["released"]:
        typer.echo(f"release_lease status=released account={account} currency={currency}")
        return
    if not force:
        unresolved = ",".join(outcome["unresolved"])
        typer.echo(f"release_lease status=refused unresolved={unresolved}")
        raise typer.Exit(code=1)
    # Re-read the owner: an execution that woke up and renewed must not be
    # released out from under itself.
    current = [
        item
        for item in store.list_execution_leases()
        if item.account_id == account and item.currency == currency
    ]
    if not current or current[0].run_id != lease.run_id:
        typer.echo("release_lease status=owner_changed")
        raise typer.Exit(code=1)
    store.force_release_execution_lease(lease.run_id, lease.approval_id, reason=str(reason))
    typer.echo(f"release_lease status=force_released account={account} currency={currency}")
```

`StateStore` 에 `release_execution_lease_dry_run`(삭제 없이 `unresolved` 만 계산)과 `force_release_execution_lease`(삭제 + `execution_lease_force_released` 이벤트를 한 트랜잭션에서)를 추가한다.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 5: Mutation-verify**

`if force and not reason:` 검사를 제거하고 `test_force_release_requires_a_reason_and_records_an_audit_event` 를 `--reason` 없이 호출하도록 임시 변형해 FAIL을 확인한 뒤 복원한다.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/cli.py src/maestro/state/store.py tests/test_lease_cli.py
git commit -m "feat(cli): inspect and conditionally release a stuck execution lease"
```

---

### Task 9: 배포 스크립트

**Files:**
- Create: `deploy/scripts/deploy.sh`
- Test: 수동 검증 (dry-run)

**Interfaces:**
- Consumes: 없음
- Produces: `deploy/scripts/deploy.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Quiesce every unit that runs live-order code, then update, then restart.
#
# systemd runs this git working tree directly, so `git merge` changes what new
# processes execute the instant it lands. A process started before the merge
# keeps the old lock protocol, and the two protocols deadlock against each
# other -- which is what killed the 2026-08-11 and 08-12 rotations. Stopping
# is not enough: run-once and approve-signal restart the operator when they
# finish (cli.py:198, 253), so the units are masked for the duration.
set -euo pipefail

UNITS=(
  maestro-resume-order-tracking.timer maestro-resume-order-tracking.service
  maestro-run-once.timer maestro-run-once.service
  maestro-symphony-signal-kr.timer maestro-symphony-signal-kr.service
  maestro-symphony-signal-us.timer maestro-symphony-signal-us.service
  maestro-telegram-operator.service
)
REPO=/root/projects/Symphony/Maestro
STATE=/root/maestro-operator/var/symphony_state.db

echo "== masking =="
systemctl mask --now "${UNITS[@]}"

echo "== waiting for units to go inactive =="
for unit in "${UNITS[@]}"; do
  for _ in $(seq 1 60); do
    systemctl is-active --quiet "$unit" || break
    sleep 1
  done
  if systemctl is-active --quiet "$unit"; then
    echo "ABORT: $unit is still active" >&2
    systemctl unmask "${UNITS[@]}"
    exit 1
  fi
done

echo "== checking for lock holders =="
"$REPO/.venv/bin/python" - "$STATE" <<'PY'
import sys
from pathlib import Path
from maestro.state.store import StateStore

state = Path(sys.argv[1])
for suffix in (".lock", ".live.lock"):
    holder = StateStore.read_lock_holder(state.with_suffix(state.suffix + suffix))
    if holder:
        print(f"ABORT: {suffix} still held by {holder}", file=sys.stderr)
        raise SystemExit(1)
print("no lock holders")
PY

echo "== updating =="
git -C "$REPO" merge --ff-only "${1:-main}"

echo "== unmasking and starting =="
systemctl unmask "${UNITS[@]}"
systemctl start maestro-telegram-operator.service
systemctl start maestro-resume-order-tracking.timer
echo "deploy complete"
```

- [ ] **Step 2: Verify it is syntactically sound and refuses on a busy unit**

```bash
chmod +x deploy/scripts/deploy.sh
bash -n deploy/scripts/deploy.sh
```
Expected: 출력 없음(문법 정상)

- [ ] **Step 3: Commit**

```bash
git add deploy/scripts/deploy.sh
git commit -m "chore(deploy): quiesce every live-order unit before updating code"
```

---

## Self-Review

**Spec coverage**

| 스펙 절 | 구현 태스크 |
|---|---|
| 잠금 신원 — canonical DB 경로 | Task 1 |
| 실행 리스 / 원자적 획득 | Task 2 |
| 해제 조건 / DB 검증 | Task 3 |
| 재단된 흐름 (폴링을 밖으로) | Task 4, 5 |
| 가드 — 기본 금지 + 명시적 허용 | Task 6 |
| 만료는 회수하지 않는다 | Task 2 (`test_a_stale_lease_is_still_not_reclaimable`) |
| sweeper + outbox | Task 7 |
| 수동 복구 | Task 8 |
| 배포 프로토콜 | Task 9 |
| `live_order_lock` 계약 불변 | Task 5, 6 (제출 허용 표식) |

**남은 결정 — 실행자에게**

- Task 5 Step 4는 세 진입점이 공유할 컨텍스트 매니저 추출을 요구한다. Task 4에서 인라인으로 쓴 코드를 그대로 복제하지 말 것.
- Task 8의 `release_execution_lease_dry_run` 은 Task 3의 조회를 재사용해야 한다. 판정 로직이 두 벌이 되면 둘이 갈라진다.
- Task 4가 기존 테스트를 깨뜨릴 가능성이 가장 높다. 깨진 단정이 `save_approval` 을 직접 기대하는 것이면 `approval_exists` 로 옮기고, 바깥 잠금을 기대하는 것이면 **그 단정을 삭제한다** — 그 잠금을 없애는 것이 이 계획의 목적이다.
