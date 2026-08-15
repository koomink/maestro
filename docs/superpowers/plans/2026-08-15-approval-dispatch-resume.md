# 승인 dispatch idempotent resume (단계 3a-3, 범위 A)

> **상태: 구현 완료 (2026-08-15).** Task 0~3 전부
> `feat/approval-dispatch-resume`에 반영됐다 (`19228c0`..`441329b`).
> 계획과 달라진 점은 아래 「구현에서 달라진 점」에 적었다.
>
> 테스트: **1533 passed, 9 skipped**. 별도로 `test_signal_approval_handoff.py`의
> CLI 테스트 5건이 실패하는데, **이 작업과 무관한 환경 문제**다 —
> `/tmp/maestro-symphony-signal.lock`이 root 시절(2026-07-21)에 `root:root`로
> 만들어져 남아 있고, CLI 기본 `--lock-path`(`cli.py:318`)가 그 경로라
> symphony 유저가 열지 못한다. 깨끗한 트리에서도 동일하게 실패함을 확인했다.
> **운영은 영향 없다**: `/etc/maestro/maestro.env`가
> `MAESTRO_SIGNAL_LOCK_PATH`를 설정하고, 08-14 22:40 US 사이클이 정상
> 완료됐다. 해소하려면 root가 그 파일을 지워야 한다(/tmp sticky bit).

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md`
(「카드 체계 A」, 데이터 흐름 6항 "승인 dispatch의 idempotent resume")
**선행:** 3a-1 (`docs/superpowers/plans/2026-08-10-approval-two-phase-persistence.md`),
3a-2 (`save_system_events_atomic`)

## Context

단계 2에서 승인 카드는 lifecycle이 소유하게 됐지만, **그 카드를 낳는 dispatch
자체가 재개 불가능하다.** 지금 코드에서 확인한 사실:

- `orchestrator.py:982`가 그룹 루프 **전에** `mark_signal_package_consumed`를
  호출한다. 루프(990~1049) 중간에 프로세스가 죽으면 일부 그룹만 승인 카드를
  받은 상태로 남는다.
- 재호출은 `orchestrator.py:894`의 `approval_consumed` 검사에 걸려
  `"Signal package already consumed"`로 거부된다. **그 signal run은 영구히
  멈춘다** — 나머지 그룹의 승인은 영원히 도달하지 않는다.
- 루프 끝의 `signal_approval_pending`(`orchestrator.py:1051`)은 **읽는 코드가
  하나도 없다** (`grep`으로 확인: src/tests 통틀어 이 한 곳뿐). 완료 표지가
  기록만 되고 아무도 소비하지 않으므로, 위 상태는 관측조차 되지 않는다.
- `ApprovalManager.create_request`(`approval/manager.py:96-101`)는 매번
  `new_approval_id()`와 `utc_now()` 기준 `expires_at`을 발급한다. 재개를
  허용해도 같은 주문 그룹에 **새 approval_id와 연장된 만료시각**이 생긴다.
- envelope의 `duplicate_key`는 `telegram-approval-pending:{approval_id}`
  (`orchestrator.py:1018`)인데, approval_id가 무작위라 **절대 충돌하지 않는다** —
  멱등 키로서 아무 일도 하지 않는다.

목표: **dispatch를 중단 지점 어디에서든 재개 가능하게 만든다.** 승인 결정
재개 경로(`handlers.py:1755-1830`)는 3a-1이 만든 fail-closed 동작
(`approval_exists`면 자동 재개하지 않고 ⚠️ 알림)을 **그대로 유지한다.**

> **범위에서 뺀 것 (B): 자동 재개 게이트 완화.** 원래 이 계획은 재개 조건을
> "approvals 행 없음"에서 "해당 승인의 어떤 주문에도 `live_order_submit_intent`
> 없음"으로 넓히려 했다. 근거 자체는 성립한다 —
> `live_order_safety.py:253-277`이 브로커 호출 직전에 intent를 남기고,
> `save_approval`(orchestrator.py:339)과
> `_execute_live_approval_orders`(orchestrator.py:348) 사이에는 audit 로그밖에
> 없다. **그러나 그 증거는 신규 데이터에 대해서만 완전하다.** intent 기록
> 도입 이전에 만들어진 승인은 실제로 브로커에 나갔어도 intent 행이 없을 수
> 있고, 그런 승인을 "미제출"로 오판하면 배포 직후 sweep이 **중복 실주문**을
> 낸다. 이를 안전하게 켜려면 intent 완전성이 보장되는 시점 경계를 저장·검사하고
> 경계 이전 승인을 격리해야 하는데, 그건 3a-5의 업그레이드 backfill이 다루는
> 문제다. B는 "운영자 알림을 줄이는" 개선이고 A가 고치는 영구 정지 결함과 달리
> 급하지 않으므로, backfill 이후로 미룬다.

## Global Constraints

- **테스트 기준선**: `1491 passed, 9 skipped` (2026-08-14 컷오버 시점 기록).
  각 태스크는 이 수를 줄이지 않는다.
- 린트: `.venv/bin/python -m ruff check src tests --output-format=concise` → `All checks passed!`
- **모든 하중 테스트는 뮤테이션으로 비공허성을 증명한다.** 구현을 되돌려
  테스트가 실패하는 것을 확인하고 복원한다.
- **3a는 roll-forward-only.** 이 배포가 남기는 새 이벤트를 구버전은 읽지
  않는다. 롤백은 quiesce 장벽 + preflight 아래에서만.
- **`dispatch_group_id`는 해시가 아니라 전체 정규화 문자열이다** (스펙 개정
  8차). 직렬화는 funding_scope와 같은 규약: canonical JSON,
  `json.dumps(..., ensure_ascii=False, separators=(",",":"))`, **유니코드
  정규화 금지**. 단 funding_scope와 달리 이 배열은 **동질 `list[str]`**이며
  nullable 필드를 담지 않는다 (Task 0) — 이종 타입 정렬 규약이 필요 없다.
- **재개 경로에 "저장본과 재계산본이 다르면 실패"를 만들지 않는다.** 이미
  기록된 것이 권위이고, 재계산은 그것을 찾기 위한 수단일 뿐이다. 문구·설정이
  배포 사이에 바뀌었다는 이유로 승인이 멈추면 안 된다.
- **전송은 at-least-once다.** 중복 카드는 수용하고, 유실 카드는 클릭 시
  self-heal에 맡긴다 (스펙 6항). 단계 2가 만든 `(card_key, chat_id)` intent
  기록과 audience 테이블이 이미 이 계약을 구현하고 있으므로 **재사용한다.**
- **기존 알림 경로를 제거하지 않는다.** 제거는 단계 5.
- 커밋 trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## File Structure

| 파일 | 책임 |
|---|---|
| `src/maestro/orchestration/dispatch_group.py` (신규) | `dispatch_group_id` 직렬화 — 순수 함수, 테스트 가능 |
| `src/maestro/orchestration/orchestrator.py` | 결정적 그룹 분할, envelope get-or-create, consumed 재해석, 재개 진입 |
| `src/maestro/state/store.py` | `insert_or_load_system_event`, duplicate_key로 payload 조회, 미완 dispatch 조회 |
| `src/maestro/integrations/telegram/handlers.py` | 미완 dispatch 재개 sweep |

승인 **결정** 재개 경로(`handlers.py:1755-1830`)와 `save_approval`,
`live_order_safety.py`는 **건드리지 않는다** (범위에서 뺀 B).

---

### Task 0: 그룹 분할을 결정적으로 만들고 `dispatch_group_id`를 정의한다

`_approval_order_groups`(`orchestrator.py:1958-1975`)는 dict 삽입 순서로
그룹을 반환한다. 입력 주문 순서가 같으면 결과도 같지만 그 사실이 어디에도
고정돼 있지 않다 — 재개가 이 순서에 의존하게 되므로 **명시적으로 정렬한다.**

- `dispatch_group.py`에 `dispatch_group_id(signal_run_id, source_strategy_ids) -> str`
  추가. 구성: `"dispatch-group:" + signal_run_id + ":" + canonical JSON
  [정렬된 strategy id 배열]`.
- **`account_ids`는 식별자에 넣지 않는다.** 그룹 키는 strategy id 튜플
  단독이므로(`orchestrator.py:1971`) `(signal_run_id, strategy_ids)`가 이미
  그룹을 유일하게 결정하고, account_ids는 그 그룹에서 파생되는 속성일 뿐이다.
  넣으면 식별력은 늘지 않고 실패 모드만 생긴다 — envelope의 account_ids는
  `sorted({... if order.account_id})`(`orchestrator.py:1012-1014`)로 None을
  걸러 만들어지므로, 식별자 쪽에서 null을 "타입 보존"으로 살리면 같은 그룹이
  두 문자열로 갈린다. **대신 재사용 시 검증한다** (Task 1).
- **배열은 동질 `list[str]`이며, 정렬은 평범한 문자열 정렬이다.**
  `_approval_order_groups`가 이미 `str(strategy_id)`로 강제하고 falsy 값을
  걸러내므로 None·정수가 섞일 수 없다. 이 전제를 코드로 고정한다: 원소가
  `str`이 아니거나 빈 문자열이면 **조용히 coerce하지 말고 raise**한다.
  이렇게 하면 이종 타입 total-order 규약을 따로 정의할 필요가 없고,
  전제가 깨지는 날에는 잘못된 id가 조용히 생기는 대신 배포가 멈춘다.
- `_approval_order_groups`가 키 튜플 기준 정렬된 리스트를 반환하게 한다.
- 테스트 (`tests/test_dispatch_group_id.py` 신규): 구분자(`:`)를 포함한
  식별자, NFC 등가지만 코드포인트가 다른 문자열 — 두 쌍이 서로 다른 id로
  직렬화된다. 주문 순서를 뒤집어도 같은 id가 나온다. `None`·정수·빈 문자열이
  섞인 입력은 `ValueError`를 낸다.
- 뮤테이션: 정렬을 제거하면 순서 뒤집기 테스트가 FAIL. 타입 검사를 제거하면
  혼합 입력 테스트가 FAIL.

### Task 1: envelope을 `dispatch_group_id`로 get-or-create 한다

**`save_system_events_atomic`을 그대로 쓰면 안 된다.** 단일 이벤트 배치에서
키가 이미 존재하면 이 API는 저장된 payload와 지금 제출한 payload를 대조하고
(`store.py:1178-1184`), 다르면 `ValueError`를 던진다. envelope에는
`approval_id`·`created_at`·`expires_at`·`message`가 들어 있으므로 재계산본은
저장본과 **거의 확실히 다르다** — 즉 재개할 때마다 예외가 나고, 이 계획이
없애려던 "signal run 영구 정지"가 그대로 재현된다. 렌더러 문구나
`reminder_seconds`가 배포 사이에 바뀌기만 해도 같은 일이 난다.

따라서 규약은 **"조회해서 있으면 아예 쓰지 않는다"**이다.

- `store.py`에 `load_system_event_payload_by_duplicate_key(key) -> dict | None`
  추가. 기존 `duplicate_key_exists`(store.py:2249)와 같은 인덱스
  (`idx_system_events_duplicate_key`)를 쓴다.
- `_dispatch_signal_approval_locked`의 그룹 루프에서, `create_request` 호출
  **전에** `dispatch-group:<id>` 키로 기존 envelope을 조회한다.
  - **있으면**: `PendingApprovalEnvelope.model_validate`로 복원해
    approval_id·created_at·**expires_at**·orders·message를 그대로 재사용하고,
    `create_request`를 부르지 않으며 **이벤트를 다시 쓰지 않는다.**
    만료시각이 재개마다 연장되면 승인 마감이 무의미해진다.
  - **없으면**: 지금처럼 생성하고 envelope의 `duplicate_key`를
    `telegram-approval-pending:{approval_id}` → `dispatch-group:{id}`로 바꾼다.
- **재사용 시 identity 검증** (스펙 「scope 불일치 방어」와 같은 취지):
  복원한 envelope의 `signal_run_id`와 `source_strategy_ids`가 지금 재계산한
  그룹과 일치하는지 확인한다. 다르면 재사용하지 않고 **크게 실패시킨다** —
  키 오염 상태에서 남의 승인 카드를 이어받는 것보다 멈추는 편이 안전하다.
  `account_ids`·`orders`가 다르면 같은 그룹의 주문 구성이 재계산 사이에
  바뀐 것이므로 역시 거부하고 운영자에게 넘긴다.
- **경쟁 삽입은 조회-후-쓰기 사이에서만 가능하고, 그때도 진행할 수 있어야
  한다.** `writer_lock`(store.py:649-663)이 프로세스 간 flock이라 같은 signal
  run의 두 dispatch는 대체로 직렬화되지만, 3a-2의 docstring이 명시하듯 이건
  **advisory lock일 뿐**이고 복구 스크립트·다른 버전 프로세스·`sqlite3` CLI는
  존중할 이유가 없다. 그래서 저장을 `store.py`의 새 API
  `insert_or_load_system_event(run_id, event_type, payload, duplicate_key) -> tuple[dict, bool]`
  로 한다: 한 트랜잭션(`BEGIN IMMEDIATE`) 안에서 INSERT를 시도하고,
  `IntegrityError`가 나면 **같은 트랜잭션에서 저장된 payload를 다시 읽어
  반환한다.** 반환값은 `(payload, created)`이고, 호출자는 `created=False`면
  위의 identity 검증을 거쳐 그 envelope으로 계속 진행한다. 예외로 dispatch를
  끝내지 않는다.
- 카드 전송(`CardLifecycleManager.deliver`, `orchestrator.py:1033`)은 그대로
  둔다 — 재개 시 이미 보낸 채팅은 단계 2의 intent/audience 기록이 걸러내고,
  전송 불명(unknown) 복사본은 재전송하지 않는다.
- 테스트 (`tests/test_approval_dispatch_resume.py` 신규):
  - 같은 그룹으로 두 번 dispatch해도 approval_id·**expires_at**이 동일하고
    `telegram_approval_pending` 이벤트가 한 건이다.
  - 저장된 envelope의 `message`를 손으로 바꾼 뒤 재개해도 **예외 없이**
    저장본으로 진행한다 (렌더러 문구 변경 시나리오).
  - 실제 동시 실행(스레드 2개, 같은 `dispatch_group_id`)에서 approval_id가
    **정확히 하나**만 나오고 양쪽 모두 예외 없이 그 id를 돌려받는다.
  - identity가 어긋난 envelope(같은 키, 다른 `source_strategy_ids`)은
    재사용되지 않고 거부된다.
- 뮤테이션: get-or-create 조회를 지우면 두 번째 dispatch가 다른 approval_id를
  내고 첫 테스트가 FAIL. `IntegrityError` 재조회 분기를 예외 재발생으로
  바꾸면 동시 실행 테스트가 FAIL. identity 검증을 지우면 마지막 테스트가 FAIL.

### Task 2: `consumed`를 배타 시작 표지로 재해석하고 재개 경로를 연다

- `store.py`에 `list_incomplete_signal_dispatches(limit)` 추가:
  `signal_package_consumed`가 있으나 같은 `signal_run_id`에
  `signal_approval_pending` / `signal_approval_completed`가 없는 signal run.
  둘 다 생성 컬럼 `signal_run_id`가 있고 인덱스
  `idx_system_events_type_signal_run`이 이미 있으므로 스캔이 아니다.
- `_dispatch_signal_approval_locked`의 894행 검사를 둘로 나눈다:
  - consumed **이고** 완료 이벤트 있음 → 지금처럼 `ValueError` (진짜 재소비).
  - consumed **이고** 완료 이벤트 없음 → **재개로 진입**한다. 검증
    (`_validate_signal_broker_baseline`, readonly preflight,
    `_validate_signal_approval_gates`)은 **그대로 다시 실행한다** — 재개
    시점의 시장·계좌 상태가 승인 조건을 여전히 만족하는지가 관심사이고,
    건너뛰면 낡은 스냅샷 위에서 카드를 새로 보내게 된다.
  - 검증이 실패하면 예외를 그대로 던지되, 아래 sweep이 **시도 예산**으로
    무한 재시도를 막고 ⚠️ 알림으로 넘긴다.
- `handlers.py`에 `_resume_incomplete_dispatches()` sweep 추가.
  `_resume_unresolved_approvals`(handlers.py:1720) 옆에 놓고 같은 poll에서
  호출한다. 구조는 기존 재개 경로를 그대로 따른다:
  - 시도 예산과 claim은 `_claim_resume`/`_record_resume_finished`
    (handlers.py:1924, 1950)와 같은 모양으로,
    `telegram-dispatch-resume:{signal_run_id}:a{n}` 키를 쓴다.
  - 예산 소진 시 `_notify_approval_needs_attention`과 같은 방식으로
    운영자에게 넘긴다 (signal_run_id 기준 1회).
  - handlers는 이미 `approval_orchestrator.dispatch_signal_approval`을
    호출한다(handlers.py:3084) — 새 의존성은 없다.
- 테스트: 그룹 루프 중간(첫 그룹 envelope 저장 후 / 첫 카드 전송 후 /
  두 번째 그룹 직전)에서 중단시킨 뒤 sweep을 돌리면, 기존 approval은 그대로
  재사용되고 없던 그룹만 새로 생기며, 끝난 뒤에야
  `signal_approval_pending`이 기록된다.
- 뮤테이션: 894행 분기를 원래대로 되돌리면 재개 테스트가
  "already consumed"로 FAIL.

### Task 3: 중단 주입 종단 테스트

기존 crash-boundary 테스트 패턴(3a-1의 Task 6, `tests/test_telegram_approval_resume.py`)을
따라 한 파일로 묶는다.

- consumed 직후 / 각 그룹 envelope 저장 전후 / 각 채팅 전송 전후 —
  각 지점에서 중단 후 sweep 재개.
- 검증: approval_id·envelope·**최초 만료시각**이 재사용되고, 일부 그룹만
  저장된 상태에서도 나머지만 생성되며, message_id가 알려진 중복 복사본은
  무력화 edit로 정리되고, 승인 callback은 어느 카드에서 눌러도 한 번만
  처리되고, 전부 끝난 뒤에야 dispatch 완료가 기록된다.
- **중단과 배포 변경이 겹친 경우**: 중단 후 재개 사이에 카탈로그 문구와
  `reminder_seconds`를 바꿔도 재개가 성공하고 저장된 envelope으로 진행한다.
- 재개가 검증 실패로 반복될 때 예산이 소진되고 ⚠️ 알림이 정확히 1회 나간다.
- **승인 결정 재개 경로가 바뀌지 않았음을 고정한다**: `approvals` 행이 있는
  승인은 여전히 자동 재개되지 않고 `partial=True` 알림으로 간다. 기존
  `tests/test_telegram_approval_resume.py`가 이미 이것을 검증하므로 **그
  테스트가 수정 없이 통과해야 한다** — 깨지면 범위를 벗어났다는 신호다.

---

## 검증

```bash
cd /home/symphony/maestro
.venv/bin/python -m pytest -q                       # 1491 passed 이상, 9 skipped
.venv/bin/python -m ruff check src tests --output-format=concise
```

태스크별로는 다음 파일에 집중한다:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dispatch_group_id.py \
  tests/test_approval_dispatch_resume.py \
  tests/test_telegram_approval_resume.py \
  tests/test_telegram_card_lifecycle.py
```

운영 확인 (컷오버 후 symphony 런타임):

1. 스테이징 DB 사본에서 `list_incomplete_signal_dispatches`를 돌려 **현재
   미완 dispatch가 0건**임을 먼저 확인한다. 0건이 아니면 배포 전에 그 건을
   먼저 다룬다 — 새 sweep이 켜지는 순간 자동 재개 대상이 되기 때문이다.
2. 배포 후 첫 KR·US 사이클에서 승인 카드가 그룹당 한 장씩만 도착하는지,
   `signal_approval_pending`이 사이클마다 기록되는지 확인한다.
3. `telegram_ui` 헬스체크가 degraded로 떨어지지 않는지 확인한다.

## 명시적으로 하지 않는 것

- **자동 재개 게이트 완화 (B)** — 위 Context의 인용 블록 참조. intent 완전성
  경계를 저장·검사할 수 있게 된 뒤(3a-5 backfill) 다시 다룬다. 그때
  `save_approval` 멱등화와 "저장된 결정이 유일한 실행 입력" 규약도 함께
  간다 — 게이트를 넓히는 순간에만 필요한 것들이라 지금 넣으면 쓰이지 않는
  코드가 된다.
- **재개 backoff·스냅샷 재채택 (이월 2번)** 과 **G1~G3 위생 항목 (이월 3·4번:
  아웃박스 상한, 도달 불가 채팅 이벤트 증식, dedup `IntegrityError`,
  `str(None)` 키)** — 이번 범위 밖. 3a-3 후속 웨이브로 남긴다.
- **funding/budget workflow head·CAS** — 3a-4.
- **업그레이드 backfill·전체 롤백 preflight CLI** — 3a-5. 다만 이 계획이
  `signal_approval_pending`을 실제 완료 표지로 만들므로,
  "consumed-without-dispatch-completion"이 3a-5 preflight에서 **조회 가능한
  상태가 된다** (지금은 아무도 읽지 않아 검출 자체가 불가능하다).
- **구 알림 경로 제거** — 단계 5.

---

## 구현에서 달라진 점

1. **테스트 파일 위치.** 계획은 `tests/test_approval_dispatch_resume.py` 신규를
   적었지만, dispatch 테스트에 필요한 픽스처(KIS 스냅샷 목, live signal config,
   FakeTelegramClient)가 전부 `tests/test_signal_approval_handoff.py`에 있고 이
   리포에는 테스트 모듈 간 import 관례가 없다. 기존 dispatch 테스트 바로 옆에
   놓는 편이 픽스처를 복제하는 것보다 낫다고 판단했다. 새 파일은 순수 단위
   테스트 쪽에만 만들었다: `test_dispatch_group_id.py`,
   `test_state_store_insert_or_load.py`, `test_state_store_incomplete_dispatch.py`,
   `test_telegram_dispatch_resume.py`.

2. **`_run_dispatch` 시임을 추가했다.** 계획에 없던 것이다. sweep이 orchestrator를
   직접 만들면 sweep 테스트가 실주문 설정 전체를 세워야 한다. 바로 옆
   `_run_resolution`이 이미 같은 목적의 시임이라 그 모양을 따랐다.

3. **`_notify_operator_chats`에 `subject_field`를 추가했다.** dispatch 알림은
   승인이 아니라 signal run에 대한 것인데, 기존 함수는 payload에 무조건
   `approval_id`를 쓴다. `approval_id`는 카드 sweep이 조회하는 생성 컬럼이라
   (`store.py:213-216`) signal_run_id를 그 자리에 넣으면 조회를 오염시킨다.

4. **중단 주입 방법.** 계획은 "각 채팅 전송 전후" 중단을 적었지만, 전송 예외는
   orchestrator에 도달하지 않는다 — lifecycle이 단계 2 설계대로 delivery-unknown
   으로 분류하고 진행한다(그리고 그게 옳다). 그래서 그룹 2의 카드를 만드는
   지점에서 중단시켰다. 전송 경계 자체는 단계 2 테스트가 이미 덮는다.

5. **`mark_signal_package_consumed` 멱등화가 필요했다.** 계획에 없었다. 재개가
   같은 메서드로 재진입하므로 가드를 넣지 않으면 consumed 행이 매 재개마다
   쌓인다.

## 배포 전 확인 결과 (2026-08-15)

운영 DB(`/home/symphony/maestro-operator/var/symphony_state.db`, 읽기 전용 조회)
기준 **미완 dispatch 0건** — consumed 패키지 23건, settled 이벤트 31건. 새 sweep은
배포 직후 아무것도 재개하지 않는다. 이는 "backfill이 필요 없다"는 근거이기도
하다: 모든 consumed 패키지가 이미 settled 이벤트를 갖고 있다.

남은 배포 확인은 계획의 「검증」 절 2·3번(첫 KR·US 사이클에서 카드가 그룹당
한 장, `telegram_ui` 헬스체크 정상)이다.
