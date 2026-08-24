# 3a 업그레이드 / 롤백 운영 절차

3a-4(funding workflow head/CAS)와 3a-5(업그레이드 backfill · 롤백 preflight)를
배포하거나 되돌릴 때의 절차다. 대상 유닛은 `deploy/systemd/`에 실제로 들어 있는
것들이며, 목록은 `src/maestro/ops/quiesce.py`가 소유하고
`tests/test_quiesce_units.py`가 `deploy/systemd/`와 대조해 검증한다 — 유닛을
새로 추가하면 그 테스트가 먼저 깨진다.

---

## 1. 권위 있는 상태 모델

3a-5 이후, 각 서브시스템에는 **현재 세대의 권위 있는 해석이 하나씩만** 있다.
같은 사실에 대한 두 번째 의견은 남기지 않는다 — 두 정의가 어긋나는 순간
시스템은 둘 중 하나를 골라 실행하고, 어느 쪽을 골랐는지는 사후에야 드러난다.

| 분류 | 뜻 | 이 저장소에서 |
|---|---|---|
| **현재 권위 상태** | 현재 런타임의 결정이 여기서만 나온다 | `funding_workflow_head` / `_claim` / `_completed` / `_superseded`, `signal_dispatch_manifest`, `telegram_approval_ack`(schema_version ≥ 2) + `telegram_approval_resolution_completed` |
| **롤백 호환 투영** | 구 바이너리가 읽으라고 쓰는 것. 현재 코드는 생명주기 판정에 쓰지 않는다 | `contribution_funding_request_ack`, `contribution_budget_request_decision` |
| **과거 상태** | 감사·마이그레이션 해석용 불변 이력 | schema_version 없는 `telegram_approval_ack`, cutoff 이전의 legacy 요청 행 |
| **격리 상태** | 자동 해석이 안전하지 않았다는 명시적 기록 | `funding_workflow_migration_quarantine` |
| **마이그레이션 메타데이터** | 업그레이드 소유권과 재개 제어 | `funding_workflow_migration_started` / `_completed` |

### 롤백 호환 투영에 대하여

`complete_workflow`는 `funding_workflow_completed`와 legacy 종결 이벤트를
**한 트랜잭션으로** 쓴다. 따로 쓰면 그 사이의 중단이 "완료됐지만 구 코드가
보기엔 여전히 pending"인 상태를 만들고, 그 상태로 롤백하면 구 handler가
`run_signal()`을 한 번 더 돌려 이번 달 현금흐름을 두 번 기록한다.

현재 런타임은 **투영을 읽지 않는다.** `_load_pending_funding_request` /
`_load_pending_budget_request`는 워크플로우 상태만 본다. 투영을 코드에서
문자열로 쓸 수 있는 모듈은 두 개뿐이고, 그 목록은
`tests/test_authoritative_funding_state.py`의
`ALLOWED_LEGACY_READERS`가 AST로 강제한다.

예외 하나: `orchestrator._selected_contribution_budget`이 투영 payload에서
`selected_budget` **금액**을 읽는다. `funding_workflow_completed`에는 그 필드가
없고, 금액을 위해 두 번째 기록을 새로 만드는 것이 더 나쁜 선택이기 때문이다.
대신 **생명주기 판정**을 권위 있게 만들었다: cutoff 위의 결정 행에 대응하는
완료가 없으면 그 자리에서 `ValueError`를 올린다. 조용히 건너뛰면
`available_cash`로 흘러 **운영자가 고른 것보다 많이 투자**한다.

---

## 2. quiesce 장벽

**서비스만 멈추는 것으로는 부족하다.** 세 가지가 동시에 성립해야 한다.

1. 모든 writer 유닛이 inactive
2. 모든 activator(타이머 · `.path` · 재시작 헬퍼)가 inactive
3. 관련 유닛에 대해 대기 중인 systemd job이 없음
   (`systemctl is-active`는 start job이 **큐에 있는** 유닛도 "inactive"라고
   답한다 — 검사 통과 직후에 뜬다)

그 위에 마이그레이션/preflight가 **StateStore writer lock을 작업 전체에 걸쳐**
잡는다. systemd 장벽은 배포된 프로세스를 막고, writer lock은 운영자가 손으로
실행하는 `maestro` CLI나 복구 스크립트를 막는다. 둘 다 필요하다.

`maestro quiesce-status`가 세 조건과 각 유닛의 현재 enable 상태를 찍는다.
아무것도 정지·기동시키지 않는다.

### 함정 두 가지

**대시보드는 read-only가 아니다.** 이름은 "Maestro read-only Dashboard"지만
`POST /api/dashboard/refresh`는 브로커·FX 상태를 갱신하고
`POST /api/dashboard/virtuoso/{strategy_id}/generate-signal`은 시그널을 돌린다.
둘 다 `system_events`에 쓴다. writer로 분류돼 있다.

**`maestro-run-once.service`를 멈추면 텔레그램 오퍼레이터가 켜진다.**
그 유닛은 `ExecStartPre=systemctl stop maestro-telegram-operator.service`와
`ExecStopPost=systemctl start maestro-telegram-operator.service`를 선언한다.
그래서 정지 순서는 `quiesce.QUIESCE_STOP_ORDER`를 따른다 — activator 먼저,
그 다음 one-shot writer, `run-once`를 오퍼레이터보다 **먼저**, 오퍼레이터와
대시보드를 마지막에. 순서를 지켜도 마지막에는 반드시 재검사한다.

---

## 3. 업그레이드 절차

```bash
# 1. 릴리스 확인
git -C /home/symphony/maestro log --oneline -1

# 2. 되돌릴 상태를 먼저 기록한다. 나중에 `enable --now`로 일괄 복구하면
#    운영자가 일부러 꺼 둔 writer까지 켜진다.
maestro quiesce-status | tee ~/quiesce-before-upgrade.txt

# 3. DB 백업 + 무결성 검사
cp /path/to/state.db ~/state-before-upgrade.db
sqlite3 /path/to/state.db 'PRAGMA integrity_check;'

# 4. activator를 먼저 내린다 (타이머 · path · 재시작 헬퍼)
sudo systemctl disable --now \
  maestro-book-performance.timer maestro-dashboard-health.timer \
  maestro-fx-refresh.timer maestro-heartbeat.timer \
  maestro-resume-order-tracking.timer maestro-run-once.timer \
  maestro-symphony-readonly.timer maestro-symphony-readonly-kr.timer \
  maestro-symphony-readonly-us.timer maestro-symphony-signal.timer \
  maestro-symphony-signal-kr.timer maestro-symphony-signal-us.timer \
  maestro-dashboard.path
sudo systemctl stop \
  maestro-dashboard-src-watch.service maestro-dashboard-health.service \
  maestro-dashboard-reload.service

# 5. writer를 QUIESCE_STOP_ORDER대로 내린다. run-once가 오퍼레이터보다 먼저다.
sudo systemctl stop \
  maestro-symphony-signal.service maestro-symphony-signal-kr.service \
  maestro-symphony-signal-us.service maestro-symphony-rebalance-kr.service \
  maestro-symphony-rebalance-us.service maestro-symphony-readonly.service \
  maestro-symphony-readonly-kr.service maestro-symphony-readonly-us.service \
  maestro-fx-refresh.service maestro-heartbeat.service \
  maestro-resume-order-tracking.service maestro-run-once.service
sudo systemctl stop maestro-telegram-operator.service maestro-dashboard.service

# 6. 장벽 재검사. quiesced=True가 아니면 여기서 멈춘다.
maestro quiesce-status

# 7. backfill 실행 (--require-quiesce가 기본값이다)
maestro upgrade-backfill --config "$MAESTRO_READONLY_CONFIG"
```

`upgrade-backfill`은 writer lock을 **전체 작업 동안** 잡고,
`migration_started`로 cutoff를 고정한 뒤 head backfill → 승인 분류 →
dispatch 분류 순으로 돌고, **마지막 쓰기로** `migration_completed`를 기록한다.

절대 하지 않는 것: 브로커 주문 제출, 승인 재집행, 시그널 생성·재생, 과거
dispatch 의도의 재계산, 현금흐름 기록, 브로커 체결 사실이나 과거 취소·완료
사실의 날조.

```bash
# 8. 막는 격리가 남았는지 본다
maestro upgrade-backfill --config "$MAESTRO_READONLY_CONFIG" | grep quarantine

# 9. 무결성 재검사
sqlite3 /path/to/state.db 'PRAGMA integrity_check;'

# 10. 2번에서 기록한 원래 상태로 정확히 되돌린다 (일괄 enable 금지)
# 11. 서비스 기동 후 health/smoke 확인
maestro health --config "$MAESTRO_READONLY_CONFIG"
# 12. 의도치 않은 자동 복구가 돌지 않았는지 확인
sqlite3 /path/to/state.db \
  "SELECT event_type, COUNT(*) FROM system_events WHERE id > <cutoff> GROUP BY 1;"
```

### ⚠️ funding 소유권이 모호하면 서비스를 올리지 않는다

`ambiguous_pending_requests` / `head_ownership_conflict` /
`malformed_workflow_identity` 격리가 하나라도 남으면 마이그레이션은
**완료되지 않고 MIGRATING 상태로 끝난다.** 그 상태에서는 런타임 게이트가
계속 닫혀 있어 funding/budget 콜백과 복구 sweep이 전부 서 있는다.

**시스템을 멈춰 두는 쪽이 잘못된 head를 살려 두는 것보다 안전하다.**
잘못된 head는 이번 달 투자를 운영자가 고르지 않은 요청에 붙인다.

---

## 4. 격리(quarantine) 처리

| reason | 서브시스템 | 완료를 막는가 | 무엇이 발견됐나 / 운영자가 할 일 |
|---|---|---|---|
| `ambiguous_pending_requests` | funding | **예** | 같은 워크플로우(scope+월)에 pending 요청이 2건 이상. 어느 쪽이 유효한지 기록에 없다. 운영자가 남기지 않을 요청에 `funding_workflow_superseded`를 직접 기록한 뒤 재실행한다. |
| `head_ownership_conflict` | funding | **예** | head가 가리키는 요청이 이 DB의 어떤 이벤트로도 확인되지 않거나, cutoff 이전의 다른 요청을 가리킨다. head 이력을 직접 확인해야 한다. |
| `malformed_workflow_identity` | funding | **예** | 요청에 `month_key`가 없어 워크플로우 식별자를 만들 수 없다. 어느 달의 예산인지가 곧 돈이 가는 곳이므로 유추하지 않는다. |
| `execution_may_have_been_entered` | approval | 아니오 | schema 없는 legacy ack + `approvals` 행은 있고 종결 기록이 없다. **주문이 이미 브로커에 나갔을 수 있다.** 증권사 앱에서 먼저 확인한다. |
| `completion_unprovable` | approval | 아니오 | legacy ack의 완료를 증명할 수 없다(예: 한 run에 그룹이 둘인데 완료 이벤트에 approval_id가 없다). |
| `legacy_dispatch_no_manifest` | dispatch | 아니오 | manifest 없이 consumed된 dispatch. 자동 재개하지 않는다 — 과거 의도가 기록되지 않아 지금 다시 계산하면 그 run이 실제로 정한 것과 다른 주문이 나갈 수 있다. |

approval·dispatch 격리가 완료를 막지 않는 이유는, **현재 런타임이 이미 그것들을
자동 실행하지 않기 때문**이다(`_resume_unresolved_approvals`는 schema 없는
ack를 건너뛰고, `_resume_incomplete_dispatches`는 manifest가 없으면 재개하지
않는다). 격리 기록은 게이트가 아니라 **주인 표시**다.

### 하지 않는 것

- legacy 승인에 `telegram_approval_resolution_completed`를 **합성하지 않는다.**
  "approvals 행 없음 + 완료 증거 없음 = 취소됨"은 로컬 영속화의 공백에서
  브로커의 행동을 읽어내는 추론이다. 주문은 프로세스가 죽기 전에 이미 나갔을
  수 있다.
- manifest 없는 dispatch의 과거 의도를 **재계산하지 않는다.**
- 모호한 funding 워크플로우의 승자를 **고르지 않는다.**

---

## 5. 크래시와 재개

| 중단 시점 | 재실행 시 |
|---|---|
| `migration_started` 이전 | 소유권 없음. 다시 quiesce하고 시작한다. |
| `migration_started` 이후 | cutoff는 이미 고정됐다. **같은 cutoff를 그대로 재사용한다** — 새 경계를 고르면 첫 시도가 legacy로 분류한 행이 현재 세대로 넘어간다. |
| head backfill 도중 | 이미 쓰인 결정적 head를 자기 작업으로 인식하고, 없는 것만 이어서 만든다. 중복 소유권은 생기지 않는다. |
| 격리 분류 도중 | 이미 쓰인 결정적 격리 행을 인식하고 이어서 분류한다. |
| `migration_completed` 이전 | 상태는 MIGRATING으로 남고, 민감한 런타임은 계속 게이트된다. |
| `migration_completed` 직후 (CLI 출력 전) | DB는 COMPLETED. 재실행은 아무것도 쓰지 않는 검증 패스다. |

모든 마이그레이션 쓰기는 안정적인 식별자에서 만든 `duplicate_key`를 쓰고
payload에 시계를 담지 않는다. 담으면 재개가 내용 비교에서 `ValueError`로
죽는다(`StateStore.save_system_events_atomic`의 replay 검증).

---

## 6. 롤백 절차

```bash
# 1. 업그레이드와 동일한 quiesce (3절 2~6단계)
# 2. DB 백업 + 무결성 검사
# 3. preflight (writer lock을 잡고, 읽기 전용으로 돈다)
maestro rollback-preflight --config "$MAESTRO_READONLY_CONFIG"
```

하나라도 어긋나면 **롤백을 중단한다.** 각 실패는 불변식 이름 · 식별자 ·
관련 event id · 왜 위험한지를 따로 찍는다.

| 불변식 | 구 바이너리가 무엇을 잘못하는가 |
|---|---|
| `R0_migration_state` | 마이그레이션이 진행 중이거나 마커가 모순. 구 코드는 어느 쪽도 해석하지 못한다. |
| `R1_workflow_claim_unresolved` | claim은 있고 completed가 없다. 구 handler는 claim을 읽지 않으므로 요청을 pending으로 보고 `run_signal()`을 다시 돌려 현금흐름을 두 번 기록한다. |
| `R2_dispatch_unsettled` | consumed인데 settled가 없다. 구 코드는 consumed를 영구로 취급해 승인 카드가 유실된다. |
| `R3_approval_unresolved` | schema_version ack은 있고 resolution이 없다. 구 handler는 ack만으로 종결로 보아 승인된 주문이 영영 나가지 않는다. |
| `R4_missing_legacy_projection` | `funding_workflow_completed`에 대응하는 legacy 종결 이벤트가 없다. |

**preflight는 검사기지 복구 도구가 아니다.** R4가 걸려도 legacy 이벤트를
만들어 넣지 않는다. `complete_workflow`가 둘을 한 트랜잭션으로 쓰므로 없다는
것은 손상 · 수동 변경 · 중간 빌드 중 하나라는 뜻이고, 여기서 지어내면 무엇이었는지
알 수 없게 되며 이 코드가 알 수 없는 종결 상태를 단언하게 된다. 해당 워크플로우를
정확히 찍고 실패한다.

```bash
# 4. 통과하면, 유닛이 정지된 상태 그대로 구 바이너리를 배포한다
# 5. 구 코드 기동/읽기 동작 확인
# 6. 1절에서 기록한 원래 상태로 정확히 복구, 기동
# 7. health 확인
```

---

## 7. ⚠️ 롤백 후 구 코드가 쓰고, 다시 업그레이드하는 경우

```
migration_completed(cutoff=100)
    ↓ 롤백
구 코드가 event 101..150을 쓴다
    ↓ 재배포
maestro upgrade-backfill
```

이때 `upgrade-backfill`은 **의도적으로 실패한다.** `reupgrade_after_rollback`을
찍고 해당 행들을 이름으로 열거한 뒤 아무것도 쓰지 않는다. 두 세대를 병합하는
방법을 추측하지 않고, 새 cutoff를 고르지도, epoch 2를 만들지도 않는다.

탐지 근거는 둘 다 **이 세대가 만들 수 없는 쓰기**의 적극적 증거다(무언가가
없다는 추론이 아니다):

- `legacy_terminal_without_completion` — `complete_workflow`는 워크플로우 완료와
  legacy 투영을 한 트랜잭션으로 쓴다. 갈라져 있다는 것은 워크플로우를 모르는
  writer가 썼다는 뜻이다.
- `request_without_head` — `plan_contribution_request`는 요청과 head를 한
  배치로 커밋한다. 어떤 head도 가리킨 적 없는 요청도 마찬가지다.

이 상황이 실제로 발생하면 전용 마이그레이션 절차를 설계해야 한다.
**강제로 통과시키지 않는다.**

---

## 8. 이후 단계 — Phase 3a-6 (legacy 호환 은퇴)

롤백 창(구 바이너리로 되돌릴 가능성)이 닫힌 뒤에 다음을 제거한다.

1. `complete_workflow`의 legacy dual-write
2. rollback preflight의 R4
3. `orchestrator._selected_contribution_budget`의 cutoff 기반 legacy 금액 읽기
   (그 전에 금액을 현재 세대 상태로 옮기는 설계가 필요하다)
4. `tests/test_authoritative_funding_state.py`의 `ALLOWED_LEGACY_READERS`

**롤백 창이 닫히기 전에는 제거하지 않는다.** 투영이 없으면 구 바이너리는
완료된 요청을 pending으로 보고 이번 달 투자를 한 번 더 실행한다.
