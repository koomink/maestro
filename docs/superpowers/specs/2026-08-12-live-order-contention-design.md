# 집행 중단 진단 — 락 계측

작성: 2026-08-12
개정: 2026-08-12 4차 (Codex 적대적 리뷰 4회 반영 — 원인을 미확정으로 낮추고 범위를 관측으로 축소)
상태: 확정 (계획 승인됨)

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

## 원인 — **미확정.** 두 가설이 같은 타임라인을 만든다

사고 당시 이벤트:

```
13:47:31Z  신호 런: live_order_status + fill_reconciliation   (정상, 30초 주기)
13:48:01Z  resume-order-tracking: live_order_status 기록
13:48:11Z  신호 런: 다음 주기 쓰기 → writer 대기 시작으로 추정 (writer 한도 10초)
13:48:21Z  신호 런: TimeoutError
13:48:21Z  resume: fill_reconciliation + live_order_tracking_resume 기록
13:48:24Z  ⚠️ 운영자 알림 (3a-1이 탐지)
```

`live_order_tracking_resume`은 `cli.py:2569`가 기록하는 `resume-order-tracking` 전용 이벤트다.

### 가설 A — 락 순서 역전에 의한 교착

두 락을 **반대 순서로** 잡는 경로가 공존한다.

| 경로 | 획득 순서 |
|---|---|
| 승인 집행 `resolve_pending_signal_approval` (`orchestrator.py:304`) → 집행 중 이벤트 기록 | `live` → `writer` |
| 체결 대조 `PartialFillReconciliationService.reconcile_latest` (`live_order_fills.py:35-36`) | **`writer` → `live`** |

`maestro-resume-order-tracking`(2분 주기)은 후자를 호출한다. resume이 `writer`를 쥐고 `live`를 기다리는 동안 신호 런이 `writer`를 기다려 10초 만에 희생됐다는 설명이다.

### 가설 B — 제3의 장기 writer 보유자

`approve_signal`(`orchestrator.py:289`)은 `writer_lock`을 쥔 채 `_execute_live_approval_orders`까지 내려가고, 체결 폴링은 **30초 × 최대 20회 = 최대 10분**이다(`config/execution.py:258-259`). 교착이 없어도 그동안 다른 프로세스의 `writer` 획득은 10초 뒤 실패한다. 제3의 writer가 양쪽을 막고 있다가 13:48:21에 풀렸다면, 신호 런은 만료되고 resume은 경쟁에서 이긴 것으로 같은 타임라인이 나온다.

### 왜 가릴 수 없는가

13:48:01의 `live_order_status`는 `poll_order_status`가 **별도의 writer 임계구역**에서 저장한 뒤 반환된 것이다. 그 뒤에야 `resume()`이 `reconcile_latest`를 호출한다. 따라서 그 기록은 **"resume이 13:48:01부터 20초간 writer를 보유했다"를 뜻하지 않는다.**

근본 이유는 **`writer_lock`과 `live_order_lock`이 `owner` 인자를 받고 즉시 버린다**는 것이다(`store.py:290`, `:348`의 `del owner`). 락 파일에 보유자·PID·획득 시각이 남지 않아, 타임아웃 예외는 누가 잡고 있었는지 말해주지 않는다.

> **개정 이력에서 내가 틀린 것들 (기록)**
> - 1차: "쓰기가 몰려 경합했다" — 혼잡으로 진단했다. 틀렸다.
> - 1차: 대책을 "`resume-order-tracking`이 물러난다"로 잡았다. 호출자 하나만 막고, `live_order_lock`이 전역(`store.py:26`)이라 **무관한 주문의 추적까지 멈춘다.** 폐기했다.
> - 2차: "집행 경로는 live→writer이므로 그것을 정본으로" — `approve_signal`은 **writer를 쥔 채** `submit_approved_order`(`live_order_safety.py:47`)에서 live를 잡는다. 근거가 무너졌다.
> - 2차: `cli.py:486/1259/1659`를 `PartialFillReconciliationService` 호출자로 인용했다. 다른 대조 서비스였다.
> - 2차: 두 락 모두 10초라고 썼다. **`live_order_lock`은 30초**(`store.py:346`)다.
> - 3차: 위 타임라인으로 가설 A를 "확정"이라고 썼다. **과했다** — 위의 이유로 증명이 아니다.

> **1·2차 개정에서 내가 틀린 것들 (기록)**
> - 1차: "쓰기가 몰려 경합했다" — 교착이 아니라 혼잡으로 진단했다. 틀렸다.
> - 1차: 대책을 "`resume-order-tracking`이 물러난다"로 잡았다. 호출자 하나만 막고, `live_order_lock`이 전역(`store.py:26`)이라 **무관한 주문의 추적까지 멈춘다.** 폐기했다.
> - 2차: "집행 경로는 live→writer이므로 그것을 정본으로" — `approve_signal`(`orchestrator.py:289`)은 **writer를 쥔 채** `submit_approved_order`(`live_order_safety.py:47`)에서 live를 잡는다. 근거가 무너졌다.
> - 2차: `cli.py:486/1259/1659`를 `PartialFillReconciliationService` 호출자로 인용했다. 다른 대조 서비스였다.
> - 2차: 두 락 모두 10초라고 썼다. **`live_order_lock`은 30초**(`store.py:346`)다.
> - 2차: "한 프로세스 안은 재진입이라 안전" — 재진입 카운터는 **thread-local**이라 다른 스레드에는 적용되지 않는다.


## 범위 — 관측만 한다

원인이 두 가설 사이에서 갈리고, 각 가설이 요구하는 구조 수정이 서로 다르며 **둘 다 실거래 집행 경로를 건드린다.** 증명 없이 고르면 틀린 쪽을 고칠 위험이 크다. 따라서 이번 범위는 관측뿐이다.

### 락 계측

`writer_lock`(`store.py:283`)·`account_refresh_lock`(`:313`)·`live_order_lock`(`:342`)은 구조가 동일하다. 공통 헬퍼로 뽑고 계측을 넣는다.

- **획득 시**: flock을 얻은 직후 락 파일에 **보유자·PID·획득 시각**을 기록한다. 배타 flock을 쥔 상태이므로 쓰기 경쟁이 없다.
- **해제 시**: 기록을 지운 뒤 unlock한다. 다음 대기자가 낡은 기록을 오해하지 않게 한다.
- **타임아웃 시**: 락 파일을 읽어 **그 시점 보유자**를 `TimeoutError` 메시지에 담는다. 비었거나 깨졌으면 `unknown`으로 두고 예외 타입·동작은 그대로다.
- **`del owner`를 제거한다.** 세 함수 모두 호출자가 문자열 리터럴을 넘기므로 인자 변경은 없다.
- **재진입 경로**(`_lock_depths` > 0)는 파일을 건드리지 않는다. 바깥 획득의 기록이 유지된다.
- 계측은 **system event를 쓰지 않는다.** 이벤트 기록이 writer 락을 요구하므로 락 원시 안에서 호출하면 재귀한다.

진단용으로 현재 보유자를 조회하는 헬퍼를 `StateStore`에 노출한다.

## 이 범위에 넣지 않는 것

- **락 순서 뒤집기·순서 규칙 선언·위반 검출** — 원인이 미확정이다. 게다가 "writer 보유 중 live 요구는 위반"이라고 선언하면서 `approve_signal`의 위반 경로를 이월하는 것은 자기모순이고, 기존 테스트(`tests/test_signal_approval_handoff.py:289-321`)가 armed live `approve_signal`을 실행하므로 CI 엄격 모드는 그것부터 깨뜨린다.
- **`approve_signal`의 장시간 writer 보유 해소** — 고치려면 writer 임계구역을 쪼개야 하고, 그러면 현재 그 락이 보장하는 **승인 단일 소비 원자성**(`approval_consumed` 조회 ~ `mark_signal_package_consumed`)을 CAS로 재설계해야 한다. 관측이 가설 B를 지목하면 그때 별도 스펙으로.
- **2부: 당일 재계산·재승인 복구** — 목표 비중 전략이므로 낡은 수량을 재제출하지 않고 최신 상태로 재계산해 새 승인을 요청한다. **세대 펜싱 필수**: 중단 건별 `recovery_group_id`와 단조 증가 generation을 영속화하고 활성 generation 하나만 원자적으로 claim한다. 현재 주문 멱등 키가 `signal_run_id`·`order_intent_id`에 묶여 있어, 재계산이 새 run을 만들면 두 복구 승인이 다른 키를 얻어 **각각 집행될 수 있다.**
- **3부: 취소 종결 상태 머신** — 스냅샷에서의 부재는 terminal 증거가 아니다(KIS 개별 조회는 미발견 시 `UNKNOWN`). 종결 증거는 명시적 CANCELED/FILLED/REJECTED와 누적 체결 대조로 제한한다. 브로커 취소 API에 idempotency key가 없으므로 같은 로컬 키의 재POST는 안전한 재시도가 아니다. `_is_duplicate_cancel`의 일괄 거부도 함께 재설계한다. `pending_broker_orders` 차단 자체는 유지한다.

## 관측 후 결정

| 관측 결과 | 다음 조치 |
|---|---|
| 타임아웃 시 보유자가 `fill_reconciliation` 계열 | 가설 A — 획득 순서를 통일한다 |
| 보유자가 `approve_signal`/`run_once` 등 장기 보유자 | 가설 B — writer 임계구역 분할 + 승인 단일소비 CAS 재설계 |
| 둘 다 관측 | 순서 통일을 먼저, 임계구역은 그다음 |

## 검증

- **계측 동작**: 락 보유 중 파일에 보유자·PID·획득 시각이 기록되고 해제 후 지워진다. 다른 프로세스가 짧은 타임아웃으로 요구하면 예외 메시지에 보유자와 PID가 담긴다.
- **견고성**: 락 파일이 비었거나 깨졌을 때 `unknown`으로 처리되고 예외 타입·동작이 그대로다. 재진입 획득이 바깥 기록을 덮어쓰지 않는다.
- **회귀**: 전체 스위트(`pytest tests/ -q`, 기준선 **1298 passed / 9 skipped**)와 `ruff check src tests`.
- **최종 성공 기준**: 다음 경합에서 `TimeoutError` 메시지가 보유자를 지목하는 것. 그때까지 원인은 미확정으로 남는다.

## 단독 배포의 안전성

이 범위는 **동작을 바꾸지 않는다** — 락 파일에 기록을 남기고, 타임아웃 메시지를 자세히 만들 뿐이다. 새로 던지는 예외가 없고 획득 순서도 그대로이므로 배포 후 상태가 지금보다 나빠지지 않는다.

## 우선순위

3a 계열보다 앞선다. 원인을 모르는 채로는 2·3부도, 3a-2/3a-3도 같은 락 위에서 같은 위험을 안고 돌기 때문이다.
