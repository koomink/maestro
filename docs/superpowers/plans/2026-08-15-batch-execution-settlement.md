# 배치 집행 결과 분류 + 승인 종결 (단계 4a) Implementation Plan

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md`
(「C-1. 배치 부분 집행 카드」, 「마이그레이션 순서」 4a 행)
**선행:** 3a-1(2단계 영속화), 3a-3 범위 A(dispatch resume) — 둘 다 완료

**Goal:** 한 승인의 주문들이 실제로 어디까지 갔는지 증거로 분류하고, 그 결과를
사실대로 기록해 승인을 종결할 수 있게 한다. 카드 UX는 4b이며, 이 계획이 만드는
분류·종결을 그대로 쓴다.

## Context

로테이션은 **먼저 팔고 그 돈으로 산다.** 중간에 멈추면 "주문 하나가 체결되지
않은" 상태가 아니라 **"팔기는 했는데 사지를 못한"** 상태가 된다.

두 번 일어났다:

- **2026-08-12 US run** — `signal_approval_completed`에 `approval_status='approved'`와
  `orders_failed=1`이 함께 기록됐다.
- **2026-08-11 크레센도 US** — 매도 2건을 브로커에 낸 직후
  `TimeoutError: State writer lock is busy`로 멈췄다. 결과: TIP 23주 전량 체결,
  SSO 20주 미체결 취소, PDBC·SPY·BIL 매수 3건 미발주. 판 돈이 현금으로 남았고
  **나흘간 아무도 닫지 못했다.**

닫지 못하는 이유는 **종결 수단이 없어서다.** 확인한 사실:

- `telegram_approval_resolution_completed`를 쓰는 코드는
  `_record_resolution_completed`(`handlers.py:1579`) 하나뿐이고, 정상 집행 경로
  (`_resolve_async_approval`)에서만 호출된다. 운영자가 종결할 길이 없다.
- `maestro approval-rollback-preflight`(`cli.py:1558`)는 이 상태를 **탐지만**
  한다. 3a-1 계획에 있던 "미완 상태의 수동 종결 운영 도구"는 만들어지지 않았다.
- `ui/approval_stage.py`의 주석이 이미 이 공백을 적어 두었다: 실패한 주문은
  "닫을 방법이 단계 4 전에는 없는 과거의 사실"이라 ⚠️가 영구히 걸린다.

결과적으로 승인 카드가 ⚠️에 영구히 갇히고, 롤백 preflight가 계속 `unsafe`를
내며, 진짜 새 사고와 오래된 미종결 건이 구분되지 않는다.

**분류 규칙은 실제 사고 데이터로 검증했다** (2026-08-15, 운영 DB 읽기 전용):

| 종목 | 주문 | intent | result | 체결 | 분류 |
|---|---|---|---|---|---|
| PDBC | 366 | ✗ | ✗ | — | 미발주 |
| SPY | 6 | ✗ | ✗ | — | 미발주 |
| BIL | 15 | ✗ | ✗ | — | 미발주 |
| SSO | 20 | ✓ | ✓ | 0 | 미체결 취소 |
| TIP | 23 | ✓ | ✓ | 23 | 체결(전량) |

운영자가 증권사 앱에서 확인한 내용과 일치한다.

## Global Constraints

- **테스트 기준선**: `1533 passed, 9 skipped` (`441329b` 기준).
  `test_signal_approval_handoff.py`의 CLI 5건은 `/tmp` lock 소유권 문제로
  실패 중이며 이 작업과 무관하다(계획 `2026-08-15-approval-dispatch-resume.md`
  상태 노트 참조).
- 린트: `.venv/bin/python -m ruff check src tests --output-format=concise`
- **모든 하중 테스트는 뮤테이션으로 비공허성을 증명한다.**
- **추측하지 않는다.** 분류는 `live_order_submit_intent`·`live_order_result`·
  `fill_watermarks`라는 기록된 증거로만 한다. 증거가 없으면 "모름"이지
  "안 나갔음"이 아니다.
- **종결이 정상 집행으로 위장하지 않는다.** 종결 이벤트는
  `settled_by=operator`와 집행 내역을 담는다.
- **이 단계는 주문을 내지 않는다.** 재계산·재주문은 4b다. 4a는 읽기와 종결
  기록뿐이다.
- 커밋 trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## File Structure

| 파일 | 책임 |
|---|---|
| `src/maestro/ops/batch_execution.py` (신규) | 증거 → 주문별 분류 + 배치 요약. 순수 함수 |
| `src/maestro/state/store.py` | 승인 단위 intent/result/체결 조회 |
| `src/maestro/cli.py` | `approval-outcome`(읽기 전용), `approval-settle`(종결) |

`ops/`에 두는 이유: `workflow_recovery.py`가 이미 같은 intent/result 쌍을
소비한다(`workflow_recovery.py:197-213`). UI가 아니므로 `ui/`에 두면 단방향
의존 규칙(`handlers.py → ui/`)을 깨고, 4b의 카드 렌더러가 순수 함수로
남지 못한다.

---

### Task 0: 주문 결과를 증거로 분류한다

- `batch_execution.py`에 순수 함수와 값 타입:
  - `OrderOutcome` — `Literal["not_sent", "unknown", "filled", "partially_filled", "cancelled_unfilled", "still_open"]`
  - `classify_order(intent, result, filled_quantity, ordered_quantity) -> OrderOutcome`
  - `BatchOutcome(approval_id, orders: list[OrderLine], counts, has_unknown: bool)`
  - `summarize_batch(envelope, intents, results, fills) -> BatchOutcome`
- 분류표는 스펙 「C-1」과 동일하다. `unknown`(intent 있음 + result 없음)은
  **별도 값으로 유지한다** — `not_sent`와 합치면 4b가 재주문을 허용하게 되고,
  그건 중복 주문이다. 단계 2의 `delivery` 3값과 같은 이유다.
- `has_unknown`이 참이면 배치 전체가 "브로커 확인 필요"로 표시된다.
- 테스트 (`tests/test_batch_execution_outcome.py` 신규): 분류표 5행 각각,
  부분 체결(체결량 < 주문량), 체결량이 주문량을 넘는 경우(브로커 정정),
  주문 0건, `unknown`이 하나라도 있으면 `has_unknown`.
- 뮤테이션: `unknown`을 `not_sent`로 접으면 해당 테스트가 FAIL.

### Task 1: 승인 하나의 증거를 모으는 조회

- `store.py`에 `load_approval_execution_evidence(approval_id) -> dict`:
  해당 승인의 `live_order_submit_intent`·`live_order_result`를 `order_id`로
  묶고, 각 `broker_order_id`의 `fill_watermarks.cumulative_quantity`를 붙인다.
- **legacy payload 형태를 함께 읽는다.** intent/result의 approval_id는
  `$.request.approval_id`에 있다(최상위 생성 컬럼 `approval_id`는 비어 있다).
  이건 3a-3에서 확인한 사실이며, 여기서도 같은 경로로 조회한다.
- 테스트: 실제 08-11 건과 같은 모양의 픽스처로 5건이 정확히 분류된다.
- 뮤테이션: `$.request.approval_id` 조회를 최상위 `approval_id`로 바꾸면
  전부 `not_sent`가 되어 FAIL.

### Task 2: 승인을 사실대로 종결한다

- `store.py` 또는 `batch_execution.py`에 `settle_approval(...)`:
  `telegram_approval_resolution_completed`를 기록하되 payload에
  `settled_by="operator"`, `reason`, `outcome`(Task 0의 요약),
  `duplicate_key=f"telegram-approval-settled:{approval_id}"`를 담는다.
- **거부 조건** (하나라도 걸리면 종결하지 않는다):
  - ack가 없거나 `schema_version < 2` — 종결 대상이 아니다
  - 이미 `resolution_completed`가 있다 — 이미 닫혔다
  - `has_unknown`이 참 — 브로커에 닿았는지 모르는 주문이 있는 상태를 닫으면
    미정산 상태를 덮는 것이다. `--i-have-reconciled-with-broker`를 명시할
    때만 통과시키고, 그 사실도 payload에 남긴다.
- **`settled_by`를 읽는 쪽을 함께 손본다.** `_deliver_resume_completion_notices`
  (`handlers.py:1670`)는 이 이벤트를 아웃박스로 삼아 "재개 완료" 통지를
  보낸다. 운영자가 손으로 종결한 건에 "처리했어요" 통지가 가면 안 되므로
  `settled_by`가 있는 건은 건너뛴다.
- 테스트: 거부 조건 각각, 종결 후 `approval-rollback-preflight`가 `safe`,
  종결 이벤트가 `settled_by`를 갖고, 재개 통지가 나가지 않는다.
- 뮤테이션: `has_unknown` 거부를 없애면 해당 테스트가 FAIL.
  `settled_by` 건너뛰기를 없애면 통지 테스트가 FAIL.

### Task 3: CLI 두 개

```
maestro approval-outcome --approval-id appr_...      # 읽기 전용
maestro approval-settle  --approval-id appr_... \
  --reason "..." --confirm SETTLE
```

- `approval-outcome`은 아무것도 쓰지 않는다. 종결 전에 무엇을 닫는지 보는
  용도이며, 4b의 카드가 렌더할 내용과 같은 데이터다.
- `approval-settle`은 `--confirm SETTLE` 없이는 거부한다
  (`release-kill`의 기존 규약, `cli.py:1605`).
- 두 명령 모두 감사 로그를 남긴다.
- 테스트: `CliRunner`로 exit code와 출력, `--confirm` 누락 시 거부.

### Task 4: 08-11 승인을 닫는다

구현이 끝나면 운영 DB에서 실제로 종결한다. **별도 커밋으로 하지 않는다** —
코드가 아니라 운영 조치다. 절차:

1. `approval-outcome`으로 위 표와 같은 결과가 나오는지 확인
2. `approval-settle --reason "TIP 23주 체결, SSO 미체결 취소, 매수 3건 미발주.
   운영자가 다음 정규장에 /rebalancing으로 대체 수행"` 실행
3. `approval-rollback-preflight`가 `status=safe unresolved=0`을 내는지 확인

08-12 건도 같은 방식으로 검토한다(`orders_failed=1`이었으므로 분류 결과가
다를 수 있다 — 종결 전에 `approval-outcome`으로 먼저 본다).

---

## 검증

```bash
cd /home/symphony/maestro
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests --output-format=concise
```

운영 확인은 Task 4가 겸한다.

## 명시적으로 하지 않는 것

- **재계산·재주문** — 4b. 이 단계는 주문을 내지 않는다.
- **카드 UX** — 4b.
- **누락된 `orders` 행 backfill.** 08-11 건은 브로커에 나간 주문 2건이
  `orders` 테이블에 없다. 잔고는 브로커 스냅샷 기반이라 정확하지만 로컬
  주문 이력에는 비어 있다. 소급 기록이 성과 집계·귀속에 어떤 영향을 주는지
  먼저 판단해야 하므로 **별도 결정 사항으로 남긴다** — 이 계획에 끼워 넣으면
  검증 표면이 장부 전체로 넓어진다.
- **자동 종결.** 종결은 항상 운영자의 명시적 행위다. sweep이 스스로 닫으면
  반쯤 실행된 로테이션이 조용히 사라진다.
