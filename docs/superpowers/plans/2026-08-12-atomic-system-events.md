# 3a-2 — `save_system_events_atomic` 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 여러 system event를 duplicate_key 전제조건과 함께 **하나의 SQLite 트랜잭션**으로 커밋하는 `StateStore.save_system_events_atomic`을 만든다. 이것이 3a 이후 모든 CAS(compare-and-swap) 전이의 기반 원시(primitive)다.

**Architecture:** 새 메서드 하나를 `src/maestro/state/store.py`에 추가한다. 기존
`apply_account_cash_flows`(`store.py:760-885`)가 이미 "여러 원장 leg + 여러
system event를 한 트랜잭션에 커밋하고, duplicate_key 집합이 이미 존재하면
멱등 no-op으로 되돌아온다"는 패턴을 확립해 두었다. 그 패턴을 **원장 없이
system event만** 다루도록 일반화하고, 여기에 **전제조건(precondition)**을
더한다. 전제조건은 트랜잭션 안에서 평가되므로 TOCTOU가 없다.

**Tech Stack:** Python 3.12, sqlite3(WAL), pytest, ruff.

## 배경 — 왜 필요한가

`docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md:258-286`이
요구한다. funding 요청 교체는 세 개의 기록을 남긴다 — 신규 요청, 이전 요청
`superseded`, 새 workflow head. 지금은 이것들을 따로 쓰므로 중간에 프로세스가
죽으면 **head에 연결되지 않은 orphan 요청** 또는 **실체 없는 dangling head**가
남는다. 스펙이 요구하는 해법은 "state store가 단일 SQLite이므로 셋을 한
트랜잭션으로 커밋하는 API를 신설한다"이다.

두 번째 요구는 claim의 CAS다(`:275-286`). "head 조회 → 별도 claim 기록"
순서에는 그 사이에 다른 run이 head를 교체하는 TOCTOU가 있다. claim 삽입은
**같은 트랜잭션 안에서 head가 기대값 그대로인지 재검증한 뒤에만** 일어나야
한다.

세 번째 소비자는 아직 계획서가 없지만 이미 확정된 필요다: `approve_signal`이
`writer_lock`을 최대 10분 쥐는 이유가 "승인 조회 ~ 소비 기록"의 원자성을
락으로 얻고 있기 때문이며, 그 임계구역을 쪼개려면 소비 기록을 CAS로 바꿔야
한다. 이 API가 그 도구다. **본 계획은 도구만 만들고 호출자는 바꾸지 않는다.**

## 설계 결정

### 전제조건을 duplicate_key 존재/부재로 표현한다

스펙은 "현재 head의 request_id와 version이 기대값과 일치"를 재검증하라고
쓰지만, 그 검증은 payload를 파싱하지 않고 표현할 수 있다. head 이벤트는
`duplicate_key = head:<workflow_id>:v<version>`으로 기록되고 **한 번 쓰이면
불변**이다. 따라서

- "head가 version n이다" ≡ `head:<wf>:v<n>`가 **존재**하고 `head:<wf>:v<n+1>`가 **부재**
- 그 head의 request_id는 v\<n\> 행이 불변이므로 함께 확정된다

payload 질의 대신 키 존재/부재로 두면 기존
`idx_system_events_duplicate_key` 유니크 인덱스를 그대로 타고, 전제조건
표현식에 파서를 만들 필요가 없다. 그래서 API는 두 개의 키 목록만 받는다.

### 반환값은 예외가 아니라 결과 dict다

CAS 실패는 버그가 아니라 **정상적인 경쟁 결과**다(구 콜백이 졌다). 호출자는
그것을 사용자 메시지로 바꿔야 하므로 예외가 아니라 값으로 받는다.
`apply_account_cash_flows`의 반환 규약과 같은 방향이다.

단 **프로그래밍 오류는 예외다**: 이벤트가 없거나, duplicate_key가 없는
이벤트가 섞였거나, 배치 안에서 키가 중복되면 `ValueError`.

### 부분 존재는 예외다

배치의 키 중 **일부만** 이미 존재하는 상태는 이 API를 거쳐서는 만들어질 수
없다(한 트랜잭션이므로 전부 아니면 전무). 그런 상태가 관측되면 호출자가 서로
다른 배치에 같은 키를 재사용한 것이며, 나머지를 마저 커밋하면 반쪽짜리 전이가
완성된다. `apply_account_cash_flows:825-833`의 선례대로 **거부한다** —
추측하지 않는다.

### 검사 순서

1. **자기 키가 전부 존재** → `already_committed`, 아무것도 쓰지 않는다.
2. **전제조건** (`require` → `forbid`).
3. **자기 키가 일부만 존재** → `ValueError`.

1번이 2번보다 앞서는 이유는 재실행(replay) 판정이다. head v2를 쓰는 배치가
`forbid=["head:wf:v2"]`를 걸었다면, 이미 커밋된 배치의 재실행은 전제조건에서
먼저 걸려 "경쟁에 졌다"로 오분류된다. 자기 키가 전부 존재하면 그건 **내가
이미 커밋한 것**이므로 멱등 성공으로 되돌려준다.

> **개정 (2026-08-12, Task 3 구현 중 발견).** 원래는 "자기 키 검사(전부·일부
> 모두)가 전제조건보다 먼저"라고 썼다. **그러면 `forbid_duplicate_keys`가
> 이 문서가 광고하는 head 전이 형태에서 도달 불가능해진다.** 세 프로세스가
> 같은 `head:wf:v2`를 두고 경쟁하고 각자 고유한 `req:N`을 함께 쓸 때, 진 쪽은
> 자기 키가 **일부만** 존재하는 상태가 되어 `forbid` 평가에 닿기 전에
> `ValueError`로 죽는다. 정상적인 CAS 패배가 예외가 된다.
>
> 3번을 2번 뒤로 내렸다. 선언된 전제조건은 **"누가 먼저 도착했다는 게 어떤
> 모습인지"를 호출자가 직접 말한 것**이다. 그것이 성립하면 답은 "경쟁에서
> 졌다"이지 예외가 아니다. 일부 겹침의 `ValueError`는 전제조건을 아무것도
> 선언하지 않은 호출자에 대한 방어선으로 남는다 — 그 경우의 겹침은 설명되지
> 않은 키 충돌이고, 크게 터뜨리는 것이 맞다.

## Global Constraints

- 이 계획은 **API만 추가한다.** 기존 호출자·기존 메서드·락 획득 순서·타임아웃을
  일절 바꾸지 않는다.
- 모든 쓰기는 `self.writer_lock("save_system_events_atomic")` 안에서, 단일
  `with self._connect() as conn:` 트랜잭션으로 이뤄진다. 새 락을 만들지 않는다.
- duplicate_key는 payload에서 기존 `_system_event_duplicate_key`
  (`store.py:1949`)로 뽑는다. 새 규칙을 만들지 않는다.
- `broker_order_id`·`order_id` 컬럼도 기존 `_system_event_broker_order_id`·
  `_system_event_order_id`로 채운다 — `save_portfolio_snapshot_with_event`
  (`store.py:658-670`)와 같은 방식.
- 테스트는 `tests/test_state_store.py`에 추가한다. 프로세스 간 검증이 필요한
  테스트는 그 파일에 이미 있는 `multiprocessing.Process` + `Event` 패턴을
  따른다(같은 프로세스의 flock은 재획득되므로 경쟁이 성립하지 않는다).
- 검증: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m ruff check src tests --output-format=concise`.
- 커밋 trailer:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK
  ```

## File Structure

- `src/maestro/state/store.py` — `save_system_events_atomic` 추가.
  `from collections.abc import Mapping, Sequence` 임포트 추가.
- `tests/test_state_store.py` — 테스트 추가.

---

### Task 1: 다중 이벤트 원자 커밋 (검증 + 멱등 재실행)

**Files:**
- Modify: `src/maestro/state/store.py` (임포트 1줄, 새 메서드)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: `_system_event_duplicate_key`, `_system_event_broker_order_id`,
  `_system_event_order_id`, `self.writer_lock`, `self._connect`
- Produces:
  ```python
  def save_system_events_atomic(
      self,
      run_id: str,
      events: Sequence[Mapping[str, Any]],
  ) -> dict[str, Any]:
  ```
  각 event는 `{"event_type": str, "payload": dict}`. 반환값은
  `{"committed": bool, "conflict": str | None, "conflicting_keys": tuple[str, ...]}`.
  Task 2가 여기에 키워드 전용 인자 두 개를 더한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_state_store.py`에 추가:

```python
def test_atomic_events_commit_together(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    result = store.save_system_events_atomic(
        "run-1",
        [
            {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
        ],
    )
    assert result["committed"] is True
    assert result["conflict"] is None
    assert store.duplicate_key_exists("req:1")
    assert store.duplicate_key_exists("head:wf:v1")


def test_atomic_events_replay_is_an_idempotent_no_op(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    events = [
        {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
        {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
    ]
    store.save_system_events_atomic("run-1", events)
    result = store.save_system_events_atomic("run-1", events)
    assert result["committed"] is False
    assert result["conflict"] == "already_committed"
    assert len(store.list_system_events_by_type("funding_request", limit=None)) == 1


def test_atomic_events_reject_a_partial_overlap(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}}],
    )
    with pytest.raises(ValueError, match="partial"):
        store.save_system_events_atomic(
            "run-2",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
            ],
        )
    assert not store.duplicate_key_exists("head:wf:v1")


def test_atomic_events_require_a_duplicate_key_on_every_event(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="duplicate key"):
        store.save_system_events_atomic(
            "run-1",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_workflow_head", "payload": {}},
            ],
        )
    assert not store.duplicate_key_exists("req:1")


def test_atomic_events_reject_duplicate_keys_within_one_batch(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="unique"):
        store.save_system_events_atomic(
            "run-1",
            [
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
                {"event_type": "funding_request", "payload": {"duplicate_key": "req:1"}},
            ],
        )


def test_atomic_events_reject_an_empty_batch(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    with pytest.raises(ValueError, match="at least one"):
        store.save_system_events_atomic("run-1", [])


def test_atomic_events_carry_broker_and_order_ids(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [
            {
                "event_type": "live_order_submit_intent",
                "payload": {
                    "duplicate_key": "intent:o-1",
                    "broker_order_id": "b-1",
                    "order_id": "o-1",
                },
            }
        ],
    )
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        row = conn.execute(
            "SELECT broker_order_id, order_id FROM system_events WHERE duplicate_key = ?",
            ("intent:o-1",),
        ).fetchone()
    assert row == ("b-1", "o-1")
```

파일 상단에 `import sqlite3`와 `import pytest`가 없으면 추가한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -k atomic -q`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'save_system_events_atomic'`

- [ ] **Step 3: 구현한다**

`store.py` 상단 임포트에 추가:

```python
from collections.abc import Mapping, Sequence
```

`apply_account_cash_flows` 바로 뒤에 메서드를 추가한다:

```python
    def save_system_events_atomic(
        self,
        run_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Commit several system events in one transaction, or none of them.

        A state transition that spans more than one event — a new request, the
        previous one superseded, the workflow head that points at the new one —
        only means anything as a set.  Writing them separately permits a crash
        that leaves a request nothing points to, or a head pointing at nothing.

        Every event must carry a ``duplicate_key``.  The keys are what make a
        retry safe: if all of them are already on record this call is a replay
        of a batch that already landed, and it returns without writing.
        """
        prepared = _prepare_atomic_system_events(events)
        keys = [item["duplicate_key"] for item in prepared]
        with self.writer_lock("save_system_events_atomic"):
            with self._connect() as conn:
                existing = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT duplicate_key FROM system_events "
                        f"WHERE duplicate_key IN ({','.join('?' * len(keys))})",
                        keys,
                    ).fetchall()
                }
                if len(existing) == len(keys):
                    return {
                        "committed": False,
                        "conflict": "already_committed",
                        "conflicting_keys": tuple(sorted(existing)),
                    }
                if existing:
                    # Half of this transition is already on record.  Committing
                    # the rest would finish a transition nobody asked for, so
                    # refuse rather than guess which half is authoritative.
                    raise ValueError(
                        "atomic system events conflict with an existing partial "
                        f"record: {sorted(existing)}"
                    )
                for item in prepared:
                    conn.execute(
                        "INSERT INTO system_events "
                        "(run_id, event_type, payload, duplicate_key, "
                        "broker_order_id, order_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            item["event_type"],
                            json.dumps(item["payload"], default=str),
                            item["duplicate_key"],
                            item["broker_order_id"],
                            item["order_id"],
                        ),
                    )
                return {"committed": True, "conflict": None, "conflicting_keys": ()}
```

모듈 하단, `_system_event_duplicate_key` 근처에 헬퍼를 추가한다:

```python
def _prepare_atomic_system_events(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not events:
        raise ValueError("at least one system event is required")
    prepared: list[dict[str, Any]] = []
    for event in events:
        payload = dict(event["payload"])
        duplicate_key = _system_event_duplicate_key(payload)
        if not duplicate_key:
            raise ValueError("every atomic system event needs a duplicate key")
        prepared.append(
            {
                "event_type": str(event["event_type"]),
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
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -k atomic -q`
Expected: PASS (7 tests)

Run: `.venv/bin/python -m ruff check src tests --output-format=concise`
Expected: All checks passed

- [ ] **Step 5: 커밋한다**

```bash
git add src/maestro/state/store.py tests/test_state_store.py
git commit -m "feat(state): commit linked system events in one transaction"
```

---

### Task 2: 트랜잭션 내부 전제조건 (CAS)

**Files:**
- Modify: `src/maestro/state/store.py` (`save_system_events_atomic`)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: Task 1의 `save_system_events_atomic`
- Produces: 같은 메서드에 키워드 전용 인자 두 개
  ```python
  def save_system_events_atomic(
      self,
      run_id: str,
      events: Sequence[Mapping[str, Any]],
      *,
      require_duplicate_keys: Sequence[str] = (),
      forbid_duplicate_keys: Sequence[str] = (),
  ) -> dict[str, Any]:
  ```
  `conflict` 값이 두 개 늘어난다: `"precondition_missing"`, `"precondition_present"`.
  `conflicting_keys`는 그 판정을 유발한 키만 담는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_atomic_events_require_an_existing_key(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    result = store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_missing"
    assert result["conflicting_keys"] == ("head:wf:v1",)
    assert not store.duplicate_key_exists("claim:1")


def test_atomic_events_commit_when_the_required_key_is_present(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}}],
    )
    result = store.save_system_events_atomic(
        "run-2",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    assert result["committed"] is True
    assert store.duplicate_key_exists("claim:1")


def test_atomic_events_refuse_when_a_forbidden_key_appeared(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    store.save_system_events_atomic(
        "run-1",
        [
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}},
            {"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}},
        ],
    )
    result = store.save_system_events_atomic(
        "run-2",
        [{"event_type": "funding_workflow_claim", "payload": {"duplicate_key": "claim:1"}}],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    assert result["committed"] is False
    assert result["conflict"] == "precondition_present"
    assert result["conflicting_keys"] == ("head:wf:v2",)
    assert not store.duplicate_key_exists("claim:1")


def test_atomic_events_replay_wins_over_a_failing_precondition(tmp_path):
    """A batch that already landed reports success, not a lost race.

    The head this batch wrote is exactly what its own forbid-precondition
    names, so evaluating preconditions first would misreport the replay.
    """
    store = StateStore(str(tmp_path / "s.db"))
    events = [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v2"}}]
    first = store.save_system_events_atomic(
        "run-1", events, forbid_duplicate_keys=["head:wf:v2"]
    )
    assert first["committed"] is True
    second = store.save_system_events_atomic(
        "run-1", events, forbid_duplicate_keys=["head:wf:v2"]
    )
    assert second["conflict"] == "already_committed"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -k "atomic and precondition or atomic and required or atomic and forbidden or atomic and replay_wins" -q`
Expected: FAIL — `TypeError: save_system_events_atomic() got an unexpected keyword argument 'require_duplicate_keys'`

- [ ] **Step 3: 구현한다**

시그니처에 키워드 전용 인자를 더하고, **자기 키 검사 뒤·INSERT 앞**에 전제조건
평가를 넣는다:

```python
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
                            "conflicting_keys": tuple(missing),
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
```

docstring에 한 문단을 더한다:

```
        ``require_duplicate_keys`` and ``forbid_duplicate_keys`` are evaluated
        inside the same transaction as the inserts, which is the whole point:
        reading the workflow head and then writing a claim in a separate
        statement leaves a window for another run to replace the head in
        between.  A batch whose own keys are all present is a replay and is
        reported as such before the preconditions are consulted.
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -k atomic -q`
Expected: PASS (11 tests)

Run: `.venv/bin/python -m ruff check src tests --output-format=concise`
Expected: All checks passed

- [ ] **Step 5: 커밋한다**

```bash
git add src/maestro/state/store.py tests/test_state_store.py
git commit -m "feat(state): evaluate duplicate-key preconditions inside the commit"
```

---

### Task 3: 실제 경쟁·롤백 증명

**Files:**
- Test: `tests/test_state_store.py`

**Interfaces:**
- Consumes: Task 1·2의 `save_system_events_atomic`
- Produces: 없음 (테스트 전용)

이 태스크는 앞의 두 태스크가 *주장*한 것 — 원자성과 CAS — 을 **실제 경쟁**과
**실제 실패**로 증명한다. 단일 프로세스 단언은 트랜잭션 경계를 증명하지
않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

파일 상단(모듈 수준, `_hold_writer_lock` 옆)에 자식 프로세스 진입점을 둔다 —
`multiprocessing`이 pickle 할 수 있어야 하므로 모듈 수준이어야 한다:

```python
def _race_workflow_head(db_path, attempt, ready, results):
    from maestro.state.store import StateStore

    store = StateStore(db_path)
    ready.wait(5.0)
    outcome = store.save_system_events_atomic(
        f"run-{attempt}",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {"duplicate_key": "head:wf:v2", "attempt": attempt},
            },
            {
                "event_type": "funding_request",
                "payload": {"duplicate_key": f"req:{attempt}"},
            },
        ],
        require_duplicate_keys=["head:wf:v1"],
        forbid_duplicate_keys=["head:wf:v2"],
    )
    results.append((attempt, outcome["committed"], outcome["conflict"]))
```

테스트:

```python
def test_only_one_process_wins_the_head_transition(tmp_path):
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    store.save_system_events_atomic(
        "run-0",
        [{"event_type": "funding_workflow_head", "payload": {"duplicate_key": "head:wf:v1"}}],
    )
    ready = multiprocessing.Event()
    with multiprocessing.Manager() as manager:
        results = manager.list()
        procs = [
            multiprocessing.Process(target=_race_workflow_head, args=(db, n, ready, results))
            for n in (1, 2, 3)
        ]
        for proc in procs:
            proc.start()
        ready.set()
        for proc in procs:
            proc.join(30.0)
            assert proc.exitcode == 0
        outcomes = list(results)

    winners = [item for item in outcomes if item[1] is True]
    assert len(winners) == 1
    assert {item[2] for item in outcomes if item[1] is False} == {"precondition_present"}
    # The loser's request event must not exist: its batch was all-or-nothing.
    winning_attempt = winners[0][0]
    for attempt in (1, 2, 3):
        assert store.duplicate_key_exists(f"req:{attempt}") is (attempt == winning_attempt)


```

> **개정 (2026-08-12, Task 1 리뷰 반영).** 원래 이 태스크에는
> `sqlite3.Connection.execute`를 monkeypatch해 두 번째 INSERT를 실패시키는
> 롤백 테스트가 있었다. **Task 1의 수정 라운드로 옮겼다** — Task 2가 이 위에
> CAS를 얹기 전에 원자성이 고정돼 있어야 하고, 전역 클래스 메서드 패치보다
> 직렬화 불가 payload로 두 번째 이벤트를 실패시키는 편이 견고하다. 여기
> 남는 것은 **프로세스 간 경쟁** 증명뿐이다.

- [ ] **Step 2: 실패를 확인한다**

경쟁 테스트는 구현이 이미 있으므로 통과할 수 있다. 그럴 경우 **비공허성을
mutation으로 확인한다**: `save_system_events_atomic`에서 `forbid` 평가
블록을 잠시 제거하고 테스트를 돌린다.

Run: `.venv/bin/python -m pytest tests/test_state_store.py -k one_process_wins -q`
Expected(mutation 상태): FAIL — 승자가 둘 이상 나온다.
그 다음 mutation을 되돌리고 다시 돌려 PASS를 확인한다. 두 결과를 모두
보고한다 — mutation에서 실패하지 않는 테스트는 원하는 테스트가 아니다.

- [ ] **Step 3: 전체 스위트와 린트**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 기존 기준선 + 13 (앞선 태스크의 테스트 포함)

Run: `.venv/bin/python -m ruff check src tests --output-format=concise`
Expected: All checks passed

- [ ] **Step 4: 커밋한다**

```bash
git add tests/test_state_store.py
git commit -m "test(state): prove atomic commits are all-or-nothing under real contention"
```

---

## 하지 않는 것

- **호출자 변경 없음.** funding head 상태 머신, claim 전이, `approve_signal`
  임계구역 분할은 각각 별도 계획이다. 본 계획은 도구만 만든다.
- **payload 기반 전제조건 표현식 없음.** 키 존재/부재로 충분하며, 필요해지면
  그때 확장한다.
- **배치 크기 제한 없음.** 이 API의 배치는 설계상 한 전이의 이벤트 몇 개이며,
  SQLite 변수 한도(999)에 접근할 경로가 없다.
- **락 구조 변경 없음.** 기존 `writer_lock`을 그대로 쓴다.

## 검증 요약

- `.venv/bin/python -m pytest tests/ -q`
- `.venv/bin/python -m ruff check src tests --output-format=concise`
- 성공 기준: 세 프로세스가 같은 head 전이를 동시에 시도할 때 **정확히 하나만**
  커밋하고, 진 쪽의 이벤트는 **한 행도 남지 않는다.**
