# Telegram UI/UX 전면 개편 설계

날짜: 2026-08-09
상태 (2026-08-28 기준): 단계 1 완료, 단계 2 완료, 단계 3a-1~3a-5 **GitHub
코드베이스 엔지니어링 종결 완료**, 단계 4a 완료.
다음 아키텍처/구현 단계: **3b** (아키텍처 확정, 구현 계획 미작성) → 4b → 5.

> **이 상태 표기는 GitHub 개발 체크아웃에 대한 것이다.** 운영 서비스는 별도
> VPS에서 돌고 있고, VPS 운영 DB가 3a-5 마이그레이션을 수행했는지 여부는 이
> 문서가 주장하지 않으며 이 문서로 확인할 수도 없다. 엔지니어링 종결은
> 배포·마이그레이션 완료와 같은 말이 아니다. 운영 상태 판정과 절차는
> `docs/rollback_and_upgrade_3a.md`가 소유한다.

계획서: 3a-4 = `docs/superpowers/plans/2026-08-16-funding-workflow-head-cas.md`,
3a-5 = `docs/superpowers/plans/2026-08-24-upgrade-backfill-rollback-preflight-v2.md`
(구 `2026-08-16-upgrade-backfill-rollback-preflight.md`는 superseded — 그 문서의
세 태스크는 있는 그대로 구현하면 안 된다),
운영 절차 = `docs/rollback_and_upgrade_3a.md`
개정 16차: 2026-08-28 — Phase 3b 리뷰 지적 4건 반영. (1) **현재 활성 요청의
`unknown`이 입양·실행 가능성을 지배한다**: 그 요청의 두 번째 실행 가능한 물리
표현을 만들지 않는다(「B-1.3」). 그리고 선행 완료 게이팅은 렌더러 관례가 아니라
**콜백 admission 불변식**임을 명시했다(「B-1.5」). (2) 3a 절에 남아 있던 옛
5단계 롤백 요약을 삭제하고 운영 정본을 `docs/rollback_and_upgrade_3a.md` 하나로
확정했다 — 재부팅 안전 quiesce 이전의 서술이라 대안 절차로 오독될 수 있었다.
(3) 일반 `🔍 자세히` 콜백 규칙에서 논리 card_key를 항상 callback_data에 담는다는
함의를 제거했다. (4) 3a 이전 결함을 현재형으로 적어 둔 문단들을 과거형/구현
완료로 명시했다
개정 15차: 2026-08-28 — Phase 3b architecture reconciliation. 월간 자금 카드의
아키텍처를 확정해 「B-1」로 편입했다: Telegram Operator 단독 전달 소유,
`card_delivery_version` 세대 절단, legacy 요청 카드 입양, 워크플로우 범위 카드
식별자 + 요청 범위 금융 콜백, 권위 상태 → 사용자 단계 투영과 attention
오버레이, 파생 read model + 스윕 권한 경계, unknown 무재전송 정본화와 증거
기반 edit 대체, CLI 실행 카드 전송자 은퇴, 마이그레이션 게이트 stand-down.
3a 규약은 재설계하지 않고 3b가 **소비하는 이미 구현된 선행 조건**으로 다시
썼다. 함께 정리: 짧은 워크플로우 토큰 레지스트리 의무화 철회, `at-least-once
+ 중복 카드 정리` 문구 철회, `3a는 roll-forward-only` 문구를 조건부 롤백으로
교정, `telegram_ui_card` 이벤트 키를 실제 `operation_id` 모델에 맞춤
개정 14차: 2026-08-15 — 예외 카드에 **배치 부분 집행**을 추가하고(「C-1」),
단계 4를 4a(분류·종결 CLI)와 4b(카드 UX)로 분리해 4a를 3a-4·3b 앞으로
당겼다. 근거: 반쯤 실행된 로테이션이 2026-08-11·08-12 두 번 발생했고
둘 다 종결 수단이 없어 승인 카드가 ⚠️에 영구히 갇힌다
개정: 2026-08-09 Codex 적대적 리뷰 반영 — 승인 카드 2계층화,
UI 실패 fallback 경로, funding_workflow_id 도입
개정 2차: 2026-08-09 Codex 적대적 리뷰 2차 반영 — funding_workflow_id를
scope 복합 키로 재정의, 액션 라우팅은 request_id에만 바인딩
개정 3차: 2026-08-09 Codex 적대적 리뷰 3차 반영 — funding 요청 교체의
비원자성 대응: 영속 workflow 상태 머신 + 원자적 claim (접근 A의 명시적 예외)
개정 4차: 2026-08-09 Codex 적대적 리뷰 4차 반영 — child run lineage 영속화,
funding/budget 공통 상태 전이(claimed→child_created→completed), budget
decision의 비종결화, 단계 3 roll-forward-only 명시
(roll-forward-only는 **개정 15차에서 조건부 롤백으로 교정됨**)
개정 5차: 2026-08-09 Codex 적대적 리뷰 5차 반영 — 워크플로우 head/version
CAS로 단일 활성 요청 보장, 승인 dispatch의 idempotent resume
(consumed와 dispatch 완료 분리)
개정 6차: 2026-08-09 Codex 적대적 리뷰 6차 반영 — head 갱신의 트랜잭션
결합 + 수렴 sweep, claim의 attempt 기반 재개(fencing), Telegram 전송
exactly-once 포기, 단계 3을 3a/3b로 분리 (당시의 `at-least-once + 중복 카드
정리`는 **개정 15차에서 철회**되고 무재전송이 정본이 됨)
개정 7차: 2026-08-09 Codex 적대적 리뷰 7차 반영 — head 검증·claim 삽입을
조건부 트랜잭션으로 결합(TOCTOU 차단), child 생성의
(source_request_id, phase) 유일성 + lock 경계 내 재검증
개정 8차: 2026-08-09 Codex 적대적 리뷰 8차 반영 — 워크플로우 영속 식별자를
canonical 직렬화 scope 전체로 변경(해시는 표시용 토큰으로 격하 — 표시용 토큰
의무화는 **개정 15차에서 철회**), 유실 카드
자동 정리 불가 불변식 + callback self-heal
개정 9차: 2026-08-09 Codex 적대적 리뷰 9차 반영 — 승인 결정의 2단계
영속화(ack ≠ 종결, resolution 재개 규약), scope 직렬화를 타입 보존
canonical JSON으로 변경(sentinel 치환 금지)
개정 10차: 2026-08-09 Codex 적대적 리뷰 10차 반영 — 3a 업그레이드
backfill(legacy 요청 v1 head 생성, legacy ack 완료 판정), 롤백 preflight
조건 확장, scope 직렬화에서 NFC 정규화 제거(원시 코드포인트 보존)
개정 11차: 2026-08-10 Codex 적대적 리뷰 11차 반영 — 결정적
dispatch_group_id로 approval get-or-create, 롤백 preflight를 quiesce
장벽 아래로 이동
개정 12차: 2026-08-10 Codex 적대적 리뷰 12차 반영 — 3a 업그레이드에도
quiesce 장벽 적용, migration_started/completed 분리 + immutable cutoff,
backfill 멱등 재개 규약
개정 13차: 2026-08-10 Codex 적대적 리뷰 13차 반영 — completed와 legacy
종결 이벤트의 원자적 dual-write(롤백 호환), preflight에 legacy 종결
누락 검사 추가

## 배경과 목표

현재 Telegram 운영 봇은 개발자 중심이다. 명령어 20개의 설명이 전부 영어이고,
메시지 대부분이 `request_id: budget_a1b2...` 같은 key:value 덤프이며, 승인 요청과
주문 상태 알림이 개별 메시지로 흩어져 유저가 "지금 어떤 상태이고 내가 뭘 해야
하는지"를 파악하기 어렵다.

목표: 메시지 문구와 상호작용 구조를 Maestro/Symphony 워크플로우
(관찰 → 신호 → 승인 → 주문 → 정산, 월간 입금/예산)에 맞춰 전면 재설계한다.

### 확정된 방향 (브레인스토밍 결론)

1. **눈높이**: 일상 언어 문장 + `🔍 자세히` 접기. 평소에는 토스 알림 수준의
   한글 문장, 세부 정보(ID, 종목코드, 전체 자릿수)는 펼친 뷰에만 표시.
2. **범위**: 메시지 문구 + 상호작용 구조 전면 개편.
3. **구조**: 푸시 카드 + 라이프사이클 추적. 하나의 리밸런스 건 = 하나의 카드가
   상태 변화를 따라 edit로 갱신.
4. **명령어**: 20개 → 핵심 5개로 통합. 구 명령어는 동작 유지하되 메뉴에서 숨김.
5. **안전 플로우**: 가이드형 마법사. 상황 설명 + 선택지 버튼, 위험 액션은
   확인 단계 1번 추가.
6. **노옵인 날**: "오늘은 매매할 것이 없어요" 한 줄 알림을 보낸다.

### 채택한 구현 접근: UI 레이어 신설 + 점진 이관 (방안 A)

프레임워크 리라이트(방안 B)는 검증된 4,700줄의 승인/복구/안전 로직 재작성과
async 전환 리스크가 과도해 기각. 문구만 교체(방안 C)는 구조 개편이 불가능해
기각. 방안 A는 실거래 로직을 건드리지 않으면서 구조 개편을 실현하고,
렌더링 자산이 프레임워크 독립적이라 먼 미래의 B 전환 비용도 낮춘다.

## 아키텍처

```
src/maestro/integrations/telegram/
├── bot.py               (기존 — HTTP 클라이언트. 단계 1·2는 무변경.
│                        3b는 거부 사유 메타데이터 보존만 최소 추가 — 「B-1.7」)
├── handlers.py          (기존 — 비즈니스 로직 유지, 텍스트 생성만 ui/로 위임)
└── ui/                  (신규)
    ├── catalog.py       # 메시지 카탈로그: 모든 한글 문구·템플릿
    ├── cards.py         # 카드 렌더러: 상태 데이터 → (text, reply_markup)
    ├── lifecycle.py     # 라이프사이클 카드 매니저: message_id 추적 + edit
    └── format.py        # 한국어 포맷 유틸: 금액, 시간, 수량
```

- **catalog.py** — 순수 데이터(문자열 상수 + 템플릿 함수). 로직 없음.
  문구 수정은 이 파일만 수정하면 끝난다.
- **cards.py** — 순수 함수: `render_*(data) -> RenderedCard(text, reply_markup)`.
  네트워크·DB 접근 없음. 접힘/펼침 뷰를 같은 데이터로 렌더.
- **lifecycle.py** — 유일한 상태 보유 컴포넌트. 전달 복사본별
  `(card_key, chat_id, message_id, 단계, 렌더 해시)`를 state store의
  system event(`telegram_ui_card`)로 기록하고, poll 루프 sweep에서 상태 변화를
  감지해 `edit_message_text`로 갱신. 전송은 항상 intent를 먼저 기록한 뒤
  수행한다(아래 데이터 흐름 1번).
- **format.py** — `money_kr`, `deadline_kr` 등 한국어 표기 유틸.

**의존성 방향**: `handlers.py → ui/` 단방향. `ui/`는 handlers·orchestrator·
execution을 임포트하지 않는다. `execution/budget_requests.py`,
`execution/funding_requests.py`에 있는 메시지 생성도 `ui/`로 이동하고
execution 모듈은 데이터만 넘긴다.

기존 `TelegramBotClient`가 이미 `edit_message_text`,
`edit_message_reply_markup`, `set_my_commands`를 지원하므로 단계 1·2에서
클라이언트 변경은 없었다.

**단, 3b에서 `bot.py`는 더 이상 "변경 없음"이 아니다.** 현재
`TelegramApiRejected`는 실패 사실만 남기고 Telegram이 돌려준 `error_code`·
`description`을 버린다(bot.py:181). 3b의 edit 대체 판정은 "이미 원하는 상태",
"대상 메시지가 없음이 증명됨", "이유 불명의 거부"를 **구분할 수 있어야**
성립하므로, 3b는 메서드명·error_code·description을 예외에 보존하는 최소 변경을
허용한다. 그 이상의 클라이언트 재작성은 3b 범위가 아니다 — 「B-1.7」 참조.

## 카드 체계 (4종)

### A. 데일리 투자 카드 (라이프사이클 카드)

**중요한 제약**: orchestrator는 한 signal run의 주문을 전략 그룹별로 나누어
서로 다른 `approval_id`를 발급하며(`_approval_order_groups`), 각 그룹은
독립적으로 승인·거절·만료될 수 있다. 따라서 카드를 2계층으로 나눈다:

- **승인 카드 (액션 카드)** — `approval_id`당 하나. 승인/거절 버튼과 해당
  그룹의 라이프사이클(⏳ 대기 → 🔵 주문 진행 → ✅ 완료 / ⚠️ 확인 필요)을
  가진 유일한 실행 가능 카드. 버튼 callback은 항상 `approval_id`에 바인딩된다.
- **데일리 요약 카드 (부모 카드)** — `signal_run_id`당 하나. **읽기 전용**
  집계 뷰로, 하위 승인 그룹별 상태를 나열한다(예: "트랑필로 ✅ 완료 ·
  크레센도 ⏳ 대기"). 승인 버튼을 갖지 않는다.
- signal run의 승인 그룹이 **1개뿐이면 부모 카드를 생략**하고 승인 카드가
  데일리 카드 역할을 겸한다 (일반적인 경우 메시지 1개 유지).
- 혼합 상태(한 그룹 완료 + 다른 그룹 실패)와 순서가 뒤바뀐 이벤트 도착을
  렌더러·매니저 테스트로 검증한다.

```
신호 생성 ── 노옵 ──→ "오늘은 매매할 것이 없어요" 한 줄 알림 후 종료
    │
    └─ 액션 필요 ──→ 승인 그룹별 승인 카드 생성 (2개 이상이면 부모 요약 카드 추가)
                        각 승인 카드:
                        ⏳ 승인 대기 (승인/거절/자세히 버튼)
                        ├─ 거절/만료 → 카드에 사유 기록 후 종료
                        └─ 승인 → 🔵 주문 진행 중 (n건 중 m건 체결)
                                    ├─ 전량 체결 → ✅ 완료 (종목·수량·금액 요약)
                                    └─ 문제 발생 → ⚠️ 확인 필요 (예외 카드로 연결)
```

승인 대기 카드 예시 (접힌 뷰):

```
📩 이번 달 투자를 진행할까요?

트랑필로 전략 · 국내 주식 2종목 · 매수 2건 · 계좌 kis_ps · 총 124만원

• 🟢 매수 삼성전자 10주 — 71.2만원
• 🟢 매수 미국 S&P500 ETF 28주 — 52.8만원

⏰ 밤 11시 30분까지 응답해 주세요.

[✅ 승인] [❌ 거절] [🔍 자세히]
```

**승인 전 필수 노출 (접힌 뷰 규칙).** 승인 버튼이 붙는 첫 화면은 다음을 반드시
포함한다: **전략 · 시장 · 종목 수 · 매수/매도 구성 · 계좌 범위 · 총액 · 위험 사유
원문 · 마감 시각.** 계좌가 3곳을 넘거나 위험 사유가 3건을 넘으면 앞의 3개를 보이고
나머지는 `외 N곳` / `- 외 N건`으로 알린다.

`🔍 자세히`에만 두는 것: 개별 종목 목록의 6줄 초과분, 종목코드·지정가 전체 자릿수,
주문별 계좌 줄, approval_id/run_id/ISO 타임스탬프.

승인 버튼은 **항상 첫 카드에 제공한다.** "자세히를 눌러야 승인 가능"으로 게이팅하지
않는다 — 카드 렌더러는 순수 함수이고 "누가 펼쳐 보았는가"를 (approval_id, chat_id,
user_id)별로 영속화해야 하며, 다중 운영자 환경에서는 게이트가 무의미해지는 한편
`editMessageText` 실패 시 마감 시한이 있는 승인이 영구 불가능해지는 새 장애 모드가
생긴다. 정보를 감추지 않는 쪽(위 필수 노출 규칙)으로 같은 위험을 해소한다.

기존의 개별 주문 상태 알림(`Maestro live order update` 연속 전송)은 카드 갱신으로
대체한다.

### B. 월간 자금 카드

입금 요청/예산 선택 흐름을 하나의 라이프사이클로 통합:
`📥 입금 필요 → 입금 확인 중 → 예산 선택 → ✅ 이번 달 예산 확정`.

**안정적인 워크플로우 키**: 입금 확인 후에는 새 signal run이
생성되고 별도의 `budget_<uuid>` 요청이 만들어지므로, 최초 요청 ID로 카드를
고정하면 카드가 멈추거나 중복 생성된다. 이를 막기 위해 영속 키
`funding_workflow_id`로 funding 요청·재생성된 signal run·budget 요청을
연결한다.

**키의 유일성 범위**: `<account_id>:<YYYY-MM>`는 유일 키가 아니다.
funding/budget 요청은 order scope 단위로 생성되며 `account_id`는 nullable이고,
같은 계좌·같은 달에도 `contribution_group_id`·`execution_sleeve`·`currency`가
다른 독립 요청이 공존할 수 있다 (orchestrator의
`_contribution_funding_request` / `_contribution_budget_request`가 order
scope마다 호출됨). 따라서 워크플로우 키는 scope 전체를 포함한 복합 키로
정의한다:

```
funding_scope = 타입 보존 canonical JSON 배열
  [contribution_group_id, account_id, execution_sleeve, currency]
  - null은 JSON null 그대로 (문자열 sentinel로 치환하지 않는다)
  - 문자열은 **원시 코드포인트 그대로 보존** — 키 생성 시 유니코드
    정규화를 하지 않는다. 정규화하면 원시 값이 다른 NFC 등가 문자열이
    같은 head/CAS를 공유해 서로를 supersede할 수 있고, 이는 "서로 다른
    scope는 충돌하지 않는다"는 불변식과 모순된다. 식별자는 항상 동일한
    config 소스에서 나오므로 표기 등가성 병합은 필요하지 않다.
  - json.dumps(..., ensure_ascii=False, separators=(",", ":")) 고정 표기
funding_workflow_id = "funding:" + funding_scope JSON + ":" + month_key
```

직렬화 규약을 이렇게 고정하는 이유: nullable 값을 `-` 같은 문자열
sentinel로 치환하면 실제 값 `-`와 null이 즉시 충돌하고, 구분자
escaping이 없는 단순 join은 구분자를 포함한 식별자(현재
account/group/sleeve는 일반 str로 이를 금지하지 않음)끼리 같은 ID로
합쳐질 수 있다. JSON 배열 직렬화는 타입(null vs 문자열)과 경계(escaping)
를 모두 보존하므로 서로 다른 scope가 같은 workflow_id가 되는 경로가
없다. null·`-`·구분자 포함 문자열·유니코드 정규화 차이 scope의 충돌
테스트를 명시한다.

**영속 식별자에 해시를 쓰지 않는다**: head/claim/lineage 등 정합성을
결정하는 모든 system event는 `funding_workflow_id`를 **canonical 직렬화된
원시 scope 문자열 그대로** 사용한다 (유니코드 코드포인트 보존, 정규화 없음 —
"정규화된 scope"라는 표현을 쓰지 않는 이유가 바로 위 문단이다). 짧은 해시(예: sha1 앞 8자리)는 32비트 식별
공간이라 서로 다른 scope가 같은 head/CAS를 공유하는 충돌이 가능하고,
충돌하면 한 scope의 요청 생성이 다른 scope를 superseded 처리해 월간
투자가 누락될 수 있다. system event payload에는 길이 제한이 없으므로
전체 키를 쓰는 데 비용이 없다. 방어선으로 head/claim 처리 시 payload에
저장된 원본 scope 튜플을 기대 scope와 비교해 불일치하면 거부한다.

**callback data 64바이트 제한 (개정 15차에서 완화)**: 이전 개정은 짧은
워크플로우 토큰 레지스트리를 **의무**로 요구했다. 그 요구는 철회한다.
`funding_workflow_id`는 DB·논리 카드 키일 뿐 **금융 콜백 payload가 아니므로**
내부적으로 길어도 무방하다. 금융 콜백은 지금처럼 request_id에 바인딩되어
있고(`operator:funding:complete:<request_id>`,
`operator:budget:sel:<request_id>:<choice>` 등) 64바이트 안에 들어간다.

따라서 이 설계는 새 워크플로우 토큰 테이블/레지스트리를 도입하지 **않는다**.
`ui:detail:...` 같은 비금융 접기/펼치기 콜백이 실제로 64바이트를 넘는다는
것을 **구체적인 바이트 길이 테스트가 증명하는 경우에만** 구현 계획이 유계
식별자(이미 존재하는 짧은 식별자 재사용 등)를 고르며, 그 선택은 구현 계획의
몫이지 이 아키텍처 문서가 미리 못박을 사항이 아니다. 어느 경우에도 그런
토큰은 표시·조회 전용이고 정합성 판단에 쓰지 않는다.

scope 필드들은 funding/budget 요청 payload에 이미 모두 존재하므로
**비즈니스 로직 변경 없이 UI 계층에서 파생 가능**하다 (접근 A 원칙 유지).

**액션 라우팅은 request_id에만 바인딩**: `funding_workflow_id`는 카드
식별·집계 전용이며, 버튼 callback은 항상 구체적인 `request_id`를 담는다.
교체·만료된 request_id의 callback은 활성 요청으로 위임하지 않고 "이미
지난 요청이에요. 최신 카드를 확인해 주세요."로 거절한다 — 구 버튼이 다른
요청을 취소/선택하는 오동작을 원천 차단한다.

**카드 상태는 활성 request_id를 소유하지 않는다 (개정 15차 교정).** 이전
개정은 "현재 활성 request_id와 요청 교체 이력을 카드 상태에 기록한다"고
적었는데, 그것은 금융 진실의 두 번째 정의를 UI 계층에 만드는 것이다. 활성
요청은 `funding_workflow_head`가 단독으로 소유하고, UI는 매번 그 권위 상태에서
**파생**한다. `telegram_ui_card`에는 전달 사실(어느 chat의 어느 message_id가
어느 단계로 갱신되었는가)만 남는다 — 「B-1.4」 참조.

**요청 교체의 비원자성 대응 (영속 workflow 상태 머신)**: "교체된 request_id
거절"은 UI 레이어만으로는 보장할 수 없다. **3a 이전의** funding 확인 흐름
(`_confirm_funding_request`)은 **현금흐름 기록 → `run_signal()`(신규
signal/요청 영속화) → funding ack 저장** 순서라서, `run_signal` 완료와 ack
저장 사이에 프로세스가 종료되면 구 요청은 여전히 pending이고 신규 요청도
이미 존재한다. `_load_pending_funding_request()`는 해당 request_id의 ack
유무만 확인하므로 구 버튼의 재시도가 signal run·현금흐름 기록을 중복
생성할 수 있다 — UI 개편 이전부터 존재하는 결함이며, funding 카드가
약속하는 상태 일관성의 전제이므로 단계 3a에서 함께 해결한다. 이 부분은
**"비즈니스 로직 불변" 원칙(접근 A)의 명시적 예외**로, handlers의 funding
확인 경로에 다음 규약을 도입했다 (아래 1~8은 3a에서 **구현 완료**된
현재 규약이다):

> **읽는 법 (개정 15차).** 아래 1~8은 원래 미래형으로 쓰였지만, 3a-1~3a-5
> 엔지니어링 종결로 **이미 구현된 선행 조건**이다. 3b는 이것들을 *소비*하며
> 재정의하거나 약화하지 않는다. 현재 구현은
> `src/maestro/state/funding_workflow.py`(head/claim/child/completed/superseded,
> `converge_workflow_invariants`, `list_incomplete_workflows`),
> `src/maestro/state/migration_state.py`(NOT_STARTED/MIGRATING/COMPLETED/INVALID
> 게이트), `src/maestro/state/rollback_preflight.py`(R0–R4, 읽기 전용)에 있다.
> 3b가 전제하는 불변식은 다음과 같고, 이 목록은 3b에서 **열지 않는다**:
> 워크플로우당 권위 있는 `funding_workflow_head` 하나 · 기존 CAS/precondition
> 규칙 아래에서만 이뤄지는 요청·head 발행 · `funding_workflow_claim`의 attempt
> fencing · 영속적인 정당 후속자 증명 · 영속 child lineage와 get-or-create ·
> 완료 권위로서의 `funding_workflow_completed` · 롤백 호환을 위한 completed +
> legacy 종결 dual-write · 운영자 승인 기반 미완 워크플로우 복구(자동 금융
> 재개 없음) · 권위 있는 마이그레이션 fence · 모호한 금융 이력의 fail-closed ·
> **증거가 없다는 것이 외부 부작용이 없었다는 증명은 아니라는 것.**

1. **워크플로우 head와 원자적 교체(CAS)**: request_id별 claim만으로는
   부족하다 — 같은 scope/month에 pending 요청 두 개가 공존하면(중단 후
   재생성, 수동/예약 run) 각자 다른 claim을 얻어 양쪽 모두 처리될 수 있다.
   워크플로우별 단일 활성 요청을 영속 head로 관리한다:
   `funding_workflow_head` system event에 (workflow_id, version, 활성
   request_id, 상태)를 기록하고, 갱신은
   `duplicate_key = head:<funding_workflow_id>:v<다음 version>`으로만
   커밋한다. duplicate_key의 전역 유일성이 같은 버전 전이의 동시 커밋을
   한 건으로 직렬화하므로 요청 생성·교체가 CAS로 동작한다. head에서
   밀려난 요청은 명시적 `superseded` 종결 상태로 기록한다.
   **head 갱신은 요청 생성과 한 트랜잭션이어야 한다**: 버전 duplicate_key
   만으로는 "요청 저장 후 head 갱신 전 중단 → head에 연결되지 않은 orphan
   요청" 또는 그 반대의 dangling head가 남는다. state store는 단일 SQLite
   이므로, 여러 system event(신규 요청, 이전 요청 superseded, 새 head)를
   duplicate_key 조건과 함께 **하나의 DB 트랜잭션으로 커밋하는 StateStore
   API**(`save_system_events_atomic`)를 신설해 요청 생성·교체·head 전환을
   원자화했다. 방어선으로, 복구 sweep가 재시작 시 불변식을 검사해
   orphan pending 요청(head 미연결)은 `superseded`로, dangling head
   (요청 실체 없음)는 직전 버전으로 수렴시키고 audit 이벤트를 남긴다.
   단, 이 수렴은 **마이그레이션 cutoff 이후에 생성된 요청에만**
   적용한다 — 3a 이전에 생성된 요청은 head가 원래 없으므로 orphan이
   아니며, 아래 "3a 업그레이드 backfill" 절차가 처리하고, 수렴 sweep
   자체가 `migration_completed` 마커 이전에는 활성화되지 않는다.
2. **원자적 claim (attempt 기반, 재개 가능)**: claim을 "존재하면 영원히
   거절"인 잠금으로 정의하면 child 생성 전에 중단된 워크플로우는 어떤
   경로로도 재진입하지 못해 영구 정지한다. claim은 **attempt 단위의 전이
   시도 기록**으로 정의한다:
   - **head 검증과 claim 삽입은 하나의 조건부 트랜잭션이다**: "head 조회 →
     별도 claim 기록" 순서로는 그 사이에 예약/수동 run이 새 요청과 head를
     원자 커밋하는 TOCTOU가 생겨, 이미 superseded된 구 callback이 유효한
     claim을 얻을 수 있다 (claim duplicate_key는 같은 request의 callback만
     직렬화할 뿐 head 교체와는 경쟁하지 않는다). 따라서 claim은
     `save_system_events_atomic`의 **precondition 기능**으로 커밋한다:
     트랜잭션 안에서 "현재 head의 request_id와 version이 기대값과 일치"를
     재검증하고, 일치할 때만 `funding_workflow_claim`
     (`duplicate_key = <funding_workflow_id>:<phase>:<request_id>:a<attempt>`,
     payload에 검증된 head version 포함)을 삽입한다. head가 그 사이
     바뀌었으면 claim 전체가 실패하고 callback은 거절된다.
   - 최초 진입은 attempt=1이며, 커밋에 실패하면(이미 존재하거나
     precondition 불일치) 처리에 진입하지 않고 "이미 처리 중이거나 완료된
     요청이에요"로 응답한다 — 동시 중복 callback과 head 교체 경쟁을 모두
     상태 변경 이전에 차단한다.
   - **재개는 attempt 증가로만**: 미완 attempt(claim은 있으나
     `completed`/`child_created` 없음)가 발견되면 복구 카드가 [재개] 버튼을
     제시하고, 운영자 승인 시 attempt+1 claim을 CAS로 커밋한 실행만이
     전이를 이어간다. attempt 번호가 fencing token 역할을 하므로 운영자가
     동시에 두 번 승인해도 재개는 한 건만 진입한다. 이전 attempt의 잔여
     실행이 뒤늦게 상태를 커밋하려 해도 자신의 attempt가 최신이 아니면
     기록을 거부한다.
3. **공통 상태 전이 `claimed → child_created → completed`**: funding과
   budget 전환 모두 이 3단계를 system event로 영속화한다. `completed`만이
   종결 상태다. 특히 **budget decision은 종결 ack가 아니라** 전환의 입력값
   (선택 금액)일 뿐이다 — **3a 이전 코드는** `contribution_budget_request_decision`
   저장 즉시 요청을 pending에서 제외했으므로, decision 직후
   refresh/config load/`run_signal()`에서 실패하면 child run 없이 요청이
   종결돼 월간 투자가 조용히 멈췄다. **3a 구현 이후에는** decision이 저장돼도
   `completed` 이벤트가 없으면 워크플로우는 미완으로 취급되어 복구 대상이
   된다.
   **completed는 legacy 종결 이벤트와 원자적으로 dual-write한다**:
   구버전 `_load_pending_funding_request()`는
   `contribution_funding_request_ack`만으로 종결을 판정하므로, 새
   `completed` 이벤트가 legacy ack를 **대체**하면 롤백 후 구버전이
   완료된 요청을 pending으로 오판해 `run_signal()`과 현금흐름 처리를
   재실행한다. 따라서 funding/budget 전이의 `completed` 기록은 대응하는
   legacy 종결 이벤트(funding은 `contribution_funding_request_ack`,
   budget은 `contribution_budget_request_decision`)와 **같은 원자 커밋**
   (`save_system_events_atomic`)으로 함께 기록해, 롤백 시 구버전이 추가
   조치 없이 종결로 인식하게 한다.
4. **child run lineage 영속화 + 생성 유일성(get-or-create)**: **3a 이전의**
   `run_signal()`은 원천 request를 알지 못했으므로 "claim 이후 생성된 run"을
   같은 전략·scope의 다른 수동/예약 run과 구분할 수 없다. `run_signal()`에
   `source_request_id`(및 funding_workflow_id)를 전달해 signal run
   기록/package에 영속화하고, 복구 시 이 lineage로만 child run을 조회한다
   (추론적 연결 금지). 같은 scope에서 동시에 존재하는 수동 run과의 조회
   유일성을 테스트로 보장한다.
   **attempt fencing만으로는 부족하다**: fencing은 상태 이벤트 커밋을
   거부할 뿐, `run_signal()`이 만드는 signal package·승인 흐름이라는
   부작용 자체를 막지 못한다. 이전 attempt가 `run_signal()` 내부에서
   지연된 사이 재개 attempt가 lineage를 조회하면 둘 다 "child 없음"을
   관찰할 수 있고, 전역 writer lock은 두 실행을 순차화할 뿐 두 번째
   실행의 재검증을 강제하지 않는다. 따라서:
   - source가 있는 child 생성은 **get-or-create**로 정의한다: signal run
     기록을 `duplicate_key = child:<source_request_id>:<phase>`로 커밋해,
     같은 원천 요청에서 child가 DB 수준에서 최대 1개만 생성되게 한다.
     duplicate_key 충돌 시 새 package를 만들지 않고 기존 child를 반환한다.
   - **최신 attempt 검증 → lineage 재조회 → child get-or-create**를 같은
     writer lock/트랜잭션 경계 안에서 수행한다. lock을 획득한 실행은
     진입 시점의 관찰이 아니라 lock 안에서 다시 읽은 상태로만 진행한다.
5. **중단 전환의 자동 재실행 금지**: claim은 있으나 `completed`가 없는
   상태로 재시작되면 같은 전환을 자동 재실행하지 않는다. lineage로 child
   signal run을 조회해 **있으면 재사용**하여 나머지 단계(승인 dispatch,
   completed 기록)만 이어서 수행하고, 없으면 기존 워크플로우 복구 카드
   ("이전 작업이 중단된 상태예요")로 라우팅한다. 복구 카드의 [재개]는
   항목 2의 attempt 증가 규약으로만 전이에 진입한다 — 운영자 승인 없이
   자동 재실행되는 경로는 없고, 승인돼도 실행은 한 건만 진입한다.
   budget의 경우 저장된 decision 금액을 그대로 사용해 재개한다.
6. **승인 dispatch의 idempotent resume**: "child run이 있으면 승인
   dispatch부터 재개"가 성립하려면 dispatch 자체가 재개 가능해야 한다.
   **3a 이전의** `dispatch_signal_approval`은 package를 **consumed로 먼저 기록한 뒤**
   그룹별 pending 이벤트 저장과 채팅별 Telegram 전송을 수행하므로,
   consumed 직후 또는 일부 그룹/채팅 전송 후 crash하면 재호출이
   "Signal package already consumed"로 거부되어 워크플로우가 영구 미완이
   되거나 일부 승인 카드만 노출됐다. **3a에서 구현된 현재 규약:**
   - consumed는 "dispatch 배타 시작" 표지로 재정의하고, **dispatch 완료
     판정과 분리**한다. 완료(`signal_approval_pending`)는 모든 그룹×채팅
     전송이 끝난 뒤에만 기록한다.
   - **결정적 dispatch_group_id로 approval을 get-or-create한다**: **3a 이전의**
     `ApprovalManager.create_request`는 매번 무작위 approval_id를
     발급했으므로, 일부 그룹 저장 후 crash한 재개 실행은 기존 그룹의
     approval_id를 알 수 없어 같은 주문 그룹에 새 approval을 만들 수
     있었다. 이를 막기 위해 그룹 분할·순서를 결정적으로 만들고(정렬),
     `dispatch_group_id = <signal_run_id> + canonical 그룹 구성(정렬된
     전략 ID·계좌 ID)`을 정의한다. envelope 저장은
     `duplicate_key = dispatch-group:<dispatch_group_id>`의 유일성 제약
     아래 **원자적 get-or-create**로 수행한다 — 이미 존재하면 새
     approval_id를 발급하지 않고 저장된 envelope(approval_id·**최초
     만료시각 포함**)을 그대로 재사용한다. 같은 주문 그룹에 승인 카드나
     결정 경로가 둘 이상 생기는 일이 DB 수준에서 차단된다.
   - **전송은 exactly-once가 아니라 intent 우선 + 모호성 보존이다**
     (개정 15차 정본 교정): sendMessage 성공과 성공 이벤트 저장 사이의
     중단은 원리적으로 구분할 수 없다. 채팅별로 전송 **intent**를 먼저
     영속화하고 성공 후 결과(message_id)를, Telegram이 `ok=false`로 명시
     거부하면 실패를 기록한다. 재개 시 **intent도 없는 채팅만** 새로
     발송한다. intent는 있으나 결과가 없는 **ambiguous 채팅은 재발송하지
     않는다** — 이전 개정의 "at-least-once + 중복 카드 정리"는 철회한다.
     버튼 달린 카드의 중복은 운영자에게 "두 개의 살아 있는 결정"으로
     보이고, 두 번째 탭이 claim에 거부되는 모습은 중복이 아니라 고장으로
     읽힌다. 모호한 복사본은 버튼 없는 안내로 에스컬레이션하고
     `telegram_ui` 헬스에 반영한다. 이것이 구현의 실제 동작이며(
     `ui/lifecycle.py`의 `deliver_once`·`refresh`), 「C-1」의 "모르는 것을
     안다고 취급하지 않는다"와도 같은 규칙이다.
   - **중복 카드는 안전하지만, 유실 카드의 자동 정리는 불가능하다
     (불변식)**: sendMessage 성공과 message_id 저장 사이에 중단되면 그
     카드의 message_id는 영구 유실된다. Telegram은 봇에게 메시지 이력
     조회 API를 제공하지 않으므로 lifecycle 매니저가 유실 카드를 찾아
     edit할 방법은 원리적으로 없다. 따라서 정리 보장은 두 층으로 나눈다:
     (a) **알려진 복사본 정리** — message_id가 기록된 모든 복사본을
     lifecycle이 누적 추적하고(카드당 message_id 목록), 상태 변화 시 전부
     갱신하며 중복 복사본은 "아래 새 카드로 대체되었어요"로 edit한다.
     (b) **유실 복사본은 클릭 시 self-heal** — 모든 승인 callback 처리는
     callback_query에 실려 오는 **그 메시지의 chat_id/message_id를 즉시
     최종 상태로 edit**하고(기존 `_edit_callback_message` 패턴), 해당
     message_id를 lifecycle 추적 목록에 등록한다. 승인 callback은
     approval_id 기반 멱등이므로(pending envelope 소비는 1회) 유실
     카드를 눌러도 결과는 같고, 누르는 순간 그 카드도 최신 상태로
     회복된다. 클릭되지 않은 유실 카드는 오래된 표시로 남을 수 있음을
     수용한다 (승인 만료 시간이 지나면 버튼도 무해하다).
   - resume 진입 조건: consumed이지만 완료 이벤트가 없는 package.
   이 변경은 funding 재생성 run만이 아니라 승인 dispatch 전체에 적용되며,
   접근 A 예외 범위에 포함된다.
7. **승인 결정의 2단계 영속화 (ack ≠ 종결)**: **3a 이전의**
   `_resolve_async_approval`은 `telegram_approval_ack`를 먼저 저장한 뒤
   주문 해석(`resolve_pending_signal_approval`)을 수행했고,
   `_pending_async_approval`과 `_sweep_pending_approvals`는 ack 존재만으로
   승인을 종결로 간주했다. 따라서 ack 저장 직후 프로세스 종료·config load
   실패·broker timeout이 발생하면 운영자의 승인 의사는 기록됐지만 주문은
   생성되지 않고, callback 재클릭도 "already decided"로 거절되어 **승인이
   영구 유실**됐다. **3a에서 구현된 현재 규약:**
   - 승인 결정도 `decision_recorded → resolution_completed` 2단계로
     영속화한다. ack(=decision_recorded)는 운영자 의사의 기록일 뿐 종결이
     아니며, **pending envelope 제외 판정은 `resolution_completed`
     기준**으로 바꾼다.
   - decision_recorded만 있는 승인은 "결정됨·집행 미완"으로 분류되어
     sweep이 재개를 시도한다. 기록된 결정을 그대로 사용하므로 운영자
     재클릭은 필요 없다. 재개 진입은 항목 2의 attempt 규약을 준용해 한
     건만 진입한다.
   - resolution 재개는 approval_id 기반 멱등이어야 한다: 이미 생성된
     주문(부분 실행 포함)을 조회해 재사용하고, 중복 주문을 만들지 않는다.
   - 반복 실패 시 승인 카드를 ⚠️ 확인 필요 상태로 전환하고 복구 카드
     ([다시 시도])로 라우팅한다 — 기존
     `telegram_approval_resolution_failed` 이벤트를 카드 상태 원천으로
     사용한다.
   - 라이프사이클 카드는 ack만으로 완료를 표시하지 않고
     `resolution_completed` 후에만 ✅/종결 상태로 렌더한다.

   **구현 메모 (3a-1, 2026-08-10)**: 종결 판정은 `telegram_approval_ack.schema_version`
   으로 신·구를 구분한다 — schema_version이 없는 ack(3a 이전)는 그 자체로 종결,
   `schema_version >= 2`인 ack는 `telegram_approval_resolution_completed`가 있어야
   종결이다. 별도의 cutoff 마커·DB 마이그레이션 없이 배포된다.

   자동 재개는 **approvals 행이 없는 승인**으로 한정한다 — `save_approval`은
   모든 브로커 호출에 선행하므로(orchestrator.py:332) 행이 없으면 부작용이
   없음이 증명된다. `live_order_lifecycle` 기록 유무로 판정하면 안 된다:
   브로커 제출이 먼저이고 기록이 나중이라(execution/live_order_lifecycle.py:76,
   :401) 그 사이 중단되면 중복 주문을 낸다. 행이 있는 상태의 재개(주문 단위
   멱등성)는 3a-3 범위이며, 그전까지는 ⚠️ 알림으로 운영자에게 라우팅한다.

   3a 이전 legacy ack는 자동 재집행하지 않되, 집행 증거(approvals 행 +
   `signal_approval_completed`)가 없는 건은 일회성 격리 통보를 보낸다.

   **왜 이 상태가 롤백에 위험한가**: 완료된 승인은 롤백해도 재집행되지
   않지만, `schema_version=2` ack가 있고 `resolution_completed`가 없는
   상태에서 롤백하면 구버전이 ack만 보고 종결 처리해 승인된 주문이 유실된다.
   이 위험 상태는 현재 롤백 preflight의 `R3_approval_unresolved`가 검출한다.

   **이 문서는 롤백 절차를 서술하지 않는다 (개정 16차).** 이전 개정이 여기에
   적어 둔 5단계 요약은 3a-5의 최종 설계 — 재부팅 안전(reboot-safe) quiesce
   장벽 — 이전의 것이라 지금은 대안 운영 절차로 오독될 수 있어 삭제했다.
   `systemctl is-active`만으로는 장벽이 증명되지 않으며(enabled 유닛은 재부팅
   시 되살아난다), preflight도 더 이상 "다음 태스크"가 아니라 이미 구현된
   읽기 전용 검사다. 절차의 정본은 `docs/rollback_and_upgrade_3a.md` 하나뿐이다.
8. **현금흐름 기록 멱등화**: 전환 내 현금흐름 기록도 request_id 기반
   duplicate_key로 멱등 처리하여, 복구 재시도 시 중복 기록되지 않는다.

### B-1. Phase 3b: 월간 워크플로우 카드 아키텍처 (개정 15차 확정)

「B」가 그리는 하나의 월간 라이프사이클을 실제로 세우는 단계가 3b다. 아래는
확정된 아키텍처이며, **구현 계획은 아직 쓰지 않았다.** 스키마·이벤트명·함수명
가운데 저장소에 이미 존재하지 않는 것은 의도적으로 열어 둔다 — 여기서 이름을
지어 두면 구현 계획과 이 문서가 곧바로 두 개의 정본이 된다.

3b는 「위 1~8」의 3a 불변식 위에 **얹히기만 한다.** head/CAS, claim fencing,
lineage, completed 권위, dual-write, 운영자 승인 복구, 마이그레이션 fence,
fail-closed 규칙 중 어느 것도 3b가 다시 열지 않는다.

#### B-1.1 네트워크 전달 소유자는 하나다

**실행 가능한(버튼 달린) funding/budget 카드의 네트워크 전달 소유자는
Telegram Operator 단독이다.** 목표 흐름:

```
daily-signal-approval / run_signal
  → 영속 request + workflow head + signal package
  → StateStore
  → Telegram Operator 스윕
  → 월간 워크플로우 카드 전송/갱신
```

CLI는 더 이상 두 번째 실행 카드 전송자가 아니다(「B-1.8」). **CLI와 operator가
둘 다 보낼 수 있게 하려고 새 프로세스 간 전달 CAS/리스 하위 시스템을 만드는
길은 택하지 않는다** — 그것은 이미 어려운 금융 CAS 옆에 두 번째 분산 합의를
세우는 일이고, 전달 소유자를 하나로 줄이면 애초에 필요가 없다.

조용한 날 안내나 일간 요약 같은 **비실행 정보성 알림은 3b가 자동으로 가져오지
않는다.** 3b는 "버튼이 달린 것"의 소유권만 옮긴다.

#### B-1.2 명시적 전달 세대 절단: `card_delivery_version`

`ContributionFundingRequest`와 `ContributionBudgetRequest` 양쪽에
`card_delivery_version`을 둔다. 의미는 승인 envelope에 이미 있는 같은 이름의
필드와 동일하다(`approval/models.py`, `handlers.py`의 `< 1` 게이트) — 저장소에
검증된 관용구가 있으므로 새 개념을 발명하지 않는다.

| 값 | 뜻 | 라이프사이클 상태가 없을 때의 해석 |
|---|---|---|
| `0` (기본·누락) | legacy 세대. CLI가 raw 전송해 message_id를 남기지 않았을 수 있다 | **최초 전송을 허가하지 않는다.** 상태의 부재가 미전송의 증거가 아니다 |
| `1` | Telegram Operator/lifecycle 소유 세대. 전송 **전에** intent를 남긴다 | 상태의 부재는 곧 **전송이 시작되지도 않았다는 적극적 증거**다. 스윕이 최초 전송을 대신할 수 있다 |

- 모델 기본값은 하위 호환을 위해 **`0`을 유지한다.**
- 3b 이후 새로 만들어지는 요청은 **명시적으로 `1`을 쓴다.**
- 이 값은 **Telegram 전달의 provenance 표지일 뿐이다.** `funding_workflow_head.version`
  이 아니며 금융 CAS에 참여하지 않는다. 두 version을 같은 축으로 읽는 순간
  전달 실패가 금융 상태를 되돌릴 수 있게 된다.
- 타임스탬프나 배포 시각 추론보다 **명시적 영속 세대 표지를 택한다.** 시각
  비교는 재기동·시계 이동·백필 앞에서 조용히 틀린다.

#### B-1.3 legacy 요청 카드 입양

3b는 논리 카드 신원을 요청 범위(`funding-request:<request_id>`,
`budget-request:<request_id>`)에서 **워크플로우 범위 월간 카드 하나**로 바꾼다.
따라서 이미 나가 있는 legacy 요청 카드를 어떻게 이어받을지가 설계 사항이다.

**입양은 새 전송이 아니다.** 입양을 "워크플로우 카드를 보냈다"로 기록하면
보내지 않은 것을 보냈다고 적는 것이고, 그 기록은 이후 모든 판단의 근거가 된다.
입양의 provenance(무엇을 어디서 물려받았는지)는 반드시 남긴다. 정확한
이벤트명·스키마는 구현 계획의 몫이다.

**현재 활성 요청의 `unknown`이 입양을 지배한다 (지배 규칙).** 이것이 이 절의
가장 중요한 불변식이다:

> **어떤 chat에서든 현재 활성(head) 요청의 legacy 복사본이 `unknown`인 동안,
> 3b는 그 요청의 실행 가능한 표현을 하나 더 만들지 않는다.** 다른 알려진
> 선행자 메시지를 그 요청의 실행 가능한 카드로 승격(edit)해서도 안 되고, 새
> 카드를 보내서도 안 된다.

왜 필요한가. 다음 중단 형태를 보자:

```
funding A 카드 = confirmed, message_id 알려짐
        ↓ 운영자가 A를 확인
자식 신호가 budget B를 만들고 HEAD = budget B
        ↓
budget B 카드 전송이 Telegram에 닿았지만 결과 기록이 유실(타임아웃)
        → legacy budget B 복사본 = unknown, 물리 B 카드는 실제로 존재할 수 있다
        ↓
funding A 부모 전이의 완료 기록은 아직 미완
```

여기서 3b가 "confirmed 선행자 A를 입양해 B 카드로 edit한다"를 적용하면
Telegram에는 **같은 활성 요청 B의 실행 가능한 물리 표현이 둘** 생길 수 있다 —
존재할지 모르는 원래 B 카드와, B 액션을 달게 된 A 메시지. request_id/head/claim
fencing이 **금융 중복 효과**는 막지만, 그것은 두 번째 실행 카드를 만들어도
좋다는 **전달 증거가 아니다.** 「B-1.7」의 "모르는 것을 안다고 취급하지
않는다"가 여기에도 그대로 적용된다.

**과일반화 금지.** 이 규칙의 대상은 **현재 실행 가능한 요청의 unknown 복사본**
이다. 이미 밀려난 선행 요청의 unknown 복사본은 계속 드러내야 할 모호성이지만,
그 낡은 버튼은 request/head 권위가 이미 금융적으로 막고 있으므로 워크플로우의
모든 향후 카드를 영구히 막지 않는다. "워크플로우 어딘가의 unknown 하나가 모든
카드를 영원히 봉인한다"로 읽어서는 안 된다.

**워크플로우 × chat 단위 우선순위:**

1. **이미 존재하는 워크플로우 범위 라이프사이클 상태** — 즉시 이긴다. 한 번
   생기면 legacy 요청 범위 상태는 **다시는 권위로 조회되지 않는다.**
2. **선행자를 고르기 전에 먼저 현재 head 요청의 라이프사이클 증거를 본다.**
   아래 3~5는 *현재 head 요청 자신의* 복사본에 대한 판정이다.
3. **head 요청에 legacy `confirmed` 복사본이 있으면** — 바로 그 물리 메시지를
   입양한다. 새 전송 없음. 이후 갱신은 그 메시지를 edit한다.
4. **head 요청에 legacy `unknown` 복사본이 있으면** — 위 지배 규칙이 걸린다:
   모호성을 워크플로우 범위 카드로 보존·전파하고, 그 활성 요청에 대해 **새
   실행 카드를 보내지 않으며**, **confirmed 선행자를 그 요청의 실행 가능한
   표현으로 edit하지도 않는다.** 기존 버튼 없는 모호성/운영자 안내를 쓰고,
   맹목적 재생 대신 **사람의 확인 / 이후의 명시적 복구**를 요구한다.
5. **head 요청에 legacy `failed` 복사본이 있으면** — 그 복사본에 한해 미전달이
   증명됐다. 기존 chat/청중 규칙에 따라 안전하게 재시도할 수 있다.
6. **head 요청에 라이프사이클 복사본이 전혀 없을 때에만** 입양 로직이
   **confirmed 영속 선행자**를 이어서 edit할 물리 메시지 후보로 고려한다.
   그때도 legacy 증거가 아예 없으면 「B-1.2」가 적용된다 —
   `card_delivery_version == 0`이면 보내지 않고, `== 1`이면 최초 전송이
   허용된다.

**6에서 후보가 여럿이면**, 정본 물리 메시지는 **영속 워크플로우 lineage**로
고른다: 현재 head의 요청 카드를 먼저, 없으면 supersession lineage를 따라 가장
가까운 영속 선행자를. **최신 시각으로 고르지 않는다** — recency는 어느 카드가
이 워크플로우의 현재 진실을 담고 있는지에 대해 아무것도 말해 주지 않으며,
3a-5가 head 소유권을 recency가 아니라 영속 증명으로 결정한 것과 같은 이유다.

**열린 운영 항목:** `unknown`이 걸린 활성 요청의 모호성을 사람이 어떤 절차로
해소하고 정상 실행 가능 상태로 되돌리는지는 저장소에 아직 메커니즘이 없다.
여기서 새 하위 시스템을 발명하지 않는다 — 구현 계획이 다룰 **열린 운영/구현
항목**으로 남긴다. 그때까지의 기본값은 fail-closed다.

정본이 아닌 것으로 판정된 알려진 legacy 카드는 **best-effort edit**로 낡은
버튼을 걷어낼 수 있다. 다만 그 정리의 실패가 금융 워크플로우를 막아서는 안
된다 — 정리는 위생이지 안전 조건이 아니다.

**legacy/version 0 입양에서 청중을 넓히지 않는다.** 새로 설정된 chat에 입양을
빌미로 카드를 보내지 않고, 알려진 legacy 청중/복사본만 보존한다. 반면 새
version 1 워크플로우 카드는 최초 라이프사이클 생성 시점에 **현재 설정된 정상
청중을 pin**할 수 있다(기존 `record_card_audience` 규약).

#### B-1.4 논리 카드 신원 vs 금융 액션 신원

두 신원은 다른 층이며 섞이면 안 된다.

| | 무엇을 식별하나 | 어디에 실리나 |
|---|---|---|
| `funding_workflow_id` | **논리 카드** — 이 달, 이 scope의 카드 하나 | 카드 키·DB. 금융 콜백 payload에는 싣지 않는다 |
| `request_id` | **금융 액션** — 지금 동의하는 구체적 요청 | 모든 금융 콜백 |

모든 금융 콜백은 현재와 개념적으로 동일하게 유지한다: funding
완료/취소 → funding request_id, budget 선택/취소 → budget request_id.

**낡은 요청의 콜백을 현재 head로 라우팅하지 않는다.** head가 B인데 사용자가
요청 A의 낡은 버튼을 눌렀다면, 시스템은 **A를 시도하고** 기존 head/claim
권위에 의해 거절되어야 한다. 그것을 B에 대한 동의로 해석하는 경로는 존재해서는
안 된다 — 사용자는 B를 본 적이 없다.

UI는 활성 요청을 **권위 있는 워크플로우 상태에서 파생**한다.
`active_request_id`나 요청 교체 이력을 Telegram 카드 상태에 **독립적인 금융
권위로 저장하지 않는다.**

#### B-1.5 권위 상태 → 사용자에게 보이는 월간 단계

「B」의 `📥 → ⏳ → 💰 → ✅` 4단 선형 모델은 지나친 단순화다. 실제 권위 상태는
head phase · 열린 claim · 완료된 completed · 미완 전이의 조합이고, 카드는 그
조합의 **진실된 투영**이어야 한다.

| 권위 상태 | 사용자에게 보이는 단계 | 금융 버튼 |
|---|---|---|
| funding head, claim 없음 | `📥 입금이 필요해요` | 입금 완료 / 취소 |
| funding 확인 claim 열림 | `⏳ 입금을 확인하고 있어요` | 없음 |
| funding 취소 claim 열림 | `⏳ 취소를 처리하고 있어요` | 없음 |
| budget head **이고** 선행 funding 전이가 영속적으로 완료됨 | `💰 이번 달 예산을 선택해 주세요` | 최소 / 추천 / 전액 / 취소 |
| budget 확인 claim 열림 | `⏳ 예산을 적용하고 있어요` | 없음 |
| budget 취소 claim 열림 | `⏳ 취소를 처리하고 있어요` | 없음 |
| funding 취소 완료 | 입금 취소됨을 사실대로 | 없음 |
| budget 취소 완료 | 예산 취소됨을 사실대로 | 없음 |
| budget 확인 완료 | `✅ 이번 달 예산을 확정했어요` | 없음. 선택 금액은 **완료된 claim의 입력값 그대로** 표시할 수 있다 |
| funding 확인 완료 + budget 후속자 없음 | `✅ 자금 확인을 마쳤어요` | 없음. **월간 예산을 골랐다고 말하지 않는다** |

**미완/정체 전이는 두 번째 금융 상태 머신이 아니라 attention 오버레이로
표현한다.** 정당한 후속자가 이미 head인데 선행 전이의 완료 기록이 아직 미완일
수 있다(3a가 명시적으로 허용하는 상태다). 그 경우 카드는 "마무리 중 ·
확인이 필요해요"를 보일 수 있지만, **선행 전이의 완료가 영속화되기 전에는
후속자의 금융 버튼을 노출하지 않는다.**

> head가 budget으로 옮겨갔다 ≠ budget 버튼을 내놓아도 안전하다.

budget 액션은 **선행 funding 전이가 영속적으로 완료된 뒤에만** 실행 가능해진다.

**이것은 렌더러 관례가 아니라 액션 admission 불변식이다.** 버튼을 숨기는 것은
안전 경계가 아니다 — 낡은/legacy Telegram 메시지에는 새 렌더러가 감춘 budget
콜백이 그대로 남아 있을 수 있고, 사용자는 그것을 누를 수 있다. 따라서:

> **선행 전이가 아직 영속적으로 완료되지 않은 후속 요청의 금융 콜백은, 새
> 금융 claim을 얻거나 전이에 진입하기 전에 fail-closed로 거절되어야 한다.**

요구되는 순서:

```
후속자 콜백
   ↓ 기존 마이그레이션 / head / request 권위 검증
   ↓ 필요한 선행 전이의 영속적 완료 검증   ← 3b가 추가하는 admission 전제
   ↓ 그때에만 후속 금융 전이 진입이 허용된다
```

특히 이 조합에서 중요하다: **funding A 미완 + budget B가 이미 head + legacy B
버튼 클릭.** request와 head는 유효한데 선행 완료 전제는 아직 아니다 — 이때
콜백은 A가 영속적으로 완료될 때까지 거절된다.

이것은 `funding_workflow_claim`의 재설계도, 3a head/CAS의 재정의도 아니다.
기존 금융 권위 **위에 얹히는 3b의 admission 전제**다. 판정을 수행할 함수명과
위치는 구현 계획의 몫이며 여기서 정하지 않는다.

**월간 카드 복구와 금융 복구는 분리된 채로 둔다.**

- 월간 카드 = 현재 워크플로우의 진실을 **보여 준다.**
- 기존 미완 워크플로우 복구 UI = 명시적 [재개]를 **제안한다.**
- 3b의 월간 카드는 4b의 예외 마법사가 **아니다.**

승인도 마찬가지로 분리한다. 월간 카드는 "워크플로우가 투자/승인 단계로
넘어갔다"고 말할 수 있지만, 승인/거절 버튼은 승인 카드에 남는다. 월간 카드를
두 번째 승인 권위로 만들지 않는다.

종결/결과 투영은 현재 워크플로우 권위에서 파생한다 — `funding_workflow_completed`
와 그에 정확히 대응하는 claim attempt. **legacy `contribution_funding_request_ack`
/ `contribution_budget_request_decision`을 3b 라이프사이클 권위로 쓰지 않는다.**
그것들은 롤백 호환 투영으로 남아 있을 뿐이다(`docs/rollback_and_upgrade_3a.md`
「1. 권위 있는 상태 모델」).

#### B-1.6 파생 read model과 operator 스윕

3b는 월간 카드의 **read model 경계**를 정의한다. 개념적으로
`FundingWorkflowCardModel`에 해당하는 **일회성(ephemeral) 객체**를 다음에서
조립한다:

- `funding_workflow_head`
- 활성 요청 payload
- `funding_workflow_superseded`
- `funding_workflow_claim`
- `funding_workflow_completed`
- 미완 워크플로우 정보
- Telegram 라이프사이클/전달 투영

담을 수 있는 파생 표현 사실: 워크플로우 id·scope·월, 렌더링 대상 phase/요청,
사용자에게 보이는 단계, attention 상태, 금융 액션 렌더 가능 여부, 영속적으로
알려진 선택 예산, 전달 세대, legacy 입양 후보.

**그러나 이것은 파생 read model이지 영속 금융 권위가 아니다.** 재시작 후
권위 있는 영속 이벤트만으로 다시 조립할 수 있어야 한다. 조립 결과를 금융
판단의 근거로 저장하는 순간 head의 두 번째 정의가 생긴다.

**같은 투영 로직을 두 진입점이 공유한다**: 주기적 Telegram Operator 스윕,
그리고 운영자 콜백/자식 전이 직후의 즉시 갱신. 두 경로가 다른 로직을 쓰면
"카드가 콜백 직후엔 맞다가 다음 스윕에 틀려지는" 상태가 생긴다.

월간 카드 스윕이 **할 수 있는 것**: 권위 상태 읽기, 모델 파생, 입양, 안전할
때의 최초 전송, 갱신/edit, 전달 모호성·실패의 에스컬레이션.

월간 카드 스윕이 **하면 안 되는 것**: `claim_workflow_attempt`, 자식 신호
실행, 현금흐름 기록, `complete_workflow`, 미완 금융 전이의 자동 재개,
워크플로우 head 수리. 이 책임들은 기존 수렴 sweep과 미완 워크플로우 복구가
계속 소유한다. **스윕은 렌더러이지 실행자가 아니다.**

워크플로우 단위로 실패를 격리한다 — 한 워크플로우가 망가져도 나머지
워크플로우 카드의 전달·갱신이 멈추지 않아야 한다.

기존 poll 루프 스윕들을 미관을 이유로 광범위하게 재정렬/리팩터링하지 않는다.
증명된 안전 제약이 요구하는 **최소 순서 변경**만 한다.

#### B-1.7 전달 실패 의미론

정본은 **intent 우선 · 모호성 보존 · 무재전송(no blind replay)** 이다.

**최초 전송**

- Telegram이 미전달을 명시적으로 증명(`ok=false`) → `failed` → 재시도 허용.
- 타임아웃 / 연결 끊김 / 수락 이후일 수 있는 해석 불가 응답 → `unknown`.
- **`unknown`은 절대 자동 재전송하지 않는다.** 버튼 없는 모호성 안내를 쓰고,
  `telegram_ui` 헬스가 unknown 복사본을 반영한다.

이전 개정의 "모호한 카드 재전송" 및 "at-least-once + 중복 카드 정리" 문구는
이 정책과 충돌하므로 철회한다. 3b가 승인 카드의 기존 unknown 동작을 약화하는
일도 없다.

**edit 동작은 "거부되면 무조건 새 전송"보다 정밀해야 한다.** Telegram이 돌려준
거부 정보를 분류 가능한 형태로 보존해 최소 다음을 구분한다:

| 증거 | 판정 | 행동 |
|---|---|---|
| 사실상 이미 원하는 상태 (`message is not modified`) | 수렴 | confirmed로 수렴. 대체 전송 없음 |
| 물리 대상의 부재가 증명됨 (`message to edit not found` 또는 동급의 강한 증거) | 대상 소실 | 대체 전송 허용 |
| 이유 불명의 명시적 edit 거부 (구 메시지가 살아 있을 수 있음) | 불명 | **edit가 실패했다는 이유만으로 새 실행 카드를 만들지 않는다.** 실패/재시도/폴백 의미론은 유지하되 대체하지 않는다 |
| edit 중 전송 모호 | 적용됐을 수 있음 | unknown으로 표시/유지. 이후 자동 전송·edit 재생 없음 |

이 분류가 성립하려면 `TelegramApiRejected`가 지금처럼 메서드·error_code·
description을 버려서는 안 된다. 따라서 3b는 `bot.py`의 최소 변경을 허용한다
(「아키텍처」의 단서 참조).

**UI 전달 실패가 금융 진실을 되돌리지 않는다.** 예산 선택이 금융적으로
완료됐는데 종결 카드 edit가 unknown이 되면, 금융은 완료로 남고 UI가 모호해질
뿐이다.

**단, 기존의 더 강한 handoff 규칙은 그대로 유지한다.** 어떤 전이의 완료가
후속자/운영자 handoff 전달을 영속적으로 셈하는 것을 조건으로 삼고 있는 곳
(handlers의 요청 카드 전달이 `sent`/`skipped`가 아니면 워크플로우 완료를
거부하고 `funding_request_card_undelivered`를 남기는 경로)은 fail-closed로
남는다. 3b 카드를 매끄럽게 만들려고 이 불변식을 약화하지 않는다.

#### B-1.8 CLI 실행 카드 전송자 은퇴

`_send_signal_request_notifications`,
`_send_signal_funding_request_notifications`,
`_send_signal_budget_request_notifications`의 **실행 카드 네트워크 전송자
역할**을 은퇴시킨다.

3b 이후 CLI 보고는 두 가지를 구분해야 한다:

> **영속 요청의 존재** ≠ **Telegram 전달 상태**

`daily-signal-approval`은 하루를 **영속화된 signal package의 요청**으로
분류한다. Telegram 전달 성공 여부로 분류하지 않는다. `action_required == false`
일 때 영속 signal package를 본다:

- funding 요청이 있으면 → 조용한 날이 아니다
- budget 요청이 있으면 → 조용한 날이 아니다
- 둘 다 없으면 → 조용한 날 / no-action 경로

`SignalRunSummary`가 Telegram 전달 권위가 될 필요는 없다. **CLI가 더 이상
소유하지 않는 전달 시도를 근거로 `request_delivery_failed`를 내지 않는다.**
Telegram 전달의 confirmed/failed/unknown/재시도/헬스는 전적으로 Telegram
Operator/lifecycle의 것이다.

진실된 운영 보고는 유지한다:

- `funding_required` = 영속 funding 요청이 존재하고 운영자 조치가 필요하다
- `budget_required` = 영속 budget 요청이 존재하고 운영자 조치가 필요하다

이 상태들은 **Telegram 전달을 약속하지 않는다.** 그리고 한 signal run에
funding과 budget 요청이 **둘 다** 있으면 둘 다 보고한다 — 현재 코드는 budget을
보고한 뒤 반환해 funding을 가린다(cli.py의 `budget_sent` 분기). 두 진실 중
하나를 감추는 것은 조용한 날 오보와 같은 종류의 거짓말이다.

기존 **조용한 날 정보성 알림은 CLI에 남겨도 된다.** 실행 가능하지 않고 자체
at-most-once 의미론을 가진다. 3b를 빌미로 모든 Telegram 정보성 메시지를
operator로 옮기지 않는다.

#### B-1.9 마이그레이션 게이트 stand-down

3b는 3a의 기존 마이그레이션 게이트를 **소비한다.** `MIGRATING` 또는 `INVALID`
동안:

- 새 실행 funding/budget 카드를 전송하지 않는다
- 새 금융 액션을 노출하게 될 갱신을 하지 않는다
- 콜백은 기존 마이그레이션 게이트를 통해 계속 fail-closed로 거절된다
- 3b는 마이그레이션 상태·워크플로우 head·legacy 이력을 **수리하지 않는다**

3b를 위한 새 마이그레이션 권위를 만들지 않는다. 판정은
`state/migration_state.py`가 단독으로 소유한다.

#### B-1.10 3b의 범위

**IN (3b가 한다)**

- 통합 월간 워크플로우 카드 렌더러
- 워크플로우 → UI read model 투영
- 워크플로우 범위 카드 신원
- 요청 범위 금융 콜백
- Telegram Operator 단독 실행 카드 전달 소유
- 전달 세대 절단(`card_delivery_version`)
- legacy 요청 카드 입양
- unknown/무재전송 정책의 정본화
- 월간 워크플로우 카드 스윕
- 같은 투영을 쓰는 콜백 직후 즉시 갱신
- 진실된 단계·종결 상태·attention 오버레이
- 선행 전이 완료를 전제로 하는 **후속자 금융 콜백 admission 게이트**
  (기존 금융 권위 위에 얹히는 3b 전제 — 「B-1.5」)
- 증거 기반 edit 대체
- CLI 실행 요청 카드 전송자 은퇴
- 마이그레이션 게이트 stand-down
- 위에 해당하는 렌더러·라이프사이클·CLI 테스트

**OUT (3b가 하지 않는다)**

- funding head/CAS 재설계
- claim/fencing 재설계
- child lineage 재설계
- 자동 금융 복구
- legacy dual-write 제거
- 롤백 preflight R4 제거
- 단계 3a-6 (현재 단계가 아니다. legacy dual-write와 롤백 호환은 **의도적으로**
  남아 있다)
- 운영/VPS 실거래 사고 수습
- 단계 4b 예외 마법사
- 단계 5 전역 Telegram 정리
- 무관한 광범위 리팩터링
- 필요가 증명되지 않은 워크플로우 토큰 레지스트리 의무화

### C. 예외 카드 (가이드형 마법사)

공통 3단 구성: **무슨 일인지(일상 언어) → 지금 선택할 수 있는 것(버튼 2~4개) →
⚙️ 자세한 사유 보기**.

| 상황 | 첫 문장 | 선택지 버튼 |
|---|---|---|
| **배치 부분 집행** | "주문이 일부만 처리됐어요" | [지금 가격으로 다시 계산] [이번은 건너뛰기] [🔍 자세히] |
| 미체결/용량 부족 (단건) | "주문이 체결되지 않았어요" | [최대 n주로 재시도] [직접 수량 입력] [오늘은 건너뛰기] |
| cash drift 감지 | "계좌에 설명되지 않은 돈의 변화가 있어요" | [배당금] [이자] [세금] [입출금] [기타·나중에] |
| 미체결 정산 경고 | "체결 내역이 장부와 맞지 않아요" | [지금 다시 확인] [⚙️ 자세한 사유] (신규 주문 차단 중임을 명시) |
| 워크플로우 복구 | "이전 작업이 중단된 상태예요" | [자동 복구 시도] [증권사 앱 확인 후 승인] [⚙️ 자세히] |
| 안전 정지(halt) | "시스템이 안전을 위해 멈췄어요" | [점검 후 재개하기] [⚙️ 원인 보기] |

**위험 액션 확인 단계**: 재주문 승인, 복구 실행, 일시중지, 긴급정지는 버튼을
누르면 카드가 "정말 실행할까요? — 실행되는 내용: ..." 확인 뷰로 바뀌고
[실행] [취소]를 다시 누른다. 현재 pause/kill_switch의 confirm 패턴을 전체 위험
액션으로 일반화한다.

#### C-1. 배치 부분 집행 카드 (개정: 2026-08-15)

**왜 단건 카드로는 부족한가.** 한 승인은 주문 여러 건을 한 묶음으로 갖고,
로테이션은 **먼저 팔고 그 돈으로 산다.** 그래서 중간에 멈추면 "주문 하나가
체결되지 않은" 상태가 아니라 **"팔기는 했는데 사지를 못한"** 상태가 된다.
주문별 카드 5장을 늘어놓으면 운영자는 그 사실을 스스로 조립해야 한다.

실제로 두 번 일어났다. 2026-08-12 US run은 `signal_approval_completed`에
`approval_status='approved'`와 `orders_failed=1`을 함께 기록했고, 2026-08-11
크레센도 US 건은 매도 2건을 낸 직후 writer lock 타임아웃으로 멈춰 매수 3건을
내지 못했다 — 매도 1건 체결, 1건 미체결 취소, 매수 3건 미발주. 나흘간
아무도 닫지 못했다. `ui/approval_stage.py`의 주석이 그 이유를 이미 적어
두었다: 실패한 주문은 "닫을 방법이 단계 4 전에는 없는 과거의 사실"이라
⚠️가 영구히 걸린다.

**주문 상태를 증거로 분류한다.** 3a-1·3a-3이 세운 증거 사슬을 그대로 쓴다 —
추측하지 않는다.

| `live_order_submit_intent` | `live_order_result` | 체결량 | 분류 | 재주문 |
|---|---|---|---|---|
| 없음 | — | — | **미발주** | 안전 |
| 있음 | 없음 | — | **제출 불명** | **금지** |
| 있음 | 있음 | > 0 | **체결** (전량/부분) | 남은 수량만 |
| 있음 | 있음 | 0, 주문 종료됨 | **미체결 취소** | 안전 |
| 있음 | 있음 | 0, 주문 열림 | **아직 열림** | 취소·수정 먼저 |

**제출 불명이 하나라도 있으면 재주문 버튼을 내리지 않는다.** intent는 있는데
결과가 없다는 것은 브로커에 닿았는지 알 수 없다는 뜻이고, 그 상태에서
재주문하면 중복 주문이 된다. 이 경우 카드는 "증권사 앱에서 먼저 확인해
주세요"로 라우팅한다. 카드 전달의 `unknown`을 재전송하지 않는 것과 같은
규칙이며, 근거도 같다 — **모르는 것을 안다고 취급하지 않는다.**

**기본 동작은 "그대로 재시도"가 아니라 "지금 가격으로 다시 계산"이다.**
승인 시점의 지정가는 시간이 지나면 무의미해진다(08-11 건은 나흘). 그대로
재발주하면 체결되지 않거나, 더 나쁘게는 의도와 다른 가격에 체결된다.
[지금 가격으로 다시 계산]은 **미발주·미체결 취소분만을 범위로 새 signal
run을 만들고, 그 결과를 새 승인 카드로 띄운다** — 즉 정상 승인 흐름으로
되돌린다. 이미 체결된 부분은 다시 사지 않는다.

접힌 뷰 예시:

```
⚠️ 주문이 일부만 처리됐어요

크레센도 US 전략 · 토스 계좌 · 5건 중 1건 완료

✅ 매도 TIP 23주 — 전량 체결
⛔ 매도 SSO 20주 — 체결되지 않아 취소됐어요
⚪ 매수 PDBC·SPY·BIL 3건 — 주문을 내지 못했어요

판 돈이 현금으로 남아 있어요. 승인하신 지 4일 지나 그때 가격은
지금과 달라요.

[지금 가격으로 다시 계산] [이번은 건너뛰기] [🔍 자세히]
```

`🔍 자세히`에 두는 것: 종목별 주문·체결 수량과 지정가 전체 자릿수,
브로커 주문 ID, approval_id, 중단 사유 원문.

**종결(settle)은 이 카드의 본체다.** 두 버튼 모두 승인을 **사실대로**
종결한다 — 집행 결과(제출 n건 / 체결 m건 / 미발주 k건)와 운영자가 고른
처리를 함께 기록하고, `telegram_approval_resolution_completed`를 남겨
승인을 닫는다. 다만 **정상 집행과 구분 가능해야 한다**: 종결 이벤트에
`settled_by=operator`와 집행 내역을 담아, 나중에 이 이벤트를 읽는 성과
집계·preflight·sweep이 "시스템이 깨끗이 집행한 건"으로 오인하지 않게 한다.

**종결 메커니즘은 카드보다 먼저 나온다.** 위 분류와 종결 기록은 카드가
없어도 성립하고, 지금 닫지 못하는 승인이 이미 두 건 있다. 따라서 단계 4는
둘로 쪼갠다 — **4a: 분류 + 종결 (CLI로 노출)**, **4b: 그 위의 카드 UX.**
4a가 만든 것을 4b가 그대로 쓴다.

### D. 조회 카드

명령어로 호출하는 스냅샷 카드.

## 명령어 체계 (20개 → 5개)

| 명령어 | 한글 설명 | 내용 |
|---|---|---|
| `/today` | 오늘의 투자 현황 | 활성 데일리 카드 재전송 (없으면 "오늘은 조용해요" + 마지막 실행 요약) |
| `/portfolio` | 내 자산 | 계좌별 평가금액·현금·보유 종목 |
| `/system` | 시스템 상태 | 헬스체크·안전상태·데이터 신선도 통합. 여기서만 [⏸ 일시중지] [🛑 긴급정지] 노출 |
| `/history` | 지난 기록 | 최근 주문·승인 이력 |
| `/help` | 도움말 | 명령어 안내 + "이런 알림이 올 수 있어요" 설명 |

- `set_my_commands`로 이 5개만 한글 설명과 함께 등록한다.
- 기존 20개 명령어는 동작을 유지하되 메뉴에서 제외한다 (하위 호환).
- `/cash_drift`, `/retry_order` 등 상황 의존 기능은 해당 예외 카드의 버튼으로만
  진입한다.

## 🔍 자세히 (펼치기/접기)

- 카드마다 `[🔍 자세히]` 버튼 → 같은 메시지를 펼친 뷰로 edit (종목코드, 지정가
  전체 자릿수, approval_id, run_id, 계좌 ID 등), 버튼은 `[접기]`로 변경.
- 새 메시지를 만들지 않아 채팅방이 지저분해지지 않는다.
- 접기/펼치기 콜백은 기존 `operator:` prefix 체계와 구분되는 `ui:` 계열을
  쓰고, **카드 유형에 맞는 유계(bounded) 콜백 신원**을 실어 Telegram의 64바이트
  제한 안에 머문다. **논리 card_key를 언제나 그대로 담는다는 뜻이 아니다**
  (개정 16차 교정): 월간 자금 카드의 `funding_workflow_id`는 canonical 직렬화된
  원시 scope 전체 + 월이라 길 수 있으므로 **callback_data에 넣지 않는다.**
- 어떤 유계 신원을 쓸지는 구현 계획의 몫이다 — 이미 존재하는 유계 요청/메시지/
  카드 식별자 재사용, `(chat_id, message_id)` 기준 조회, 또는 필요가 **증명된**
  경우에 한한 유계 토큰 등. 지금 하나를 고르지 않는다. 필요가 증명되지 않은
  워크플로우 토큰 레지스트리는 도입하지 않는다(「B-1.4」).
- 어느 경우에도 이 표시/조회용 식별자는 **금융 권위가 되지 않는다.** 금융
  액션은 `request_id`에 바인딩되고, `funding_workflow_id`는 논리 월간 카드·DB
  신원으로 남는다.

## 언어·포맷 규칙 (catalog.py의 헌법)

1. **첫 줄 = 결론.** 첫 줄만 읽어도 "무슨 일 + 내가 뭘 해야 하는지"가 파악된다.
2. **전부 한글.** 기술 식별자(ID, 종목코드)는 펼친 뷰에만. 예외: 티커·전략명 등
   고유명사.
3. **금액**: 원화 `124만원`, `71.2만원` (1만 미만은 `8,500원`). 달러 `$1,240.50`.
   펼친 뷰에서는 전체 자릿수.
4. **시간**: 운영자 타임존(KST) 기준 `밤 11시 30분`, `오전 9시 10분`.
   ISO 타임스탬프는 펼친 뷰에만.
5. **이모지는 두 용도만.** (a) 첫 줄 카드 유형 아이콘 1개 (📩 승인 요청 /
   📥 입금 / ⚠️ 예외 / 📊 조회 등), (b) 상태 신호등: ✅ 완료 / 🔵 진행 중 /
   ⏳ 응답 대기 / ⚠️ 확인 필요 / 🛑 정지 / 🟢 매수 / 🔴 매도. 그 외 장식용 금지.
6. **에러도 일상 언어로.** 예외 원문은 첫 화면에 노출하지 않고 "왜 안 됐는지 +
   다음에 뭘 할 수 있는지"로 번역. 원문은 ⚙️ 자세히에.

## 데이터 흐름

1. 카드 상태는 `telegram_ui_card` system event로 기록한다. 기존 state store
   사용, 새 저장소 없음.

   **상태 키는 `(card_key, chat_id)`다.** `card_key`는 논리 카드이고, 전달
   복사본은 chat별로 하나씩 존재한다. `card_key` 하나가 `message_id` 하나를
   들고 있는 구조는 금지한다 — 지금 운영 chat이 하나뿐이라 그렇게 접고 싶어지지만,
   접는 순간 두 번째 chat이 추가될 때 카드가 한 곳에서만 갱신되고 나머지는
   영원히 낡은 화면으로 남는다. 되돌리려면 이미 쌓인 기록을 마이그레이션해야 한다.

   **전송 전에 intent를 먼저 기록한다.** 순서는
   `intent → 텔레그램 호출 → result`(또는 명시 거부 시 `failure`)이며,
   `message_id`는 텔레그램이 응답해야 존재하므로 result에만 있다.

   **이벤트의 유일성 키는 `(card_key, chat_id, 단계)`가 아니다** (개정 15차
   교정). 그 셋만으로 키를 만들면 같은 단계의 두 번째 시도가 쓰기 자체를
   거부당한다 — `system_events`의 `duplicate_key`에는 UNIQUE 제약이 있다.
   현 구현은 시도마다 **`operation_id`**를 발급해
   `telegram-ui-card:<phase>:<card_key>:<chat_id>:<stage>:<operation_id>`로
   기록하므로, 재시도는 각자의 intent/result/failure를 남길 수 있다. 반면
   **투영의 키는 `(card_key, chat_id)`**이며, 이벤트를 오래된 것부터 접어
   현재 복사본 하나를 만든다(`ui/card_state.py`의 `resolve_card_copies`).
   즉 "시도의 신원"과 "현재 복사본의 신원"은 다른 층이다.

   이 순서가 아니면 — 즉 전송 후에만 기록하면 — `sendMessage`가 성공하고
   이벤트를 쓰기 전에 프로세스가 죽었을 때 **텔레그램에는 카드가 있는데 우리
   기록에는 없는** 상태가 된다. sweep은 그 카드를 영원히 갱신하지 않고, 재시도는
   중복 카드를 만든다. 이는 2026-08-11·08-12 주문 경로에서 확인한 ambiguous
   submit과 정확히 같은 결함 모양이다: 외부 시스템에 부작용을 일으킨 뒤 기록을
   나중에 쓰는 것. 대상만 브로커에서 텔레그램으로 바뀐다.

   intent만 있고 result가 없는 카드는 **관측 가능한 상태**여야 한다. 그것을
   찾아 자가 치유하는 것은 단계 3a-3의 몫이지만, **그 기록을 만드는 것은 단계 2**다.
   나중에 끼워 넣으면 이미 쌓인 intent 없는 기록을 마이그레이션해야 하고,
   그만큼 3a-3이 커진다.
   `card_key`는 카드 유형별 원천 ID로 정한다:
   승인 카드 = `approval:<approval_id>` (유일한 액션 카드),
   데일리 요약 카드 = `daily:<signal_run_id>` (읽기 전용 집계),
   월간 자금 카드 = `funding_workflow_id`
   (`funding:<canonical 직렬화 원시 scope>:<YYYY-MM>` — 유니코드 정규화 없음;
   이벤트 payload에 전체 키를 그대로 쓴다. **금융 콜백은 이 키를 싣지 않고
   request_id를 싣는다** — 「B-1.4」),
   예외 카드 = `<유형>:<원천 event id>`.

   3b 이전에 쓰이던 요청 범위 키 `funding-request:<request_id>` /
   `budget-request:<request_id>`는 legacy 세대의 카드 신원이다. 3b는 이를
   워크플로우 범위 카드로 **입양**하며, 입양은 새 전송이 아니다 —
   「B-1.3」.
2. poll 루프 sweep에 `_sweep_lifecycle_cards()` 추가: 활성 카드의 원천 이벤트
   (승인 ack, 주문 상태, 정산 결과)를 조회해 단계 변화 시 re-render →
   `edit_message_text`.
3. 렌더 결과가 직전과 같으면(해시 비교) edit 생략 — "message is not modified"
   에러와 불필요한 API 호출 방지.

## 에러 처리

- **edit 실패의 처리는 증거로 나눈다 (개정 15차 교정).** 이전 개정의
  "edit 실패 → 무조건 새 메시지"는 철회한다. edit가 거부됐다는 사실만으로
  새 카드를 보내면, 원본이 멀쩡히 살아 있는 경우 버튼 달린 카드가 두 장
  된다. 3b의 요구는 Telegram이 돌려준 거부 정보를 **분류 가능한 형태로
  보존**하는 것이다 — 「B-1.7」의 네 갈래(이미 원하는 상태 / 대상 부재가
  증명됨 / 이유 불명 거부 / 전송 중 모호).
- **UI 렌더링 실패는 거래 로직을 막지 않지만, 조용히 사라지지도 않는다**:
  카드 갱신 실패는 audit 이벤트를 남기고 다음 sweep에서 재시도하되,
  같은 카드가 **연속 3회 이상 렌더/edit에 실패하면** 렌더러를 거치지 않는
  고정 템플릿의 일반 텍스트 알림(카드 유형·단계·원천 ID만 포함)을 별도
  메시지로 발송하고, `telegram_ui` 헬스체크를 degraded로 전환한다.
  운영자가 진행·실패 상태를 놓친 채 거래가 계속되는 상황을 차단한다.
- 승인/주문 처리는 UI와 독립적으로 동작.
- **4,096자 제한**: 펼친 뷰에서 주문이 많으면 종목 목록 페이지네이션
  ([다음 5건] 버튼). 기본 뷰는 항상 한 화면.
- **중복 방지**: 카드 생성·노옵 알림은 `duplicate_key`로 멱등 처리 (기존 컨벤션).

## 테스트 전략

- **cards.py 스냅샷 테스트**: 순수 함수이므로 상태별 렌더 결과(텍스트+버튼)를
  스냅샷으로 고정. 문구 수정 시 스냅샷 diff로 리뷰.
- **lifecycle.py**: 기존 fake Telegram client 패턴으로 "단계 전이 → edit 호출"
  검증. 다음 시나리오를 반드시 포함한다:
  - 한 signal run의 복수 승인 그룹 혼합 상태 (완료+대기, 완료+실패)
  - 순서가 뒤바뀐 이벤트 도착 (주문 체결 이벤트가 승인 ack보다 먼저 조회됨)
  - 운영자 프로세스 재시작 후 활성 카드 복원
  - funding 요청 교체 후 구 요청 버튼의 중복 callback (거절 응답 확인,
    활성 요청에 도달하지 않음)
  - 같은 계좌·같은 달에 sleeve/group/currency가 다른 복수 funding scope →
    scope별 독립 카드 생성 (합쳐지지 않음)
  - `account_id`가 없는(null) funding scope의 카드 생성·갱신
  - **crash-boundary (funding)**: `run_signal()` 완료 직전/직후에 프로세스가
    중단된 뒤 재시작 — 신규 signal/요청이 하나만 존재하고, 구 요청 버튼은
    거절되며, child signal run이 이미 있으면 lineage로 조회해 재사용된다
  - **crash-boundary (budget)**: budget decision 저장 직후(refresh/config
    load/`run_signal()` 실패 포함) 중단 — 요청이 종결로 오인되지 않고
    복구 대상으로 잡히며, 저장된 decision 금액으로 재개된다
  - **lineage 조회 유일성**: claim된 워크플로우와 같은 전략·scope의 수동
    run이 동시에 존재해도 child run 조회가 잘못된 run을 연결하지 않는다
  - **head 경쟁**: 같은 scope/month에 pending 요청 두 개가 공존하는 상태
    (중단 후 재생성, 수동 run)에서 head인 요청만 처리되고, 밀려난 요청은
    `superseded`로 종결되며 그 버튼은 거절된다
  - **동시 중복 callback**: 같은 funding 요청의 버튼이 동시에 두 번
    처리되어도 claim에 의해 한 건만 진입하고 나머지는 거절된다
  - **dispatch crash 주입**: consumed 직후 / 각 그룹 pending 이벤트 저장
    전후 / 각 채팅 전송 전후에 중단 후 재개 — dispatch_group_id
    get-or-create에 의해 기존 approval_id·envelope·최초 만료시각이
    재사용되고(일부 그룹만 저장된 상태 포함), **intent 없는 채팅만 발송되고
    ambiguous 채팅은 재발송되지 않으며**(버튼 없는 안내로 에스컬레이션),
    message_id가 알려진 중복 복사본은 무력화 edit로 정리되고,
    message_id가 유실된 복사본은 클릭 시 self-heal(그 메시지가 즉시 최종
    상태로 edit되고 추적 목록에 등록)되며, 승인 callback은 어느 카드에서
    눌러도 한 번만 처리되고, 전부 끝난 뒤에야 dispatch 완료가 기록된다
  - **scope 불일치 방어**: head/claim 이벤트 payload의 원본 scope가 기대
    scope와 다르면(식별자 오염 가정) 처리가 거부된다
  - **scope 직렬화 충돌**: null vs 문자열 `-`, 구분자(`:` 등)를 포함한
    식별자, NFC 등가이지만 코드포인트가 다른 유니코드 문자열로 구성된 서로
    다른 scope 쌍이 서로 다른 funding_workflow_id로 직렬화된다 (정규화를
    하지 않으므로 등가 문자열은 합쳐지지 않는다)
  - **승인 resolution crash-boundary**: ack 저장 직후·주문 해석 도중·완료
    직전 각 지점에서 중단 — ack-only 승인이 종결로 오인되지 않고 sweep이
    기록된 결정으로 재개하며, 주문이 중복 생성되지 않고, 카드는
    `resolution_completed` 전까지 완료로 렌더되지 않는다
  - **head 트랜잭션 crash 주입**: 요청 생성·superseded·head 전환의 원자
    커밋을 중단 지점별로 검증하고, 인위적으로 만든 orphan 요청/dangling
    head가 복구 sweep에서 수렴된다
  - **단일 재개(fencing)**: child 생성 전 각 중단 지점에서 복구 카드
    [재개]를 동시에 두 번 승인해도 attempt CAS에 의해 한 건만 진입하고,
    이전 attempt의 잔여 실행은 상태 커밋이 거부된다
  - **head 교체 TOCTOU**: callback의 head 검증 직후·claim 커밋 직전에
    예약/수동 run이 새 요청과 head를 원자 커밋 — claim precondition이
    불일치를 감지해 구 callback이 거절되고 아무 부작용도 남기지 않는다
  - **child 생성 경쟁**: 이전 attempt가 `run_signal()` 내부에서 지연된
    상태로 재개 attempt가 진입 — get-or-create duplicate_key에 의해
    signal package와 승인 흐름이 원천 요청당 1개만 생성된다
  - 연속 렌더/edit 실패 → 고정 템플릿 fallback 발송 + 헬스 degraded
- **기존 handlers 테스트 유지**: 비즈니스 로직 불변이 원칙이므로 기존 테스트가
  깨지면 로직을 건드렸다는 경고 신호.

### 단계 3b 테스트 범위 (개정 15차)

기존 3a 테스트와 불변식은 그대로 둔다. 아래는 3b가 **추가로** 덮어야 하는
범위이며, 파일 배치와 픽스처 구성은 구현 계획의 몫이다.

**전달 세대 (`card_delivery_version`)**

- 요청 모델에 `card_delivery_version`이 없으면 legacy 세대 `0`으로 읽힌다
- 3b가 새로 만든 funding/budget 요청은 `1`이다
- version 0 + 라이프사이클 상태 없음 → **자동 전송하지 않는다**
- version 1 + 라이프사이클 상태 없음 → 최초 전송이 허용된다

**legacy 요청 카드 입양**

- legacy `confirmed` 요청 카드 → 알려진 message_id를 입양하고 새 전송을 하지
  않으며, 이후 갱신이 그 물리 메시지를 edit한다
- legacy `unknown` 요청 카드 → 워크플로우 카드도 unknown이 되고 재전송하지 않는다
- legacy `failed` 요청 카드 → 재시도가 허용된다
- 알려진 legacy `confirmed` 카드가 여럿 → 정본 선택이 head/lineage로 결정되고
  **최신 시각으로 결정되지 않는다** (단, 이 선택은 head 요청 자신의 복사본이
  없을 때에만 도달한다)
- **현재 활성 요청 unknown 지배 (핵심 시나리오)**: confirmed funding 선행자 A
  + budget B가 현재 head + legacy budget B 전달이 `unknown` + funding A 선행
  완료가 아직 미완인 상태에서 —
  - B의 모호성이 보존된다
  - **A의 confirmed 물리 메시지가 두 번째 실행 가능 B 카드로 승격되지 않는다**
  - **새 실행 가능 B 전송이 일어나지 않는다**
  - A 완료 전에 낡은/존재할 수 있는 B 콜백을 눌러도 **금융 전이 진입 이전에**
    거절된다
  - 선행 완료와 명시적·안전한 운영자 경로에 따른 모호성 해소 이후에야 정상 B
    실행 가능성이 재개된다
- 이미 밀려난 선행 요청의 unknown 복사본은 모호성으로 드러나지만, 워크플로우의
  향후 카드를 영구히 봉인하지는 않는다
- legacy 청중은 새로 설정된 chat으로 확장되지 않는다
- 워크플로우 범위 상태가 한 번 생긴 뒤에는 나중에 도착한 legacy 이벤트가
  권위를 되찾지 못한다

**신원**

- `funding_workflow_id`가 카드 신원이고 콜백은 `request_id`에 바인딩된 채로 남는다
- 낡은 요청의 콜백이 현재 head로 재라우팅되지 않는다 (A를 시도해 거절된다)

**단계 투영과 attention**

- funding 선행자 미완 + budget 후속자가 head → **budget 버튼은 비활성**
- **콜백 admission**: budget B가 유효한 현재 head인데 funding 선행자가 미완이면
  budget 콜백이 거절되고 **새 budget claim·자식 run·현금흐름 등 어떤 금융
  부작용도 남지 않는다** (버튼을 숨기는 것만으로는 검증되지 않는다)
- 선행자 완료 → budget 버튼이 실행 가능해진다
- 취소/확인 종결 문구가 사실대로다 (입금 취소 ≠ 예산 확정)
- budget 후속자 없는 funding 확인 완료가 "예산을 골랐다"고 말하지 않는다
- 승인 handoff는 승인 카드에 남는다 (월간 카드는 승인 권위가 아니다)

**스윕 권한 경계**

- 월간 카드 스윕이 claim / run_signal / 현금흐름 / complete_workflow /
  head 수리 중 **어떤 부작용도 내지 않는다**
- 워크플로우 하나가 malformed여도 다른 워크플로우 카드의 전달·갱신이 멈추지 않는다

**전달 실패 의미론**

- 최초 전송의 명시적 거부(`ok=false`)는 재시도된다
- 최초 전송의 unknown은 재생되지 않는다
- edit 대상 부재가 증명되면 대체 전송이 허용된다
- 대상 소실 증거 없는 edit 거부는 **두 번째 실행 카드를 만들지 않는다**
- edit 중 전송 모호는 재생되지 않는다
- `message is not modified`는 대체 없이 수렴한다
- UI 모호성이 영속 금융 완료를 되돌리지 않는다
- 기존 후속자 handoff 규칙이 적용되는 자리에서는 전달 실패/unknown이
  **여전히 부모 전이를 미완으로 남긴다**

**CLI**

- CLI가 더 이상 funding/budget 실행 카드를 보내지 않는다
- 조용한 날 분류가 **영속 요청의 존재**에서 나온다 (전달 성공 여부가 아니다)
- 한 run에 funding과 budget이 모두 있으면 **둘 다** 보고된다
- no-action 정보성 알림 동작은 그대로 유지된다

**마이그레이션**

- `MIGRATING` / `INVALID`에서는 새 실행 카드 전달과 금융 액션이 차단된다


## 마이그레이션 순서 (단계별 독립 배포, 롤백 규칙은 단계별로 명시)

| 단계 | 내용 | 효과 |
|---|---|---|
| 1 ✅ | `ui/` 모듈 신설 + 승인 카드 교체 + 자세히 토글 + 메뉴 5개 등록 | 가장 자주 보는 메시지 즉시 개선 |
| 2 ✅ | 라이프사이클 카드 매니저 + 승인/데일리 카드 + 노옵 한 줄 + fallback 알림 경로 | 구조 개편의 핵심 |
| 3a-1~3a-5 ✅(엔지니어링) | **정합성 기반 작업 (UI 아님, 접근 A 예외)**: StateStore 원자 커밋 API, 워크플로우 head/CAS, attempt 기반 claim·재개, lineage, dispatch idempotent resume, 승인 결정 2단계 영속화, 수렴 sweep, 업그레이드 backfill · 마이그레이션 상태 머신 · 롤백 preflight | 기존 교체 경합·중단 복구 결함 해소. **GitHub 코드베이스 기준 종결이며 VPS 운영 마이그레이션 수행 여부는 별건이다** |
| 4a ✅ | **배치 집행 결과 분류 + 승인 종결 (CLI)** — 「C-1」 참조. 3a-4·3b보다 **먼저** 갔다 | 닫지 못하는 승인 2건을 닫고, 4b·3a-5의 토대가 된다 |
| 3b ← **다음** | 월간 워크플로우 카드 (`funding_workflow_id` 기반 입금·예산 통합, 3a 위에 구축) — 아키텍처는 「B-1」 확정, 구현 계획 미작성 | 월초 경험 개선 + 실행 카드 전달 소유자 단일화 |
| 4b | 예외 마법사 (배치 부분 집행 카드, 재주문·cash drift·복구·안전 정지) | 장애 대응 경험 개선 |
| 5 | 조회 카드 5종 + 구 명령어 메뉴 숨김 + 구 알림 경로 제거 | 마무리 |

### 3b 선행 조건 (3a-4 최종 리뷰에서 이관, 2026-08-19 — **개정 15차에서 결론 확정**)

3a-4는 handlers의 funding 확인 경로만 건드리기로 한 단계라 아래 둘을 열어
두었다. 3b가 이 카드들을 lifecycle 카드로 대체하므로 그때 함께 닫는다.
**두 항목의 결론은 「B-1」에서 확정됐다**: (1)은 「B-1.1」·「B-1.2」·「B-1.8」
— CLI의 실행 카드 전송자 역할을 은퇴시키고 `card_delivery_version`으로 세대를
절단한다. (2)는 「B-1.7」 — **무재전송이 정본이다.** 아래 서술은 그 결론에
이르게 된 문제 진술로 남겨 둔다.

1. **`cli.py`의 최초 카드 발송이 lifecycle 밖에 있다.**
   `_send_signal_request_notifications`는 raw `client.send_message`를 쓰므로
   card state(`telegram_ui_card_state`)를 남기지 않는다. 그래서 텔레그램 봇의
   `deliver_once`와 서로 멱등하지 않다 — 봇은 사본이 없다고 보고 버튼 달린
   카드를 한 장 더 보낸다. 지금은 예약 실행이 만든 요청과 자식 run이 만든
   요청의 id가 달라 실제로 겹치지 않지만, 보장이 아니라 우연이다.

   **순서 제약**: "전달되지 않은 요청 카드를 자동 재전달하는 sweep"을
   만든다면 **반드시 이 라우팅 이후**여야 한다. 그 전에는 CLI가 이미 보낸
   카드와 한 번도 보내지 않은 카드가 구분되지 않아, 진행 중인 모든 요청에
   중복 카드가 나간다. 같은 문제를 다루는 관용구가 저장소에 둘 있다 —
   `card_delivery_version < 1` 게이트와 `load_migration_cutoff`.

2. **`unknown` 사본 재전송 정책이 스펙 안에서 어긋나 있다.** 개정 6차는
   "Telegram 전송 exactly-once 포기(at-least-once + 중복 카드 정리)"이고
   「단계 2」 항목은 "intent는 있으나 결과가 없는 ambiguous 채팅은
   **재발송한다** — 중복 카드가 생길 수 있음을 수용한다"인데, 「C-1」의
   제출 불명 논의는 반대로 "카드 전달의 unknown을 재전송하지 않는 것과 같은
   규칙"이라며 구현 쪽을 인용한다. 구현은 승인 카드·요청 카드 모두
   재전송하지 않는다.

   **결론 (개정 15차): 무재전송이 정본이다.** 요청 카드의 재전송이 중복
   *결정*을 만들지 않는 것은 맞지만(head+claim이 두 번째 탭을 거부한다),
   그것은 안전의 증명이지 좋음의 증명이 아니다. 운영자에게는 살아 있는
   버튼이 두 개로 보이고, 두 번째 탭이 거부되는 모습은 중복이 아니라 고장으로
   읽힌다. 승인 카드에는 그 논거조차 적용되지 않는다. 따라서 승인 카드·요청
   카드 모두 `unknown`을 재전송하지 않으며 — 이미 구현이 그렇게 동작한다 —
   이 문서의 반대 문구(개정 6차의 `at-least-once + 중복 카드 정리`, 단계 2
   항목의 `ambiguous 채팅은 재발송한다`)는 철회했다. 「B-1.7」이 정본이다.

**단계 2의 안전망**: 개별 주문 알림·미체결 경고·halt·정산 불일치 알림의
**기존 전송 경로는 단계 2에서 제거하지 않고 카드와 병행 유지**한다.
카드 전달 성공률이 실운영에서 검증된 후(아래 승인 조건 충족) 단계 5에서
구 경로를 제거한다.

**단계 3a의 롤백은 조건부로만 안전하다 (개정 15차 교정).** 이전 개정은
"단계 3a는 roll-forward-only"라고 단정했지만, 3a-5 이후의 설계는 그렇지 않다.
정확한 진술은 이렇다:

- 단계 1·2·3b·4·5는 UI 전용이므로 코드 롤백만으로 되돌릴 수 있다.
- 단계 3a는 새 불변식(claim, 2단계 승인 종결, dispatch manifest)을 남기므로
  **아무 때나 되돌릴 수 없다.** 기본 대응은 여전히 수정 배포(roll-forward)다.
- 그러나 **롤백 창이 열려 있고**(legacy 종결 이벤트 dual-write와 롤백 호환
  투영이 아직 제거되지 않았고), 정본 3a-5 런북의 **재부팅 안전(reboot-safe)
  quiesce 장벽**과 **읽기 전용 롤백 preflight**가 **둘 다** 허용하는 경우에만
  롤백은 지원된다. 3a-5는 이를 위해 preflight(R0–R4)와 장벽 검증
  (`maestro quiesce-status`)을 실제로 구현했다.
- preflight는 **검사기지 복구 도구가 아니다.** 위험 상태를 발견하면 고치지
  않고 거절한다.

**이 UX 스펙은 롤백 운영 절차를 복제하지 않는다.** systemd 명령 순서, 정지·
disable 대상 유닛 목록, 재부팅 안전 조건 검사, R0–R4의 정확한 판정, 롤백 후
재업그레이드 경로, 롤백 창을 닫는 조건의 **유일한 운영 정본은
`docs/rollback_and_upgrade_3a.md`다.** 소유 관계:

- 운영 절차: `docs/rollback_and_upgrade_3a.md`
- 설계/구현 계획: `docs/superpowers/plans/2026-08-24-upgrade-backfill-rollback-preflight-v2.md`
- 코드: `src/maestro/state/rollback_preflight.py`, `src/maestro/ops/quiesce.py`,
  `src/maestro/state/migration_state.py`

**3b는 이 롤백 창을 유지한다.** legacy dual-write 제거도, R4 검사 제거도 3b의
범위가 아니다(「B-1.10」). 그것은 단계 3a-6의 일이고, 3a-6은 현재 단계가 아니다.

**3a 업그레이드 backfill** (아래는 3a-5로 **구현 완료된** 설계다. 실제 코드는
`state/migration_state.py`·`state/upgrade_backfill.py`이고, 절차의 정본은
`docs/rollback_and_upgrade_3a.md`다 — 여기 남긴 서술은 왜 그렇게 만들었는지의
근거다): 새 불변식은 3a 이전에 쌓인 상태를 모르므로,
backfill 없이 켜면 두 가지 오판이 발생한다 — (a) 기존 pending
funding/budget 요청은 head가 없어 수렴 sweep이 orphan으로 오판해
supersede할 수 있고, (b) 기존에 정상 완료된 승인 ack는
`resolution_completed`가 없어 sweep이 미완으로 오판해 재집행할 수 있다.

**업그레이드도 롤백과 같은 quiesce 장벽을 요구한다**: state store에는
telegram-operator와 per-market signal 타이머 등 여러 writer가 있으므로,
장벽 없이 backfill하면 그 사이 구버전 writer가 head 없는 legacy 요청을
새로 만들거나(경계 오염), 다른 프로세스가 watermark만 보고 sweep을
켤 수 있다. 절차:

1. **quiesce**: 롤백 절차와 동일하게 state store에 쓰는 모든 systemd
   unit을 정지하고 DB 배타 lock을 잡는다. backfill은 이 장벽 아래에서만
   실행된다.
2. **`migration_started` 기록 + immutable cutoff**: started 이벤트에
   cutoff(그 시점 system_events의 최대 id)를 저장한다. 모든 backfill
   판정은 이 cutoff를 기준으로 하며, **재기동 시 미완 마이그레이션이
   발견되면 새 watermark를 만들지 않고 기존 started의 cutoff로
   재개한다.**
3. **legacy pending 요청의 head 초기화**: cutoff 이전 pending
   funding/budget 요청을 workflow(scope+month)별로 묶어 v1 head를
   원자적으로 생성한다. 같은 workflow에 pending이 2건 이상인 모호한
   그룹은 **자동 supersede하지 않고** 운영자 검토 카드로 격리한다.
4. **legacy ack의 완료 판정**: 결정 이벤트에 스키마 버전 필드를 추가해
   신·구 이벤트를 구분한다. cutoff 이전 ack는
   `signal_approval_completed` 이벤트와 주문·실행 기록을 증거로 완료
   여부를 판정해 `resolution_completed`를 backfill하고, 증거가 모호한
   ack는 **자동 재실행하지 않고** 운영자 검토 카드로 격리한다.
5. **`migration_completed` 기록 후에만 재개**: 모든 backfill 쓰기는
   결정적 duplicate_key(workflow_id·approval_id 기반)로 멱등화해 중단
   후 반복 실행이 안전하다. 3~4가 전부 끝난 뒤 `migration_completed`를
   기록하고, **completed 마커가 확인되기 전에는 어떤 프로세스도 신규
   스키마 write·수렴 sweep·funding/budget callback 처리를 시작하지
   않는다** (started만 있으면 기동 시 backfill 재개로 진입). completed
   후에 unit을 재개한다.
6. **업그레이드 테스트**: 실제 구버전 DB 스냅샷(fixture)으로 검증한다 —
   정상 pending 요청이 유지되고, 완료된 승인이 재집행되지 않으며, 모호
   케이스가 격리된다. 실패 주입: backfill 각 단계 사이의 crash 후 재개
   (같은 cutoff 재사용, 결과 불변), 구·신 버전 동시 기동(completed 전
   신규 스키마 write 차단), started 직후 legacy 요청이 생기는 경계
   오염(quiesce가 차단함을 확인).

**각 단계의 배포 승인 조건** (다음 단계로 넘어가기 전 확인):

- 운영자 프로세스 재시작 후 활성 카드가 정상 복원·갱신된다.
- 복수 승인 그룹이 있는 signal run에서 카드가 그룹별로 올바르게 동작한다.
- funding/budget 요청 교체 시 카드가 이어지고 중복 카드가 생기지 않는다.
- 같은 달 복수 funding scope(다른 sleeve/group/currency) 및 account_id=null
  환경에서 카드가 scope별로 분리되고, 구 request_id callback이 다른 scope의
  요청에 도달하지 않는다.
- 요청 전환 중 crash를 주입해도(run_signal 완료 직전/직후, budget decision
  저장 직후) 신규 요청은 하나만 생성되고 구 요청 callback은 거절되며,
  동시 중복 callback은 claim에 의해 한 건만 처리되고, 중단된 워크플로우는
  복구 대상으로 잡혀 재개된다.
- 승인 dispatch 중단(consumed 직후, 그룹 이벤트 전후, 채팅 전송 전후) 후
  재개 시 approval이 중복 생성되지 않고, ambiguous 채팅이 재발송되지 않으며,
  message_id가 알려진 중복 복사본은 정리되고 유실 복사본은 클릭 시
  self-heal되며 승인 callback은 멱등하고,
  같은 scope/month의 병행 pending 요청 경쟁에서 head인 요청만 처리된다.
- claim-only 상태의 워크플로우가 복구 카드 [재개]로 정확히 한 번 재개되고,
  orphan 요청/dangling head가 sweep에서 수렴된다.
- 승인 ack 이후 resolution 실패/중단이 영구 유실로 남지 않는다: sweep이
  기록된 결정으로 재개하고, 주문은 중복 생성되지 않으며, 반복 실패는
  ⚠️ 복구 카드로 노출된다.
- (단계 3a) claim-only 상태에서 구버전으로 롤백하는 시나리오를 검증해
  중복 실행 위험을 확인하고, 조건부 롤백 절차(quiesce → 읽기 전용 preflight
  → 배포 → 재개)가 `docs/rollback_and_upgrade_3a.md`에 문서화되어 있다.
- (단계 3a) 구버전 DB fixture로 업그레이드 backfill을 검증한다: 기존
  pending 요청이 v1 head로 연결되어 유지되고, 완료된 legacy 승인이
  재집행되지 않으며, 모호 케이스(복수 pending, 증거 불충분 ack)는 자동
  처리 대신 운영자 검토로 격리된다. 실패 주입 포함: backfill 단계 사이
  crash 후 같은 cutoff로 멱등 재개, 구·신 버전 동시 기동 시 completed
  마커 이전의 신규 스키마 write·sweep 차단, quiesce 없이 진행할 경우의
  경계 오염 검출.
- (단계 3a) 롤백 preflight가 미완 funding/budget 전이,
  consumed-without-dispatch-completion,
  decision_recorded-without-resolution_completed를 모두 검출하고, 각 미완
  상태에서의 롤백 시나리오가 테스트되어 있으며, preflight는 quiesce
  (writer·callback 유입 정지 + DB 배타 lock) 아래에서만 유효한 것으로
  절차화되어 있다.
- (단계 3a) 완료된 funding/budget 요청이 있는 DB를 구버전으로 롤백해도
  dual-write된 legacy 종결 이벤트 덕분에 구버전이 요청을 pending으로
  오판하지 않고, 구 callback이 거절된다.
- 부분 전송 실패(일부 chat_id 실패, edit 실패) 시 fallback 경로가 동작하고
  헬스체크에 반영된다.

VPS systemd 구성 변경 없음. 단계 1의 `set_my_commands`만 봇 시작 시 1회 호출.
