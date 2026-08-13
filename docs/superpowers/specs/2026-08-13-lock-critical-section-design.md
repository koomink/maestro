# 잠금 임계구역 재설계 — 불변식을 스키마로, 잠금은 상태 전이로

날짜: 2026-08-13
선행: `2026-08-12-live-order-contention-design.md` (락 계측)

## 배경 — 관측이 원인을 확정했다

`2026-08-12` 스펙은 원인을 **미확정**으로 남기고 계측만 했다. 그 계측이 다음 경합에서 답을 냈다.

8/12 22:44 KST, US 로테이션이 8/11과 같은 방식으로 중단됐다. 예외 메시지가 양쪽 보유자를 지목했다:

```
State writer lock is busy: /root/maestro-operator/var/symphony_state.db.lock
  waiter insert:system_events pid=2007964,
  holder fill_reconciliation pid=2064322 since 13:44:00 held 24.2s,
  live_order_lock holder resolve_pending_signal_approval pid=2007964
    since 13:42:40 held 104.2s,
  waited 10.0s, host load1=0.01 runnable=2/408 mem_avail=582.0MB
```

- pid 2007964 = telegram operator. `live_order_lock` 보유(104.2초), `writer_lock` 대기.
- pid 2064322 = `maestro-resume-order-tracking`. `writer_lock` 보유(24.2초), `live_order_lock` 대기.

**가설 A(락 순서 역전)가 확정됐다.** `load1=0.01`이 가설 C(자원 고갈)를, writer 보유 24.2초가 가설 B(장기 writer 보유)를 배제한다.

순서 역전 자체는 2026-08-13에 수정·배포했다(main `7953e61`). 이 스펙은 **그다음 층**을 다룬다.

## 문제 — 잠금이 불변식의 집행 수단이 되어 있다

순서를 통일해도 남는 것이 있다. `resolve_pending_signal_approval`은 다음 전 구간에 `live_order_lock`을 건다:

```
live_order_lock {
    패키지 로드
    capacity 검증          ← 브로커 I/O
    save_approval          ← 단일 소비
    브로커 주문 제출
    체결 폴링              ← 최대 20회 × 30초 = 10분
    완료 기록
}
```

이 잠금이 지키려는 것은 "승인 하나는 한 번만 집행된다"이다. 그런데 그 구간 안에 네트워크 호출과 최대 10분의 대기가 들어 있다.

**이것이 범주 오류다.** 잠금은 짧은 상태 전이를 보호하는 도구이지, 외부 I/O가 끝날 때까지 시스템을 멈추는 도구가 아니다. 이 오류가 남는 한 순서를 아무리 잘 지켜도:

- 2분마다 도는 `resume-order-tracking`이 10~30초 타임아웃으로 계속 실패한다
- 새로운 형태의 경합이 언제든 재발할 수 있다

그리고 결정적으로, **이 잠금은 이미 존재하는 보장을 휘발적으로 중복하고 있다.**

## 잠금 계약

각 잠금이 무엇을 지키는지 명시한다. 지금까지 문서화된 적이 없어 필요할 때마다 범위가 넓어졌다.

### `live_order_lock` — "브로커에 나가는 주문 한 건의 결정"

지키는 것: 일일 한도·중복 검사 → 제출 → 기록. 이 셋이 원자적이지 않으면 동시 제출 두 건이 같은 한도를 각자 통과한다.

보유 시간 상한: **브로커 왕복 1회.**

**변경 없음.** `submit_approved_order`(`live_order_safety.py:47`)가 이미 정확히 이 모양이다.

### `writer_lock` — "SQLite 쓰기 트랜잭션 하나"

지키는 것: 개별 상태 전이.

보유 시간 상한: **트랜잭션 1개.**

**변경 없음.** `StateStore` 메서드마다 이미 짧게 잡는다.

### 바깥 보유 — 삭제

`resolve_pending_signal_approval`의 전 구간 보유가 제거 대상이다.

근거: **exactly-once는 이미 스키마가 보장한다.** `StateStore.save_approval`은 중복 `approval_id`에 `ValueError`를 던지고, 브로커 호출보다 **먼저** 기록된다. `handlers.py:1760`의 주석이 이를 "2차 방어선"으로 이미 명시하고 있다 — 늦게 잠금을 잡은 시도는 아무것도 제출하지 못하고 실패한다.

즉 이 재설계는 새 보장 장치를 만드는 일이 아니라 **덧씌운 잠금을 걷어내는 일**이다. 변경 표면이 작고, 그만큼 안전하다.

### 계약에서 나오는 규칙

> **잠금은 상태 전이를 보호한다. 불변식은 스키마가 보장한다.**
>
> 불변식을 지키려고 잠금 보유를 늘리고 싶어지면, 그것은 스키마에 제약이 빠졌다는 신호다.

## 재단된 흐름

```
[잠금 없음]    패키지 로드, 형상 검증, capacity 사전 필터 (브로커 I/O)
[짧은 writer]  save_approval                        ← 정확히 한 번이 여기서 확정
[잠금 없음]    주문 제출 — 건별로 submit_approved_order가 짧게 live 획득
[잠금 없음]    체결 폴링 — 상태 기록마다 writer 짧게
[짧은 writer]  완료 기록
```

### capacity 검사는 사전 필터로 격하한다 — 의도적 약화

`OrderCapacityService.partition`은 브로커 조회(`lookup`)를 파티셔닝 도중 인라인으로 호출한다. 조회와 판정이 예약 누계 계산으로 얽혀 있어 "조회는 잠금 밖, 판정은 안"으로 쪼갤 수 없다. 쪼개려면 사전 조회 결과를 주입받도록 `partition`을 재작성해야 한다.

그렇게 하지 않는다. **capacity 검사는 원래부터 구속력 있는 검사가 아니다.** `order_capacity.py:72-77`의 주석이 스스로 밝히듯 모든 측정치가 pre-fill 판독이며, 로테이션의 매수는 함께 낸 매도가 결제되기 전까지 매수여력 0으로 보인다. 구속력 있는 방어선은 두 곳에 따로 있다:

1. `submit_approved_order`의 일일 한도·중복 검사 — 짧은 `live_order_lock` 안에 그대로 남는다
2. 브로커 자신의 거부

따라서 capacity 파티션 전체를 잠금 밖으로 내보낸다. 잠금 밖에서 읽은 값이 제출 시점에 낡을 수 있으나, **잠금 안에서 읽어도 pre-fill 판독이라는 사실은 변하지 않는다.** 잠금은 이 검사에 정확성을 준 적이 없다.

이는 의도적 약화이며, 얻는 것은 브로커 왕복 N회를 잠금 밖으로 빼는 것이다.

### `save_approval`에는 `writer_lock`만 필요하다

exactly-once는 유니크 제약에서 나온다. `live_order_lock`은 주문 제출 결정을 지키는 잠금이지 승인 기록을 지키는 잠금이 아니다. 선점 구간은 `writer_lock` 하나로 충분하다.

폴링 루프 안에서 `reconcile_latest`가 건별로 `live → writer`를 짧게 잡는 것은 계약 위반이 아니다. 금지되는 것은 **잠금을 쥔 채 루프를 도는 것**이다.

동시에 도는 `resume-order-tracking`과 체결 반영이 겹칠 수 있으나, fill watermark 테이블이 중복 적용을 막는다(`test_fill_watermark_table_prevents_double_apply`).

## 가드 — 기본 금지 + 좁은 명시적 허용

순서 가드(`_assert_live_order_lock_order`)가 순서 역전을 구조적으로 불가능하게 만든 것과 같은 방식으로, 보유 시간 상한을 코드로 집행한다.

**금지**: 잠금을 쥔 채 — 승인 대기, 체결 폴링 루프, readonly 계정 refresh.

**허용(명시적)**: `submit_approved_order`의 `check → submit → persist`. 이 브로커 호출은 **의도적으로** 잠금 안에 있어야 한다. 전면 금지는 이 정당한 임계구역까지 막는다.

허용은 암묵적이면 안 된다. `allow_broker_io_under_lock("order submit")` 형태의 명시적 표식을 두어, 예외가 코드에 드러나고 감사 가능하게 한다. 표식 없는 브로커 호출이 잠금 아래에서 일어나면 예외.

**대가**: 테스트가 덮지 못한 경로가 있으면 런타임에 실패한다. 이는 순서 가드에서 이미 받아들인 트레이드오프와 같다 — 실거래에서 조용한 교착보다 시끄러운 실패가 낫다.

## 잠금 신원 — canonical DB 경로에서 파생

현재 잠금 경로는 주어진 DB 경로에서 파생된다. `/real/state.db`와 그 심링크 `/alias.db`는 같은 SQLite 파일을 열지만 잠금은 `/real/state.db.lock`과 `/alias.db.lock`으로 갈라진다. 두 `StateStore`가 서로를 배제하지 못하고, 순서 검사도 상대의 보유를 보지 못한다. 일일 한도 검사가 병행되어 한도 초과 주문으로 이어질 수 있다.

**수정**: `StateStore.__init__`에서 DB 경로를 먼저 canonicalize하고, 모든 잠금 경로와 depth 키를 그 canonical 경로에서 파생한다.

상대/절대 표기 차이는 현재 `_lock_key`의 `resolve()`가 이미 흡수한다. canonical화는 심링크 별칭까지 덮어 원인 지점에서 해결한다.

**프로덕션 현황**: 해당 없음. `/root/maestro-operator/var/symphony_state.db`는 심링크가 아니고, 세 설정 모두 동일한 절대경로를 쓴다. 잠재 결함을 닫는 것이지 현재 사고를 고치는 것이 아니다.

## 배포 프로토콜 — 혼합 버전 창

이 레포는 systemd가 git 워킹트리를 직접 실행한다(`ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro ...`). 따라서 **`git merge` 순간 신규 프로세스가 새 코드로 바뀌고, 이미 떠 있는 장기 프로세스는 옛 코드로 남는다.**

옛 코드는 `writer → live`, 새 코드는 `live → writer`다. 두 버전이 공존하면 정확히 그 순환 대기가 생기고, 새 코드의 `RuntimeError` 가드는 옛 프로세스를 끊지 못한다.

**2026-08-13 배포에도 이 창이 있었다** — 머지 14:50, operator 재시작 14:51:21. 집행 중인 승인이 없어 사고가 없었을 뿐이다.

**배포 순서를 프로토콜로 고정한다:**

1. `systemctl stop maestro-telegram-operator.service`
2. 코드 갱신 (`git merge`)
3. `systemctl start maestro-telegram-operator.service`

타이머 잡은 짧게 살고 즉시 끝나므로, 장기 보유가 가능한 유일한 프로세스인 operator만 닫으면 창이 사라진다.

**추가 방어(저비용)**: 잠금 보유자 기록에 프로토콜 버전 필드를 넣는다. 새 코드가 버전 필드 없는 보유자 기록(= 옛 코드)을 보면 즉시 실패한다. 옛 쪽은 여전히 자기 타임아웃까지 매달리지만, 새 쪽이 10~30초 침묵 대신 원인을 지목하며 죽는다. 진단 가치가 비용보다 크다.

## 남는 복구 창 — 없애지 못한다

프로세스가 **선점 이후·완료 기록 이전에** 죽으면, 승인은 소비됐는데 집행은 미완인 상태가 남는다.

이는 지금도 동일하다 — 8/12가 정확히 그 상태였다. 복구 경로도 이미 있다(`live_order_recovery_required` + `resume-order-tracking` 2분 주기). 이 재설계는 창을 **좁히지만 없애지 못한다.** 없애려면 브로커와의 분산 트랜잭션이 필요하고, 그것은 불가능하다.

창을 닫는 몫은 **2부(당일 재계산·재승인 복구, `recovery_group_id` + 세대 펜싱)**다. `save_system_events_atomic`(3a-2)의 CAS 전제조건이 본질적으로 필요한 곳도 거기다.

## 이 범위에 넣지 않는 것

- **`save_system_events_atomic`을 선점에 도입** — `save_approval`의 유니크 제약이 이미 exactly-once를 만족한다. 만들어 둔 API를 여기서 쓰는 것은 YAGNI 위반이다. 제자리는 2부다.
- **집행 전용 워커 프로세스 분리** — 격리는 낫지만 새 systemd 유닛·큐·장애 처리가 붙는다. 현재 문제를 푸는 데 필요한 수준을 넘는다.
- **백그라운드 잡의 경합 시 graceful skip** — 임계구역이 짧아지면 경합 자체가 사라진다. 증상이 남으면 그때 별도로 다룬다.
- **2부·3부** — 요구사항만 보존.

## 검증

- `.venv/bin/python -m pytest tests/ -q` — 기준선 **1352 passed, 9 skipped**
- `.venv/bin/python -m ruff check src tests --output-format=concise`
- 모든 하중 테스트는 **뮤테이션으로 비공허성을 증명**한다. 잠금 관련 결함은 단일 프로세스 테스트에서 조용히 통과하므로, 실제 다중 프로세스 경합으로 재현하지 않은 테스트는 아무것도 증명하지 못한다.
- 신규 회귀 테스트:
  - 선점 이후 잠금이 실제로 풀렸는지 — 집행 중 다른 프로세스가 `writer_lock`을 획득할 수 있다
  - 잠금 아래 브로커 I/O가 가드에 걸린다 (허용 표식이 있는 제출은 통과한다)
  - 심링크로 별칭된 두 `StateStore`가 상호 배제하고 순서 위반을 검출한다
  - 동시 승인 두 건 중 하나만 집행된다 — 잠금 없이 스키마만으로
  - capacity 파티션이 잠금을 쥐지 않은 채 수행된다 (사전 필터 격하의 회귀 방지)

## 성공 기준

**US 로테이션이 끝까지 집행되고, 그 동안 `resume-order-tracking`이 한 번도 실패하지 않는다.**

순서 수정만으로는 앞의 절반만 달성된다. 뒤의 절반이 이 재설계의 몫이다.
