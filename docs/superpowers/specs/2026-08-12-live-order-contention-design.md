# 집행 중단 방지 — 락 순서 역전 제거 (1부)

작성: 2026-08-12
개정: 2026-08-12 3차 (Codex 적대적 리뷰 3회 반영 — 원인을 증거로 확정하고 범위를 축소)
상태: 개정 완료, 계획 작성 대기

## 배경 — 2026-08-11 리밸런싱 절반 중단

2026-08-11 22:44 KST(13:44Z), 운영자가 US 리밸런싱 5건을 승인했다. 집행은 절반에서 멈췄다.

| 주문 | 결과 |
|---|---|
| TIP 23주 매도 | **체결** |
| SSO 20주 매도 | 제출됨, **미체결(OPEN)로 잔존** |
| PDBC 366주 매수 | **제출 안 됨** |
| SPY 6주 매수 | **제출 안 됨** |
| BIL 15주 매수 | **제출 안 됨** |

실패: `TimeoutError: State writer lock is busy`. USD **$11,576 미배치**, 미체결 SSO가 다음 런의 `pending_broker_orders` 게이트를 막는 상태.

## 원인 — 락 순서 역전에 의한 교착 (이벤트 타임라인으로 확정)

두 락을 **반대 순서로** 잡는 경로가 공존한다.

| 경로 | 획득 순서 |
|---|---|
| 승인 집행 `resolve_pending_signal_approval` (`orchestrator.py:304`) → 집행 중 이벤트 기록 | `live` → `writer` |
| 체결 대조 `PartialFillReconciliationService.reconcile_latest` (`live_order_fills.py:35-36`) | **`writer` → `live`** |

`maestro-resume-order-tracking`(2분 주기)은 후자를 호출한다. 사고 당시 이벤트가 이 교착을 그대로 보여준다:

```
13:47:31Z  신호 런: live_order_status + fill_reconciliation   (정상, 30초 주기)
13:48:01Z  resume-order-tracking: live_order_status 기록
           → 이후 writer 보유, live 대기 (live 한도 30초)
13:48:11Z  신호 런: 다음 주기 쓰기 → writer 대기 시작 (writer 한도 10초)
13:48:21Z  신호 런: TimeoutError (10초 만료, 먼저 희생)
13:48:21Z  resume: fill_reconciliation + live_order_tracking_resume 기록 (락 획득)
13:48:24Z  ⚠️ 운영자 알림 (3a-1이 탐지)
```

`live_order_tracking_resume`은 `cli.py:2569`가 기록하는 `resume-order-tracking` 전용 이벤트다. 신호 런의 30초 주기 쓰기가 13:47:31 이후 끊긴 것, 두 사건이 13:48:21에 같이 일어난 것, 락 한도가 각각 10초·30초인 것이 모두 맞물린다.

> **1·2차 개정에서 내가 틀린 것들 (기록)**
> - 1차: "쓰기가 몰려 경합했다" — 교착이 아니라 혼잡으로 진단했다. 틀렸다.
> - 1차: 대책을 "`resume-order-tracking`이 물러난다"로 잡았다. 호출자 하나만 막고, `live_order_lock`이 전역(`store.py:26`)이라 **무관한 주문의 추적까지 멈춘다.** 폐기했다.
> - 2차: "집행 경로는 live→writer이므로 그것을 정본으로" — `approve_signal`(`orchestrator.py:289`)은 **writer를 쥔 채** `submit_approved_order`(`live_order_safety.py:47`)에서 live를 잡는다. 근거가 무너졌다.
> - 2차: `cli.py:486/1259/1659`를 `PartialFillReconciliationService` 호출자로 인용했다. 다른 대조 서비스였다.
> - 2차: 두 락 모두 10초라고 썼다. **`live_order_lock`은 30초**(`store.py:346`)다.
> - 2차: "한 프로세스 안은 재진입이라 안전" — 재진입 카운터는 **thread-local**이라 다른 스레드에는 적용되지 않는다.

## 범위 — 확정된 원인만 제거한다

### 1. 역순 획득 제거

`PartialFillReconciliationService.reconcile_latest`(`live_order_fills.py:35-36`)의 획득 순서를 **`live_order_lock` → `writer_lock`**으로 바꾼다. 정본 순서를 이 방향으로 정하는 근거는 `live_order_lock`이 **더 바깥에서 더 오래 잡히는 자원**이기 때문이다 — 주문 제출·체결 대기는 수 분이 걸리고 그동안 짧은 쓰기가 여러 번 필요하다.

호출 그래프를 훑어 **전이적으로도** `writer`를 쥔 채 `live`를 요구하는 경로가 남지 않는지 확인한다.

### 2. 회귀 방지 — 순서 위반 검출

`writer_lock`을 보유한 상태에서 `live_order_lock`을 요구하면 위반이다. 이를 검출하되, **프로덕션에서 새 예외 경로를 만들지 않는다**:

- **테스트·CI**: 위반 시 즉시 실패한다(엄격 모드). 회귀를 여기서 잡는다.
- **프로덕션**: 위반을 **이벤트로 기록**하고 진행한다. 예외를 던지지 않는다.

프로덕션에서 예외를 던지면, 승인이 소비된 뒤 브로커 호출 전에 터지는 경로가 생겨 **지금보다 나쁜 상태**(승인만 소비되고 집행도 복구도 없음)를 만들 수 있다. 1부 단독 배포 시점에는 당일 복구 흐름(2부)이 없으므로 그 출구가 없다.

### 3. 락 계측

`writer_lock`·`live_order_lock`이 `owner`를 버리지 않게 한다(`store.py:290`의 `del owner`). 락 파일에 **보유자·PID·획득 시각**을 기록하고, 타임아웃 예외와 이벤트에 **그 시점의 보유자**를 담는다.

이번에는 이벤트 타임라인을 역산해 보유자를 특정할 수 있었지만, 그건 resume이 마침 자기 이벤트를 남겼기 때문이다. 다음 사고가 그런 흔적을 남긴다는 보장은 없다.

## 이 범위에 넣지 않는 것

- **`approve_signal`의 장시간 writer 보유** — `approve_signal`(`orchestrator.py:289`)은 `writer_lock`을 쥔 채 `_execute_live_approval_orders`까지 내려가고, 체결 폴링은 **30초 × 최대 20회 = 최대 10분**(`config/execution.py:258-259`)이다. 즉 교착이 없어도 그동안 다른 프로세스의 `writer` 획득은 10초 뒤 실패한다. **실재하는 별개 위험**이지만, 이를 고치려면 writer 임계구역을 쪼개야 하고 그러면 현재 그 락이 보장하는 **승인 단일 소비 원자성**(`approval_consumed` 조회 ~ `mark_signal_package_consumed`)을 CAS로 재설계해야 한다. 이번 사고의 원인이 아니고 범위가 다르므로 분리한다.
- **2부: 당일 재계산·재승인 복구** — 목표 비중 전략이므로 낡은 수량을 재제출하지 않고 최신 상태로 재계산해 새 승인을 요청한다. **세대 펜싱 필수**: 중단 건별 `recovery_group_id`와 단조 증가 generation을 영속화하고 활성 generation 하나만 원자적으로 claim한다. 현재 주문 멱등 키가 `signal_run_id`·`order_intent_id`에 묶여 있어, 재계산이 새 run을 만들면 두 복구 승인이 다른 키를 얻어 **각각 집행될 수 있다.**
- **3부: 취소 종결 상태 머신** — 스냅샷에서의 부재는 terminal 증거가 아니다(KIS 개별 조회는 미발견 시 `UNKNOWN`). 종결 증거는 명시적 CANCELED/FILLED/REJECTED와 누적 체결 대조로 제한한다. 브로커 취소 API에 idempotency key가 없으므로 같은 로컬 키의 재POST는 안전한 재시도가 아니다. `_is_duplicate_cancel`의 일괄 거부도 함께 재설계한다. `pending_broker_orders` 차단 자체는 유지한다.

## 검증

- **교착 부재**: **두 프로세스**가 각각 집행(`live` 보유)과 체결 대조를 동시에 시도해도 교착·타임아웃이 없음을 확인한다. 재진입이 thread-local이므로 **다중 스레드** 경우도 별도로 확인한다.
- **위반 검출**: 고의로 `writer` 보유 중 `live`를 요구하면 테스트 모드에서 실패하고, 프로덕션 모드에서는 이벤트만 남고 예외가 나지 않음을 확인한다.
- **계측**: 타임아웃 시 예외·이벤트에 보유자와 대기 시간이 담기는지 확인한다.
- **회귀**: 전체 스위트(`pytest tests/ -q`)와 `ruff check src tests`.

## 1부 단독 배포의 안전성

이 범위는 **새 실패 경로를 만들지 않는다** — 획득 순서를 바꾸고, 관측을 늘리고, 위반을 기록할 뿐이다. 프로덕션에서 새로 던지는 예외가 없으므로 배포 후 상태가 지금보다 나빠지지 않는다. 2·3부가 없어도 운영자는 지금과 같은 수단(체결 대기, 증권사 앱 취소, `/clear_halt`)을 그대로 쓴다.

## 우선순위

1부가 3a 계열보다 앞선다. 확정된 원인이고, 2·3부와 이후 모든 집행 경로가 같은 락 위에서 돌기 때문이다.
