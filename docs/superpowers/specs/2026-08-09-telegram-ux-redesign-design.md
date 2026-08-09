# Telegram UI/UX 전면 개편 설계

날짜: 2026-08-09
상태: 설계 승인 완료 (구현 계획 대기)
개정: 2026-08-09 Codex 적대적 리뷰 반영 — 승인 카드 2계층화,
UI 실패 fallback 경로, funding_workflow_id 도입
개정 2차: 2026-08-09 Codex 적대적 리뷰 2차 반영 — funding_workflow_id를
scope 복합 키로 재정의, 액션 라우팅은 request_id에만 바인딩
개정 3차: 2026-08-09 Codex 적대적 리뷰 3차 반영 — funding 요청 교체의
비원자성 대응: 영속 workflow 상태 머신 + 원자적 claim (접근 A의 명시적 예외)
개정 4차: 2026-08-09 Codex 적대적 리뷰 4차 반영 — child run lineage 영속화,
funding/budget 공통 상태 전이(claimed→child_created→completed), budget
decision의 비종결화, 단계 3 roll-forward-only 명시
개정 5차: 2026-08-09 Codex 적대적 리뷰 5차 반영 — 워크플로우 head/version
CAS로 단일 활성 요청 보장, 승인 dispatch의 idempotent resume
(consumed와 dispatch 완료 분리)

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
├── bot.py               (기존 — HTTP 클라이언트, 변경 없음)
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
- **lifecycle.py** — 유일한 상태 보유 컴포넌트. 카드별
  `(card_key, chat_id, message_id, 단계, 렌더 해시)`를 state store의
  system event(`telegram_ui_card`)로 기록하고, poll 루프 sweep에서 상태 변화를
  감지해 `edit_message_text`로 갱신.
- **format.py** — `money_kr`, `deadline_kr` 등 한국어 표기 유틸.

**의존성 방향**: `handlers.py → ui/` 단방향. `ui/`는 handlers·orchestrator·
execution을 임포트하지 않는다. `execution/budget_requests.py`,
`execution/funding_requests.py`에 있는 메시지 생성도 `ui/`로 이동하고
execution 모듈은 데이터만 넘긴다.

기존 `TelegramBotClient`가 이미 `edit_message_text`,
`edit_message_reply_markup`, `set_my_commands`를 지원하므로 클라이언트 변경은
없다.

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

트랑필로 전략 · 국내 주식 2종목 · 총 124만원

• 삼성전자 10주 — 71.2만원
• 미국 S&P500 ETF 28주 — 52.8만원

⏰ 밤 11시 30분까지 응답해 주세요.

[✅ 승인] [❌ 거절] [🔍 자세히]
```

기존의 개별 주문 상태 알림(`Maestro live order update` 연속 전송)은 카드 갱신으로
대체한다.

### B. 월간 자금 카드

입금 요청/예산 선택 흐름을 하나의 라이프사이클로 통합:
`📥 입금 필요 → 입금 확인 중 → 예산 선택 → ✅ 이번 달 예산 확정`.

**안정적인 워크플로우 키**: 현재 흐름에서는 입금 확인 후 새 signal run이
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
funding_scope = (contribution_group_id or "-", account_id or "-",
                 execution_sleeve or "-", currency)
funding_workflow_id = funding:<scope_hash>:<month_key>
```

`scope_hash`는 funding_scope 정규화 튜플의 짧은 해시(예: sha1 앞 8자리)로,
callback data 64바이트 제한 안에서 card_key로 쓸 수 있게 한다. 원본
funding_scope 튜플은 `telegram_ui_card` 이벤트 payload에 그대로 기록해
디버깅 시 역추적할 수 있게 한다. 이 필드들은 funding/budget 요청 payload에
이미 모두 존재하므로 **비즈니스 로직 변경 없이 UI 계층에서 파생 가능**하다
(접근 A 원칙 유지).

**액션 라우팅은 request_id에만 바인딩**: `funding_workflow_id`는 카드
식별·집계 전용이며, 버튼 callback은 항상 구체적인 `request_id`를 담는다.
교체·만료된 request_id의 callback은 활성 요청으로 위임하지 않고 "이미
지난 요청이에요. 최신 카드를 확인해 주세요."로 거절한다 — 구 버튼이 다른
요청을 취소/선택하는 오동작을 원천 차단한다. 카드 상태에는 **현재 활성
request_id와 요청 교체 이력**을 기록하여, 어떤 단계에서 요청이 재발급되어도
같은 카드가 이어서 갱신된다.

**요청 교체의 비원자성 대응 (영속 workflow 상태 머신)**: "교체된 request_id
거절"은 UI 레이어만으로는 보장할 수 없다. 현재 funding 확인 흐름
(`_confirm_funding_request`)은 **현금흐름 기록 → `run_signal()`(신규
signal/요청 영속화) → funding ack 저장** 순서라서, `run_signal` 완료와 ack
저장 사이에 프로세스가 종료되면 구 요청은 여전히 pending이고 신규 요청도
이미 존재한다. `_load_pending_funding_request()`는 해당 request_id의 ack
유무만 확인하므로 구 버튼의 재시도가 signal run·현금흐름 기록을 중복
생성할 수 있다 — UI 개편 이전부터 존재하는 결함이며, funding 카드가
약속하는 상태 일관성의 전제이므로 단계 3에서 함께 해결한다. 이 부분은
**"비즈니스 로직 불변" 원칙(접근 A)의 명시적 예외**로, handlers의 funding
확인 경로에 다음 규약을 도입한다:

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
2. **원자적 claim**: funding/budget callback 처리 시작 시 먼저 해당
   request_id가 **현재 head인지 검증**하고(head가 아니면 거절),
   `funding_workflow_claim` system event를
   `duplicate_key = <funding_workflow_id>:<phase>:<request_id>`로 기록한다
   (기존 duplicate_key 멱등 컨벤션 재사용). 이미 claim이 존재하면 처리에
   진입하지 않고 "이미 처리 중이거나 완료된 요청이에요"로 응답한다 —
   동시 중복 callback과 교체 전 요청의 재실행을 상태 변경 이전에 차단한다.
3. **공통 상태 전이 `claimed → child_created → completed`**: funding과
   budget 전환 모두 이 3단계를 system event로 영속화한다. `completed`만이
   종결 상태다. 특히 **budget decision은 종결 ack가 아니라** 전환의 입력값
   (선택 금액)일 뿐이다 — 현재 코드는 `contribution_budget_request_decision`
   저장 즉시 요청을 pending에서 제외하므로, decision 직후
   refresh/config load/`run_signal()`에서 실패하면 child run 없이 요청이
   종결돼 월간 투자가 조용히 멈춘다. 개편 후에는 decision이 저장돼도
   `completed` 이벤트가 없으면 워크플로우는 미완으로 취급되어 복구 대상이
   된다.
4. **child run lineage 영속화**: 현재 `run_signal()`은 원천 request를 알지
   못하므로 "claim 이후 생성된 run"을 같은 전략·scope의 다른 수동/예약
   run과 구분할 수 없다. `run_signal()`에 `source_request_id`(및
   funding_workflow_id)를 전달해 signal run 기록/package에 영속화하고,
   복구 시 이 lineage로만 child run을 조회한다 (추론적 연결 금지).
   같은 scope에서 동시에 존재하는 수동 run과의 조회 유일성을 테스트로
   보장한다.
5. **중단 전환의 자동 재실행 금지**: claim은 있으나 `completed`가 없는
   상태로 재시작되면 같은 전환을 자동 재실행하지 않는다. lineage로 child
   signal run을 조회해 **있으면 재사용**하여 나머지 단계(승인 dispatch,
   completed 기록)만 이어서 수행하고, 없으면 기존 워크플로우 복구 카드
   ("이전 작업이 중단된 상태예요")로 라우팅해 운영자 확인 후 진행한다.
   budget의 경우 저장된 decision 금액을 그대로 사용해 재개한다.
6. **승인 dispatch의 idempotent resume**: "child run이 있으면 승인
   dispatch부터 재개"가 성립하려면 dispatch 자체가 재개 가능해야 한다.
   현재 `dispatch_signal_approval`은 package를 **consumed로 먼저 기록한 뒤**
   그룹별 pending 이벤트 저장과 채팅별 Telegram 전송을 수행하므로,
   consumed 직후 또는 일부 그룹/채팅 전송 후 crash하면 재호출이
   "Signal package already consumed"로 거부되어 워크플로우가 영구 미완이
   되거나 일부 승인 카드만 노출된다. 개편 후 규약:
   - consumed는 "dispatch 배타 시작" 표지로 재정의하고, **dispatch 완료
     판정과 분리**한다. 완료(`signal_approval_pending`)는 모든 그룹×채팅
     전송이 끝난 뒤에만 기록한다.
   - 그룹 분할·순서를 결정적으로 만들어(정렬) 재개 시 동일한 그룹이
     나오게 하고, 이미 저장된 그룹별 `telegram_approval_pending` envelope
     (approval_id duplicate_key로 durable)이 있으면 새 approval을 만들지
     않고 재사용한다.
   - 채팅별 전송 성공을 `duplicate_key = sent:<approval_id>:<chat_id>`
     이벤트로 기록해, 재개 시 미전송 채팅만 발송한다.
   - resume 진입 조건: consumed이지만 완료 이벤트가 없는 package.
   이 변경은 funding 재생성 run만이 아니라 승인 dispatch 전체에 적용되며,
   접근 A 예외 범위에 포함된다.
7. **현금흐름 기록 멱등화**: 전환 내 현금흐름 기록도 request_id 기반
   duplicate_key로 멱등 처리하여, 복구 재시도 시 중복 기록되지 않는다.

### C. 예외 카드 (가이드형 마법사)

공통 3단 구성: **무슨 일인지(일상 언어) → 지금 선택할 수 있는 것(버튼 2~4개) →
⚙️ 자세한 사유 보기**.

| 상황 | 첫 문장 | 선택지 버튼 |
|---|---|---|
| 미체결/용량 부족 | "주문이 체결되지 않았어요" | [최대 n주로 재시도] [직접 수량 입력] [오늘은 건너뛰기] |
| cash drift 감지 | "계좌에 설명되지 않은 돈의 변화가 있어요" | [배당금] [이자] [세금] [입출금] [기타·나중에] |
| 미체결 정산 경고 | "체결 내역이 장부와 맞지 않아요" | [지금 다시 확인] [⚙️ 자세한 사유] (신규 주문 차단 중임을 명시) |
| 워크플로우 복구 | "이전 작업이 중단된 상태예요" | [자동 복구 시도] [증권사 앱 확인 후 승인] [⚙️ 자세히] |
| 안전 정지(halt) | "시스템이 안전을 위해 멈췄어요" | [점검 후 재개하기] [⚙️ 원인 보기] |

**위험 액션 확인 단계**: 재주문 승인, 복구 실행, 일시중지, 긴급정지는 버튼을
누르면 카드가 "정말 실행할까요? — 실행되는 내용: ..." 확인 뷰로 바뀌고
[실행] [취소]를 다시 누른다. 현재 pause/kill_switch의 confirm 패턴을 전체 위험
액션으로 일반화한다.

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
- callback data는 `ui:detail:<card_key>` 형식으로 기존 `operator:` prefix 체계와
  구분하며 Telegram 64바이트 제한을 준수한다.

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

1. 카드 전송 시 `telegram_ui_card` system event로
   `(card_key, chat_id, message_id, 단계, 렌더 데이터 해시)` 기록.
   기존 state store 사용, 새 저장소 없음.
   `card_key`는 카드 유형별 원천 ID로 정한다:
   승인 카드 = `approval:<approval_id>` (유일한 액션 카드),
   데일리 요약 카드 = `daily:<signal_run_id>` (읽기 전용 집계),
   월간 자금 카드 = `funding:<scope_hash>:<YYYY-MM>` (funding_workflow_id;
   scope_hash는 contribution_group_id·account_id·execution_sleeve·currency
   정규화 튜플의 짧은 해시, 원본 튜플은 이벤트 payload에 기록),
   예외 카드 = `<유형>:<원천 event id>`.
2. poll 루프 sweep에 `_sweep_lifecycle_cards()` 추가: 활성 카드의 원천 이벤트
   (승인 ack, 주문 상태, 정산 결과)를 조회해 단계 변화 시 re-render →
   `edit_message_text`.
3. 렌더 결과가 직전과 같으면(해시 비교) edit 생략 — "message is not modified"
   에러와 불필요한 API 호출 방지.

## 에러 처리

- **edit 실패 → 새 메시지 폴백** 후 message_id 갱신 (48시간 경과, 메시지 삭제
  등). 기존 `_edit_callback_message` 폴백 패턴 재사용.
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
    전후 / 각 채팅 전송 전후에 중단 후 재개 — 기존 approval_id·envelope이
    재사용되고, 미전송 채팅에만 발송되며, 전부 끝난 뒤에야 dispatch 완료가
    기록된다
  - 연속 렌더/edit 실패 → 고정 템플릿 fallback 발송 + 헬스 degraded
- **기존 handlers 테스트 유지**: 비즈니스 로직 불변이 원칙이므로 기존 테스트가
  깨지면 로직을 건드렸다는 경고 신호.

## 마이그레이션 순서 (단계별 독립 배포, 롤백 규칙은 단계별로 명시)

| 단계 | 내용 | 효과 |
|---|---|---|
| 1 | `ui/` 모듈 신설 + 승인 카드 교체 + 자세히 토글 + 메뉴 5개 등록 | 가장 자주 보는 메시지 즉시 개선 |
| 2 | 라이프사이클 카드 매니저 + 승인/데일리 카드 + 노옵 한 줄 + fallback 알림 경로 | 구조 개편의 핵심 |
| 3 | 월간 자금 카드 (funding_workflow_id 기반 입금·예산 통합) + funding/budget 확인 경로의 상태 머신·원자적 claim·lineage (접근 A 예외) | 월초 경험 개선 + 기존 교체 경합 결함 해소 |
| 4 | 예외 마법사 (재주문·cash drift·복구·안전 정지) | 장애 대응 경험 개선 |
| 5 | 조회 카드 5종 + 구 명령어 메뉴 숨김 + 구 알림 경로 제거 | 마무리 |

**단계 2의 안전망**: 개별 주문 알림·미체결 경고·halt·정산 불일치 알림의
**기존 전송 경로는 단계 2에서 제거하지 않고 카드와 병행 유지**한다.
카드 전달 성공률이 실운영에서 검증된 후(아래 승인 조건 충족) 단계 5에서
구 경로를 제거한다.

**단계 3은 roll-forward-only**: 단계 1·2·4·5는 UI 전용이므로 코드 롤백만으로
안전하게 되돌릴 수 있지만, 단계 3은 다르다. 새 코드가 남긴
`funding_workflow_claim`을 구 handlers는 확인하지 않으므로, claim 이후
`completed` 이전 상태에서 구버전으로 롤백하면
`_load_pending_funding_request()`가 요청을 다시 pending으로 간주해
`run_signal()`을 중복 실행할 수 있다 — 즉 장애 시점의 롤백이 새 불변식을
제거한다. 따라서 단계 3 이후에는 **버그 대응도 수정 배포(roll-forward)로만
진행**하고, 부득이하게 롤백해야 하면 미완(claim-only) 워크플로우가 없음을
먼저 확인하거나 해당 워크플로우를 수동 종결한 뒤 롤백한다. 이 절차는 운영
문서에 명시한다.

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
  재개 시 approval이 중복 생성되지 않고 미전송 채팅만 발송되며, 같은
  scope/month의 병행 pending 요청 경쟁에서 head인 요청만 처리된다.
- (단계 3) claim-only 상태에서 구버전으로 롤백하는 시나리오를 검증해
  중복 실행 위험을 확인하고, roll-forward-only 운영 절차가 문서화되어 있다.
- 부분 전송 실패(일부 chat_id 실패, edit 실패) 시 fallback 경로가 동작하고
  헬스체크에 반영된다.

VPS systemd 구성 변경 없음. 단계 1의 `set_my_commands`만 봇 시작 시 1회 호출.
