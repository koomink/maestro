# 인수인계 — 단계 2 구현 중단 지점 (2026-08-13)

새 세션이 이 작업을 이어받기 위해 필요한 전부. 읽는 순서: 이 문서 → 계획 → 스펙.

## 지금 어디인가

```
브랜치  feat/telegram-ux-phase2  (origin에 푸시됨)
기준선  1392 passed, 9 skipped / ruff clean
main    f41dd87 + 스펙·계획 커밋 (origin/main 동기화됨)
```

**중요: 이 브랜치는 아직 프로덕션 동작을 하나도 바꾸지 않는다.** `ui/lifecycle.py`를
호출하는 곳이 아직 없다. 그것이 남은 Task 5다. 롤백은 브랜치를 버리면 끝난다.

## 완료 (7개 중 5개)

| # | 내용 | 파일 |
|---|---|---|
| 1 | 카드 상태 계약 — `(card_key, chat_id)`, intent/result/failure, `operation_id` | `ui/card_state.py` |
| 2 | 전송 — intent → send → result, 예외별 delivery 분류 | `ui/lifecycle.py`, `bot.py` |
| 3 | 투영 테이블 + `refresh` | `state/store.py`, `ui/lifecycle.py` |
| 4 | 데일리 카드 + 단계 판정(진행/주의 두 축) | `ui/cards.py`, `ui/approval_stage.py` |
| 7 | 폴백 + ambiguous 통지 | `ui/lifecycle.py`, `ui/catalog.py` |

Task 6은 `catalog.NO_ACTION_NOTICE` 문구만 있고 **CLI에 연결되지 않았다.**

## 남은 것 — Task 5 (계획 파일의 해당 절 참조)

`handlers.py`(4,900행)에 `_sweep_lifecycle_cards`를 등록하고 승인 카드를 lifecycle로
이관한다. 계획에서 **가장 얇게 서술된 태스크이고, 계획대로 굴러가지 않을 확률이 가장
높다고 미리 표시해 둔 곳**이다.

구체적 난점 세 가지:

1. **recovery 상관관계.** `live_order_recovery_required` 페이로드에는 `approval_id`가
   없다 — `order_id`뿐이다(운영 DB에서 확인). 승인과 잇는 경로는
   `live_order_batch_lifecycle`의 `items[].request.approval_id` 또는
   `live_order_submit_intent`의 request다.
2. **역행 금지 적용.** `approval_stage.keep_forward_progress`가 이미 있다. 투영에 저장된
   직전 진행값과 비교해 적용해야 한다.
3. **기존 알림 경로를 지우지 말 것.** 스펙 「단계 2의 안전망」이 병행 유지를 요구한다.
   제거는 단계 5다. 계획은 이를 테스트로 고정하라고 지시한다.

`_sweep_pending_approvals`(handlers.py:2018)가 봉투 순회와 chat별 재시도의 기존
선례다. `poll_once`(handlers.py:293)의 sweep 튜플에 추가하면 예외 격리를 그대로 받는다.

## 반드시 알아야 할 함정

- **`list_system_events_by_type`은 `ORDER BY id DESC`** (store.py:1815), 기본 `limit=10`.
  시간순으로 접으려면 뒤집어야 한다. **현재 상태 재구성에 쓰지 말 것** — 투영 테이블
  `telegram_ui_card_state`를 읽는다.
- **`duplicate_key`에 UNIQUE 인덱스**(store.py:203) + `save_system_event`는 평범한 INSERT.
  재시도 가능한 이벤트 키에는 시도별 고유값이 필요하다.
- **`writer_lock`은 advisory flock이지 트랜잭션이 아니다.** 원자성이 필요하면 단일 연결에
  `isolation_level = None` + `BEGIN IMMEDIATE` (`record_card_event`가 그 예).
- **잠금 순서는 항상 `live_order_lock` → `writer_lock`.** 위반하면 `RuntimeError`.
- **모르는 것을 안다고 취급하지 말 것.** 이 세션에서 같은 실수를 두 번 했다(락 리스 자동
  회수, 카드 ambiguous 재전송). 관측 가능성을 만든 직후 자동 조치를 붙이는 형태로 나타난다.

## 검증 규율

모든 하중 테스트는 **뮤테이션으로 비공허성을 증명**한다. 구현을 되돌려 어떤 테스트가
실패하는지 확인하고 복원한다. 이 브랜치에서 확인한 것:

| 되돌린 것 | 잡은 테스트 |
|---|---|
| chat 차원 접기 | 7건 |
| 모든 예외를 `failed`로 | `..._timeout_after_telegram_accepted_stays_unknown` |
| ambiguous 재전송 허용 | `..._an_ambiguous_copy_is_never_resent` |
| 투영 대신 이벤트 스캔 | `..._card_older_than_any_event_window_is_still_found` |
| 폴백 임계값 3→5 | `..._three_consecutive_rejections_send_a_plain_text_fallback` |

## 미확인 항목

**2026-08-13 22:40 KST US 런의 결과를 아직 보지 못했다.** 오늘 배포한 락 순서 수정
(main `7953e61`)의 유일한 실전 검증이다. 확인 방법:

```bash
journalctl -u maestro-symphony-signal-us.service --since "2026-08-13 22:35" --no-pager
journalctl -u maestro-resume-order-tracking.service --since "2026-08-13 22:35" --no-pager \
  | grep -cE "Traceback|is busy"
```

성공 기준: 로테이션이 끝까지 집행되고, 그 동안 `resume-order-tracking` 실패가 0이다.

## 보류된 별도 작업

`docs/superpowers/plans/2026-08-13-lock-critical-section.md` (9개 태스크)는 **의도적으로
보류**했다. 관측된 프로덕션 장애는 이미 고쳐져 배포됐고, 그 계획이 다루는 것은 아직
일어나지 않은 문제다. 그 부위가 실제로 아플 때 꺼낸다.
