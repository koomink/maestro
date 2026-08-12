# 락 계측 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 파일 락의 보유자·PID·획득 시각을 기록해, 다음 락 타임아웃이 "누가 잡고 있었는지"를 스스로 말하게 한다.

**Architecture:** `StateStore`의 세 락(`writer_lock`, `account_refresh_lock`, `live_order_lock`)은 구조가 동일하다. 공통 컨텍스트 매니저 하나로 합치고 거기에 계측을 넣는다. 락을 잡은 직후 락 파일에 보유자 기록을 쓰고, 해제 시 지우며, 타임아웃 시 그 파일을 읽어 예외 메시지에 담는다. 배타 flock을 쥔 상태에서만 쓰므로 쓰기 경쟁이 없다.

**Tech Stack:** Python 3.12, `fcntl.flock`, SQLite(`StateStore`), pytest, ruff. 신규 의존성 없음.

## Global Constraints

- 스펙: `docs/superpowers/specs/2026-08-12-live-order-contention-design.md`. **관측만 한다** — 락 획득 순서를 바꾸지 않고, 새 예외 경로를 만들지 않고, 동작을 바꾸지 않는다.
- **계측에서 system event를 쓰지 않는다.** 이벤트 기록은 writer 락을 요구하므로 락 원시 안에서 호출하면 재귀한다.
- 기존 `TimeoutError` **타입과 메시지 접두어를 유지한다** (`State writer lock is busy: `, `Live order lock is busy: `, `Account refresh is already running: `). 뒤에 보유자 정보를 덧붙이는 방식이다.
- 락 파일이 비었거나 깨졌을 때 **예외를 던지지 않는다** — `unknown`으로 처리하고 원래 동작을 유지한다.
- 검증: `.venv/bin/python -m pytest tests/ -q` (기준선 **1298 passed, 9 skipped**), `.venv/bin/python -m ruff check src tests --output-format=concise`.
- 커밋 트레일러:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK
  ```

## 배경: 왜 계측이 먼저인가

2026-08-11 리밸런싱이 `TimeoutError: State writer lock is busy`로 절반에서 중단됐다(TIP 23주 매도 체결, 매수 3건 미제출, USD $11,576 미배치). 원인 가설이 둘인데 — 락 순서 역전에 의한 교착이냐, 제3의 장기 writer 보유자냐 — **현재 데이터로 가릴 수 없다.** `writer_lock`과 `live_order_lock`이 `owner` 인자를 받고 즉시 버리기 때문이다(`store.py:290`, `:348`의 `del owner`).

두 가설이 요구하는 구조 수정은 서로 다르고 둘 다 실거래 집행 경로를 건드린다. 증명 없이 고르지 않는다.

## 현재 코드의 사실관계 (구현 전 확인됨)

- `src/maestro/state/store.py`의 세 락은 **거의 동일한 본문**을 가진다:
  - `writer_lock`(283행, 기본 타임아웃 10초, `_lock_depths.writer` 재진입 카운터, `self.lock_path`)
  - `account_refresh_lock`(313행, 기본 0초, **재진입 카운터 없음**, 계좌별 경로를 매번 계산, 메시지에 `account_id` 사용)
  - `live_order_lock`(342행, 기본 30초, `_lock_depths.live_order`, `self.live_order_lock_path`)
- 공통 본문: `open("a+")` → `flock(LOCK_EX|LOCK_NB)` 재시도 루프 → 데드라인 초과 시 `TimeoutError` → `yield` → `finally`에서 `flock(LOCK_UN)`.
- `writer_lock`·`live_order_lock`은 재진입 시 파일을 열지 않고 그대로 `yield`한다.
- `store.py`에 **`os`가 import되어 있지 않다**(현재: `fcntl, json, sqlite3, threading, time`). 추가해야 한다.
- 세 락의 호출자는 모두 `owner`에 문자열 리터럴을 넘긴다(`"approve_signal"`, `"fill_reconciliation"`, `"resolve_pending_signal_approval"` 등) — 시그니처 변경은 없다.
- 같은 프로세스라도 획득 때마다 **새로 `open()`** 하므로 별개의 open file description이 되고, flock은 서로 충돌한다. 다만 `_lock_depths`는 thread-local이라 **다른 스레드**는 재진입 분기를 타지 않고 실제로 경쟁한다.

---

### Task 1: 세 락을 공통 헬퍼로 합친다 (동작 불변)

계측을 세 곳에 세 번 넣지 않기 위해 먼저 합친다. **이 태스크는 동작을 바꾸지 않는다.**

**Files:**
- Modify: `src/maestro/state/store.py` (`writer_lock` 283행, `account_refresh_lock` 313행, `live_order_lock` 342행)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Produces: `StateStore._file_lock(lock_path, *, owner, timeout_seconds, depth_attr, busy_message) -> ContextManager[None]` — 세 공개 락이 이것을 감싼다. `depth_attr`가 `None`이면 재진입 카운터를 쓰지 않는다(`account_refresh_lock`용).

- [ ] **Step 1: 기존 동작을 고정하는 테스트를 쓴다**

세 락이 실제로 배타적인지, 재진입이 동작하는지를 먼저 못 박는다. 리팩터가 이것을 깨면 즉시 드러난다.

`tests/test_state_store.py`는 현재 `sqlite3`, `datetime`, `StateStore`만 import한다. 이 계획의 테스트들은 추가로 `multiprocessing`, `os`, `threading`, `pytest`가 필요하다. 기존 테스트는 `StateStore(str(path), initial_cash=1000)` 형태를 쓰므로 새 테스트도 `initial_cash=0`처럼 키워드로 맞춘다.

```python
import multiprocessing
import time

from maestro.state.store import StateStore


def _hold_writer_lock(db_path, hold_seconds, ready, done):
    store = StateStore(db_path, 0)
    with store.writer_lock("holder"):
        ready.set()
        time.sleep(hold_seconds)
    done.set()


def test_writer_lock_is_exclusive_across_processes(tmp_path):
    db = str(tmp_path / "state.db")
    StateStore(db, 0)  # 스키마 생성
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 2.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError, match="State writer lock is busy"):
            with store.writer_lock("waiter", timeout_seconds=0.3):
                pass
    finally:
        proc.join(timeout=10)


def test_writer_lock_is_reentrant_in_the_same_thread(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("outer"):
        with store.writer_lock("inner", timeout_seconds=0.1):
            pass  # 재진입이 막히면 TimeoutError가 난다


def test_live_order_lock_is_exclusive_and_reentrant(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.live_order_lock("outer"):
        with store.live_order_lock("inner", timeout_seconds=0.1):
            pass


def test_account_refresh_lock_rejects_a_second_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.account_refresh_lock("kis_ps"):
        with pytest.raises(TimeoutError, match="Account refresh is already running"):
            with store.account_refresh_lock("kis_ps"):
                pass
```

`multiprocessing`은 기본 start method가 fork인 리눅스에서 동작한다. 모듈 최상위에 `_hold_writer_lock`을 두는 이유는 spawn 방식에서도 picklable해야 하기 때문이다.

- [ ] **Step 2: 테스트가 현재 코드에서 통과하는지 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q`
Expected: PASS (리팩터 전 기준선을 고정하는 것이 목적이다)

- [ ] **Step 3: 공통 헬퍼로 합친다**

```python
    @contextmanager
    def _file_lock(
        self,
        lock_path: Path,
        *,
        owner: str,
        timeout_seconds: float,
        depth_attr: str | None,
        busy_message: str,
    ) -> Any:
        if depth_attr is not None and getattr(self._lock_depths, depth_attr, 0) > 0:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(busy_message) from exc
                    time.sleep(0.1)
            if depth_attr is not None:
                setattr(self._lock_depths, depth_attr, getattr(self._lock_depths, depth_attr, 0) + 1)
            try:
                yield
            finally:
                if depth_attr is not None:
                    setattr(self._lock_depths, depth_attr, getattr(self._lock_depths, depth_attr) - 1)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
```

세 공개 락을 이것으로 감싼다. `owner`는 아직 쓰이지 않지만 **`del owner`를 지우고 헬퍼로 넘긴다** — Task 2에서 쓴다.

```python
    @contextmanager
    def writer_lock(self, owner: str, *, timeout_seconds: float = 10.0) -> Any:
        with self._file_lock(
            self.lock_path,
            owner=owner,
            timeout_seconds=timeout_seconds,
            depth_attr="writer",
            busy_message=f"State writer lock is busy: {self.lock_path}",
        ):
            yield

    @contextmanager
    def account_refresh_lock(self, account_id: str, *, timeout_seconds: float = 0.0) -> Any:
        safe_account_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in account_id
        )
        lock_path = self.path.with_suffix(self.path.suffix + f".refresh-{safe_account_id}.lock")
        with self._file_lock(
            lock_path,
            owner=f"account_refresh:{account_id}",
            timeout_seconds=timeout_seconds,
            depth_attr=None,
            busy_message=f"Account refresh is already running: {account_id}",
        ):
            yield

    @contextmanager
    def live_order_lock(self, owner: str, *, timeout_seconds: float = 30.0) -> Any:
        with self._file_lock(
            self.live_order_lock_path,
            owner=owner,
            timeout_seconds=timeout_seconds,
            depth_attr="live_order",
            busy_message=f"Live order lock is busy: {self.live_order_lock_path}",
        ):
            yield
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS — 기준선 1298 + 신규 4건

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "refactor: unify the three state store file locks"
```

---

### Task 2: 보유자를 락 파일에 기록한다

**Files:**
- Modify: `src/maestro/state/store.py` (`_file_lock`, import 추가)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: Task 1의 `_file_lock(..., owner=...)`
- Produces: `StateStore.read_lock_holder(lock_path: Path) -> dict[str, Any] | None` — 현재 보유자 기록. 파일이 없거나 비었거나 깨졌으면 `None`. 기록 형식은 `{"owner": str, "pid": int, "acquired_at": str}`(ISO 8601 UTC).
- Produces: `StateStore.writer_lock_path` / `live_order_lock_path` — 진단 시 `read_lock_holder`에 넘길 경로. `live_order_lock_path`는 이미 있으므로(`store.py:26`) `writer_lock_path`만 `lock_path`의 별칭으로 노출한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_lock_file_records_the_holder_while_held(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("approve_signal"):
        holder = store.read_lock_holder(store.writer_lock_path)
        assert holder is not None
        assert holder["owner"] == "approve_signal"
        assert holder["pid"] == os.getpid()
        assert holder["acquired_at"]  # ISO 문자열


def test_lock_file_is_cleared_after_release(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("approve_signal"):
        pass
    assert store.read_lock_holder(store.writer_lock_path) is None


def test_reentrant_acquisition_keeps_the_outer_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.writer_lock("outer"):
        with store.writer_lock("inner"):
            holder = store.read_lock_holder(store.writer_lock_path)
            assert holder["owner"] == "outer"


def test_read_lock_holder_tolerates_a_corrupt_file(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    store.writer_lock_path.write_text("깨진 내용 not json", encoding="utf-8")
    assert store.read_lock_holder(store.writer_lock_path) is None


def test_live_order_lock_records_its_own_holder(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), 0)
    with store.live_order_lock("resolve_pending_signal_approval"):
        holder = store.read_lock_holder(store.live_order_lock_path)
        assert holder["owner"] == "resolve_pending_signal_approval"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q -k "holder or reentrant or corrupt"`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'read_lock_holder'`

- [ ] **Step 3: 구현한다**

`store.py` 상단에 `import os`를 추가한다(현재 없다).

`__init__`에 별칭을 더한다 — `self.lock_path` 바로 뒤:

```python
        self.writer_lock_path = self.lock_path
```

`_file_lock`의 flock 획득 직후와 해제 직전에 기록을 넣는다:

```python
            self._write_lock_holder(lock_file, owner)
            if depth_attr is not None:
                setattr(self._lock_depths, depth_attr, getattr(self._lock_depths, depth_attr, 0) + 1)
            try:
                yield
            finally:
                if depth_attr is not None:
                    setattr(self._lock_depths, depth_attr, getattr(self._lock_depths, depth_attr) - 1)
                self._clear_lock_holder(lock_file)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
```

보조 메서드 세 개:

```python
    @staticmethod
    def _write_lock_holder(lock_file: Any, owner: str) -> None:
        """배타 flock을 쥔 상태에서만 호출된다 — 쓰기 경쟁이 없다."""
        record = {
            "owner": owner,
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps(record, ensure_ascii=False))
        lock_file.flush()

    @staticmethod
    def _clear_lock_holder(lock_file: Any) -> None:
        """해제 전에 지운다. 다음 대기자가 낡은 기록을 보유자로 오해하지 않게 한다."""
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.flush()

    @staticmethod
    def read_lock_holder(lock_path: Path) -> dict[str, Any] | None:
        """락을 잡지 않고 읽는다. 진단용이므로 어떤 실패도 None으로 흡수한다."""
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None
```

`datetime`은 이미 import되어 있으나 `UTC`는 없을 수 있다 — `from datetime import UTC, datetime`으로 맞춘다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: record the holder in state store lock files"
```

---

### Task 3: 타임아웃 메시지에 보유자를 담는다

**Files:**
- Modify: `src/maestro/state/store.py` (`_file_lock`의 타임아웃 분기)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: Task 2의 `read_lock_holder`, `_write_lock_holder`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

다른 프로세스가 잡고 있어야 flock이 실제로 경쟁한다. Task 1에서 만든 `_hold_writer_lock` 헬퍼를 재사용한다.

```python
def test_timeout_message_names_the_holder(tmp_path):
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_writer_lock, args=(db, 3.0, ready, done))
    proc.start()
    try:
        assert ready.wait(timeout=10)
        store = StateStore(db, 0)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.3):
                pass
        message = str(exc_info.value)
        assert "State writer lock is busy" in message   # 기존 접두어 유지
        assert "holder" in message
        assert str(proc.pid) in message                  # 실제 보유자의 PID
        assert "waited" in message
    finally:
        proc.join(timeout=10)


def test_timeout_message_says_unknown_when_the_record_is_missing(tmp_path, monkeypatch):
    """기록이 없어도 예외 타입과 접두어는 그대로다."""
    store = StateStore(str(tmp_path / "state.db"), 0)
    monkeypatch.setattr(StateStore, "_write_lock_holder", staticmethod(lambda *a, **k: None))
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    # 같은 프로세스의 다른 스레드로 경쟁시킨다 — _lock_depths가 thread-local이라 실제로 막힌다
    import threading

    def hold():
        with store.writer_lock("holder"):
            ready.set()
            done.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(timeout=5)
        with pytest.raises(TimeoutError) as exc_info:
            with store.writer_lock("victim", timeout_seconds=0.3):
                pass
        assert "State writer lock is busy" in str(exc_info.value)
        assert "unknown" in str(exc_info.value)
    finally:
        done.set()
        thread.join(timeout=5)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q -k "timeout_message"`
Expected: FAIL — 메시지에 `holder`/`waited`가 없다

- [ ] **Step 3: 구현한다**

`_file_lock`의 타임아웃 분기를 바꾼다. `started`를 루프 진입 전에 잡아 대기 시간을 잰다.

```python
        deadline = time.monotonic() + timeout_seconds
        started = time.monotonic()
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"{busy_message} ({self._describe_lock_holder(lock_path)}, "
                            f"waited {time.monotonic() - started:.1f}s)"
                        ) from exc
                    time.sleep(0.1)
```

```python
    @classmethod
    def _describe_lock_holder(cls, lock_path: Path) -> str:
        holder = cls.read_lock_holder(lock_path)
        if not holder:
            return "holder unknown"
        return (
            f"holder {holder.get('owner', 'unknown')} "
            f"pid={holder.get('pid', 'unknown')} "
            f"since {holder.get('acquired_at', 'unknown')}"
        )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 기존 메시지에 의존하는 곳이 없는지 확인한다**

Run: `grep -rn "lock is busy\|already running" src tests --include=*.py`
접두어를 유지했으므로 부분 일치는 그대로 통과해야 한다. 완전 일치(`==`)로 비교하는 곳이 있으면 부분 일치로 고친다.

- [ ] **Step 6: 커밋**

```bash
git add -A
git commit -m "feat: name the lock holder in timeout errors"
```

---

## 검증

- 각 태스크: 명시된 pytest 명령으로 RED 확인 → 구현 → GREEN 확인 → 커밋
- 최종: `.venv/bin/python -m pytest tests/ -q` (기준선 1298 passed / 9 skipped 이상), `.venv/bin/python -m ruff check src tests --output-format=concise`

## 배포 확인

- 운영자 서비스 재시작 후, 락 파일에 기록이 실제로 쓰이는지 **읽기 전용으로** 확인한다:
  ```bash
  cat /root/maestro-operator/var/symphony_state.db.lock; echo
  cat /root/maestro-operator/var/symphony_state.db.live.lock; echo
  ```
  평상시에는 비어 있고(아무도 안 잡고 있음), 신호 런이 도는 동안 보유자 기록이 보여야 한다.
- 명령 메뉴는 바뀌지 않으므로 `telegram-set-commands` 재실행은 불필요하다.
- **최종 성공 기준은 배포 시점에 확인되지 않는다.** 다음 락 타임아웃이 발생했을 때 `TimeoutError` 메시지가 보유자를 지목하면 성공이다. 그때까지 원인은 미확정으로 남는다.

## 다음 단계

관측이 원인을 지목하면 그 증거로 구조 수정을 설계한다:

| 관측된 보유자 | 다음 조치 |
|---|---|
| `fill_reconciliation` 계열 | 가설 A — 락 획득 순서 통일 |
| `approve_signal` / `run_once` 등 장기 보유자 | 가설 B — writer 임계구역 분할 + 승인 단일소비 CAS 재설계 |

그 밖에 스펙이 이월한 항목: 2부(당일 재계산·재승인 복구, 세대 펜싱), 3부(취소 terminal 상태 머신). 3a-2·3a-3은 그 뒤다.
