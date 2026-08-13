# 잠금 임계구역 재설계 — 폴링을 임계구역 밖으로

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

## 문제 — 임계구역이 체결 폴링을 삼키고 있다

순서를 통일해도 남는 것이 있다. `resolve_pending_signal_approval`은 다음 전 구간에 `live_order_lock`을 건다:

```
live_order_lock {
    패키지 로드
    capacity 파티션        ← 브로커 I/O
    save_approval
    브로커 주문 제출        ← 브로커 I/O
    체결 폴링              ← 최대 20회 × 30초 = 10분   ★
    완료 기록
}
```

★ 표시한 한 줄이 문제의 거의 전부다. 나머지 단계는 모두 초 단위인데, 폴링만 분 단위다. 8/12 관측에서 `live_order_lock`이 104.2초 보유된 것도 폴링 때문이다.

이 폴링 구간 때문에 2분마다 도는 `resume-order-tracking`이 10~30초 타임아웃으로 계속 실패한다.

## 핵심 판단 — 경계는 제출 앞이 아니라 폴링 앞이다

이 재설계의 첫 초안은 잠금을 "선점(save_approval)까지만"으로 좁히려 했다. **그것은 틀렸다.** 두 가지 서로 다른 직렬화를 하나로 뭉뚱그렸기 때문이다:

1. **한 승인 내부의 직렬화** — 선점이 끝나면 불필요하다. 다른 프로세스가 같은 승인을 다시 집행할 수 없다.
2. **서로 다른 승인 사이의 직렬화** — 필요하다. 두 승인이 같은 계정의 매수여력과 일일 한도를 각자 통과한 뒤 순차 제출되면, 한도를 함께 초과하거나 자금이 모자란 부분 집행이 남는다.

`save_approval`의 유니크 제약은 (1)만 준다. (2)는 잠금이 주고 있었다. 잠금을 제출 앞에서 놓으면 (2)를 잃는다.

**따라서 잠금은 제출까지 유지하고, 폴링 직전에 놓는다.**

```
[잠금 없음]        패키지 로드, 형상 검증
[live_order_lock]  capacity 파티션 + save_approval + 이 승인의 전 주문 제출
[잠금 없음]        체결 폴링 — 상태 기록마다 writer 짧게        ★ 여기가 풀린다
[짧은 writer]      완료 기록
```

보유 시간이 **10분 이상에서 브로커 왕복 N회(초 단위)로** 줄어든다. 승인 간 직렬화는 그대로 유지된다. capacity 검사를 건드릴 필요도, 내구적 매수여력 예약을 새로 만들 필요도 없다.

## 잠금 계약

각 잠금이 무엇을 지키는지 명시한다. 지금까지 문서화된 적이 없어 필요할 때마다 범위가 넓어졌다.

### `live_order_lock` — "한 승인의 제출 결정 전체"

지키는 것: capacity 판정 → 승인 선점 → 일일 한도·중복 검사 → 제출 → 기록. 이것이 원자적이지 않으면 서로 다른 승인이 같은 한도와 같은 매수여력을 각자 통과한다.

보유 시간 상한: **브로커 왕복 N회** (N = 이 승인의 주문 수). 체결 폴링은 포함하지 않는다.

`submit_approved_order`(`live_order_safety.py:47`)는 이미 이 잠금을 잡으므로, 승인 단위로 감싸면 재진입으로 흡수된다.

### `writer_lock` — "SQLite 쓰기 트랜잭션 하나"

지키는 것: 개별 상태 전이.

보유 시간 상한: **트랜잭션 1개.**

**변경 없음.** `StateStore` 메서드마다 이미 짧게 잡는다.

### 계약에서 나오는 규칙

> **잠금 보유 시간은 외부 I/O 횟수에 비례할 수 있으나, 외부 시스템의 응답을 기다리는 시간에 비례해서는 안 된다.**
>
> 제출은 왕복이고 폴링은 대기다. 전자는 임계구역에 있어도 되고, 후자는 안 된다.

## 보장의 정확한 이름 — exactly-once가 아니다

이 스펙의 초안은 "exactly-once"라고 썼다. 틀렸다. 정확한 이름은 **주문 건당 at-most-once 제출**이다.

보장되는 것:

- 한 `approval_id`는 한 번만 소비된다 (`save_approval` 유니크 제약)
- 한 주문은 두 번 제출되지 않는다 (`duplicate_key` + 제출 임계구역)

**보장되지 않는 것 — 모호한 제출 창(ambiguous submit):**

`submit_approved_order`는 `_persist_submit_intent` → `submit_limit_order` 순으로 동작한다(`live_order_safety.py:58-60`). 제출 직전의 intent는 내구적으로 남지만, 브로커 호출 도중 프로세스가 죽으면 주문이 나갔는지 알 수 없는 상태가 된다.

그리고 **현재 복구 경로는 이 상태를 보지 못한다.** `LiveOrderTrackingService.resume`은 `list_outstanding_orders`가 돌려주는 것만 폴링하는데, 그 목록은 `broker_order_id`가 있는 기록에서만 만들어진다. intent만 있고 결과가 없는 주문은 복구 대상에 들어오지 않는다.

이 창은 **이 재설계가 만들지도, 넓히지도 않는다.** intent와 제출은 지금도 앞으로도 `submit_approved_order`의 짧은 잠금 안에 함께 있고, 이 스펙은 그 구간을 건드리지 않는다.

복구는 가능하다 — 브로커가 우리 `order_id`를 `clientOrderId`로 되돌려주기 때문이다(8/12 페이로드에서 확인: `"clientOrderId": "ord_42305ecbb595483bb8d24f37dd62350b"`). client order id로 조회해 intent-only 주문을 재조정하는 상태 머신을 만들면 창이 닫힌다.

**그 작업은 이 스펙의 범위가 아니다.** 잠금 구조와 직교하며, 큐에 있는 **3부(취소 terminal 상태 머신)**와 **2부(당일 재승인 복구)**가 다룰 자리다. 여기서는 창의 존재와 위치를 명시하는 것으로 끝낸다 — 이름을 잘못 붙인 채 넘어가지 않는 것이 이 절의 목적이다.

## 가드 — 기본 금지 + 좁은 명시적 허용

순서 가드(`_assert_live_order_lock_order`)가 순서 역전을 구조적으로 불가능하게 만든 것과 같은 방식으로, 보유 시간 상한을 코드로 집행한다.

**금지**: 잠금을 쥔 채 — 승인 대기, **체결 폴링 루프**, readonly 계정 refresh.

**허용(명시적)**: 주문 제출 왕복. 위 계약에 따라 이것은 임계구역 안에 있어야 한다.

허용은 암묵적이면 안 된다. `allow_broker_io_under_lock("order submit")` 형태의 명시적 표식을 두어 예외가 코드에 드러나고 감사 가능하게 한다. 표식 없는 브로커 호출이 잠금 아래에서 일어나면 예외.

**대가**: 테스트가 덮지 못한 경로가 있으면 런타임에 실패한다. 순서 가드에서 이미 받아들인 트레이드오프와 같다 — 실거래에서 조용한 교착보다 시끄러운 실패가 낫다.

## 잠금 신원 — canonical DB 경로에서 파생

현재 잠금 경로는 주어진 DB 경로에서 파생된다. `/real/state.db`와 그 심링크 `/alias.db`는 같은 SQLite 파일을 열지만 잠금은 `/real/state.db.lock`과 `/alias.db.lock`으로 갈라진다. 두 `StateStore`가 서로를 배제하지 못하고 순서 검사도 상대의 보유를 보지 못한다. 위 계약이 지키려는 승인 간 직렬화가 통째로 무너진다.

**수정**: `StateStore.__init__`에서 DB 경로를 먼저 canonicalize하고, 모든 잠금 경로와 depth 키를 그 canonical 경로에서 파생한다.

상대/절대 표기 차이는 현재 `_lock_key`의 `resolve()`가 이미 흡수한다. canonical화는 심링크 별칭까지 원인 지점에서 덮는다.

**프로덕션 현황**: 해당 없음. `/root/maestro-operator/var/symphony_state.db`는 심링크가 아니고 세 설정 모두 동일한 절대경로를 쓴다. 잠재 결함을 닫는 것이지 현재 사고를 고치는 것이 아니다.

## 배포 프로토콜 — 혼합 버전 창

이 레포는 systemd가 git 워킹트리를 직접 실행한다(`ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro ...`). 따라서 **`git merge` 순간 신규 프로세스가 새 코드로 바뀌고, 이미 떠 있는 프로세스는 옛 코드로 남는다.**

옛 코드와 새 코드의 잠금 순서가 다르면 정확히 그 순환 대기가 생기고, 새 코드의 가드는 옛 프로세스를 끊지 못한다.

**operator만 멈추는 것으로는 부족하다.** 8/12 사고의 한쪽 당사자는 operator가 아니라 `resume-order-tracking`이었고, 그 프로세스가 `writer_lock`을 24.2초 보유했다. 2분 주기 타이머가 머지 직전에 시작되면 옛 코드로 계속 돈다.

**배포 순서:**

1. `systemctl stop maestro-resume-order-tracking.timer maestro-telegram-operator.service`
   (그리고 live-order를 건드리는 모든 유닛)
2. 각 유닛이 `inactive`인지 확인한다 — 타이머 정지는 이미 실행 중인 서비스를 죽이지 않는다
3. 잠금 파일에 보유자 기록이 없는지 확인한다 (`StateStore.read_lock_holder`)
4. 코드 갱신 (`git merge`)
5. 역순으로 재시작
6. 실패 시: 이전 커밋으로 `git checkout` 후 같은 순서로 재기동

**2026-08-13 배포에는 이 창이 있었다** — 머지 14:50, operator 재시작 14:51:21, 타이머는 정지시키지 않았다. 집행 중인 승인이 없어 사고가 없었을 뿐이다.

**추가 방어(저비용)**: 잠금 보유자 기록에 프로토콜 버전 필드를 넣는다. 새 코드가 버전 필드 없는 보유자 기록(= 옛 코드)을 보면 즉시 실패한다. 옛 쪽은 자기 타임아웃까지 매달리지만, 새 쪽이 침묵 대신 원인을 지목하며 죽는다. 3단계 확인을 사람이 건너뛰었을 때의 그물이다.

## 이 범위에 넣지 않는 것

- **`save_system_events_atomic`을 선점에 도입** — `save_approval`의 유니크 제약이 승인 단일 소비를 이미 만족한다. 만들어 둔 API를 여기서 쓰는 것은 YAGNI 위반이다. 제자리는 2부다.
- **모호한 제출 창의 복구 상태 머신** — 위에 명시했듯 잠금 구조와 직교한다. 2부·3부의 몫이다.
- **내구적 매수여력 예약** — 폴링 앞에 경계를 그으면 승인 간 직렬화가 유지되므로 불필요하다.
- **집행 전용 워커 프로세스 분리** — 격리는 낫지만 새 systemd 유닛·큐·장애 처리가 붙는다. 현재 문제에 비해 과하다.

## 검증

- `.venv/bin/python -m pytest tests/ -q` — 기준선 **1352 passed, 9 skipped**
- `.venv/bin/python -m ruff check src tests --output-format=concise`
- 모든 하중 테스트는 **뮤테이션으로 비공허성을 증명**한다. 잠금 결함은 단일 프로세스 테스트에서 조용히 통과하므로, 실제 다중 프로세스 경합으로 재현하지 않은 테스트는 아무것도 증명하지 못한다.
- 신규 회귀 테스트:
  - **폴링 중 잠금이 풀려 있다** — 집행이 폴링 단계일 때 다른 프로세스가 `live_order_lock`과 `writer_lock`을 모두 획득할 수 있다
  - **제출 중에는 잠금이 유지된다** — 제출 단계에서는 다른 프로세스가 `live_order_lock`을 얻지 못한다 (승인 간 직렬화 회귀 방지)
  - 잠금 아래 폴링 루프가 가드에 걸린다 (허용 표식이 있는 제출은 통과한다)
  - 심링크로 별칭된 두 `StateStore`가 상호 배제하고 순서 위반을 검출한다
  - 동시 승인 두 건 중 하나만 소비된다

## 성공 기준

**US 로테이션이 끝까지 집행되고, 그 동안 `resume-order-tracking`이 한 번도 실패하지 않는다.**

순서 수정(`7953e61`)만으로는 앞의 절반만 달성된다. 뒤의 절반이 이 재설계의 몫이다.
