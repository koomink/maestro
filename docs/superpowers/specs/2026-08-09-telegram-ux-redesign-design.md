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
개정 6차: 2026-08-09 Codex 적대적 리뷰 6차 반영 — head 갱신의 트랜잭션
결합 + 수렴 sweep, claim의 attempt 기반 재개(fencing), Telegram 전송
exactly-once 포기(at-least-once + 중복 카드 정리), 단계 3을 3a/3b로 분리
개정 7차: 2026-08-09 Codex 적대적 리뷰 7차 반영 — head 검증·claim 삽입을
조건부 트랜잭션으로 결합(TOCTOU 차단), child 생성의
(source_request_id, phase) 유일성 + lock 경계 내 재검증
개정 8차: 2026-08-09 Codex 적대적 리뷰 8차 반영 — 워크플로우 영속 식별자를
전체 정규화 scope로 변경(해시는 표시용 토큰으로 격하), 유실 카드
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
결정하는 모든 system event는 `funding_workflow_id`를 **전체 정규화 scope
문자열 그대로** 사용한다. 짧은 해시(예: sha1 앞 8자리)는 32비트 식별
공간이라 서로 다른 scope가 같은 head/CAS를 공유하는 충돌이 가능하고,
충돌하면 한 scope의 요청 생성이 다른 scope를 superseded 처리해 월간
투자가 누락될 수 있다. system event payload에는 길이 제한이 없으므로
전체 키를 쓰는 데 비용이 없다. 방어선으로 head/claim 처리 시 payload에
저장된 원본 scope 튜플을 기대 scope와 비교해 불일치하면 거부한다.

**callback data 64바이트 제한은 토큰 매핑으로 해결한다**: card_key가
callback data에 들어가야 하는 경우(`ui:detail:...`)에는 워크플로우 식별을
전체 키로 하지 않고, 카드 생성 시 발급하는 **짧은 불투명 토큰**(예: 8자
무작위)을 쓴다. 토큰 → funding_workflow_id 매핑은 `telegram_ui_card`
이벤트에 기록하며, 토큰 발급은 duplicate_key get-or-create로 충돌 없이
1:1을 보장한다. 토큰은 표시·조회 전용이고 정합성 판단에는 쓰지 않는다.

scope 필드들은 funding/budget 요청 payload에 이미 모두 존재하므로
**비즈니스 로직 변경 없이 UI 계층에서 파생 가능**하다 (접근 A 원칙 유지).

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
약속하는 상태 일관성의 전제이므로 단계 3a에서 함께 해결한다. 이 부분은
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
   **head 갱신은 요청 생성과 한 트랜잭션이어야 한다**: 버전 duplicate_key
   만으로는 "요청 저장 후 head 갱신 전 중단 → head에 연결되지 않은 orphan
   요청" 또는 그 반대의 dangling head가 남는다. state store는 단일 SQLite
   이므로, 여러 system event(신규 요청, 이전 요청 superseded, 새 head)를
   duplicate_key 조건과 함께 **하나의 DB 트랜잭션으로 커밋하는 StateStore
   API**(`save_system_events_atomic`)를 신설해 요청 생성·교체·head 전환을
   원자화한다. 방어선으로, 복구 sweep가 재시작 시 불변식을 검사해
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
   (선택 금액)일 뿐이다 — 현재 코드는 `contribution_budget_request_decision`
   저장 즉시 요청을 pending에서 제외하므로, decision 직후
   refresh/config load/`run_signal()`에서 실패하면 child run 없이 요청이
   종결돼 월간 투자가 조용히 멈춘다. 개편 후에는 decision이 저장돼도
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
4. **child run lineage 영속화 + 생성 유일성(get-or-create)**: 현재
   `run_signal()`은 원천 request를 알지 못하므로 "claim 이후 생성된 run"을
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
   현재 `dispatch_signal_approval`은 package를 **consumed로 먼저 기록한 뒤**
   그룹별 pending 이벤트 저장과 채팅별 Telegram 전송을 수행하므로,
   consumed 직후 또는 일부 그룹/채팅 전송 후 crash하면 재호출이
   "Signal package already consumed"로 거부되어 워크플로우가 영구 미완이
   되거나 일부 승인 카드만 노출된다. 개편 후 규약:
   - consumed는 "dispatch 배타 시작" 표지로 재정의하고, **dispatch 완료
     판정과 분리**한다. 완료(`signal_approval_pending`)는 모든 그룹×채팅
     전송이 끝난 뒤에만 기록한다.
   - **결정적 dispatch_group_id로 approval을 get-or-create한다**: 현재
     `ApprovalManager.create_request`는 매번 무작위 approval_id를
     발급하므로, 일부 그룹 저장 후 crash한 재개 실행은 기존 그룹의
     approval_id를 알 수 없어 같은 주문 그룹에 새 approval을 만들 수
     있다. 이를 막기 위해 그룹 분할·순서를 결정적으로 만들고(정렬),
     `dispatch_group_id = <signal_run_id> + canonical 그룹 구성(정렬된
     전략 ID·계좌 ID)`을 정의한다. envelope 저장은
     `duplicate_key = dispatch-group:<dispatch_group_id>`의 유일성 제약
     아래 **원자적 get-or-create**로 수행한다 — 이미 존재하면 새
     approval_id를 발급하지 않고 저장된 envelope(approval_id·**최초
     만료시각 포함**)을 그대로 재사용한다. 같은 주문 그룹에 승인 카드나
     결정 경로가 둘 이상 생기는 일이 DB 수준에서 차단된다.
   - **전송은 exactly-once가 아니라 at-least-once**: sendMessage 성공과
     성공 이벤트 저장 사이의 중단은 원리적으로 구분할 수 없으므로
     "미전송 채팅만 정확히 발송"은 보장 대상이 아니다. 채팅별로 전송
     **intent**(`intent:<approval_id>:<chat_id>`)를 먼저 영속화하고,
     sendMessage 성공 후 결과(message_id)를 기록한다. 재개 시 intent도
     없는 채팅만 새로 발송하고, intent는 있으나 결과가 없는 **ambiguous
     채팅은 재발송한다** — 중복 카드가 생길 수 있음을 수용한다.
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
7. **승인 결정의 2단계 영속화 (ack ≠ 종결)**: 현재
   `_resolve_async_approval`은 `telegram_approval_ack`를 먼저 저장한 뒤
   주문 해석(`resolve_pending_signal_approval`)을 수행하고,
   `_pending_async_approval`과 `_sweep_pending_approvals`는 ack 존재만으로
   승인을 종결로 간주한다. 따라서 ack 저장 직후 프로세스 종료·config load
   실패·broker timeout이 발생하면 운영자의 승인 의사는 기록됐지만 주문은
   생성되지 않고, callback 재클릭도 "already decided"로 거절되어 **승인이
   영구 유실**된다. 개편 후 규약:
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

   **롤백은 조건부로만 안전하다**: 완료된 승인은 롤백해도 재집행되지 않지만,
   `schema_version=2` ack가 있고 `resolution_completed`가 없는 상태에서 롤백하면
   구버전이 ack만 보고 종결 처리해 승인된 주문이 유실된다. 롤백 절차는
   quiesce 아래에서 이를 검사하는 5단계다: (1) **quiesce** — 타이머뿐 아니라
   이미 실행 중인 서비스 인스턴스까지 모두 정지한다(타이머만 멈추면 실행
   중인 인스턴스가 writer로 계속 남는다), (2) **장벽 확인** — writer를
   되살릴 수 있는 유닛(`maestro-run-once` 포함)까지 정지 대상을 빠짐없이
   열거하고 `systemctl is-active`로 전부 inactive임을 확인한다, (3) **검사**
   — 위 위험 상태(및 다른 3a 미완 상태)가 하나도 없는지 확인한다. 이 검사는
   다음 태스크가 `maestro approval-rollback-preflight` CLI로 제공하며,
   인라인 쿼리가 아니다, (4) **구버전 배포** — 검사와 배포 사이 어떤 unit도
   재시작하지 않는다, (5) **재개** — 구버전 기동을 확인한 뒤에만 타이머·
   서비스를 재개한다. (3)에서 위험 상태가 하나라도 발견되면 롤백하지 않는다.
8. **현금흐름 기록 멱등화**: 전환 내 현금흐름 기록도 request_id 기반
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
   월간 자금 카드 = funding_workflow_id
   (`funding:<정규화 scope 전체>:<YYYY-MM>`; 이벤트 payload에는 전체 키,
   callback data에는 발급 토큰만 — 토큰↔키 매핑은 `telegram_ui_card`에 기록),
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
    전후 / 각 채팅 전송 전후에 중단 후 재개 — dispatch_group_id
    get-or-create에 의해 기존 approval_id·envelope·최초 만료시각이
    재사용되고(일부 그룹만 저장된 상태 포함), intent 없는 채팅은 발송·ambiguous 채팅은 재발송(중복 허용)
    되며, message_id가 알려진 중복 복사본은 무력화 edit로 정리되고,
    message_id가 유실된 복사본은 클릭 시 self-heal(그 메시지가 즉시 최종
    상태로 edit되고 추적 목록에 등록)되며, 승인 callback은 어느 카드에서
    눌러도 한 번만 처리되고, 전부 끝난 뒤에야 dispatch 완료가 기록된다
  - **scope 불일치 방어**: head/claim 이벤트 payload의 원본 scope가 기대
    scope와 다르면(식별자 오염 가정) 처리가 거부된다
  - **scope 직렬화 충돌**: null vs 문자열 `-`, 구분자(`:` 등)를 포함한
    식별자, NFC 정규화 전후가 다른 유니코드 문자열로 구성된 서로 다른
    scope 쌍이 서로 다른 funding_workflow_id로 직렬화된다
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

## 마이그레이션 순서 (단계별 독립 배포, 롤백 규칙은 단계별로 명시)

| 단계 | 내용 | 효과 |
|---|---|---|
| 1 | `ui/` 모듈 신설 + 승인 카드 교체 + 자세히 토글 + 메뉴 5개 등록 | 가장 자주 보는 메시지 즉시 개선 |
| 2 | 라이프사이클 카드 매니저 + 승인/데일리 카드 + 노옵 한 줄 + fallback 알림 경로 | 구조 개편의 핵심 |
| 3a | **정합성 기반 작업 (UI 아님, 접근 A 예외)**: StateStore 원자 커밋 API, 워크플로우 head/CAS, attempt 기반 claim·재개, lineage, dispatch idempotent resume, 승인 결정 2단계 영속화, 수렴 sweep | 기존 교체 경합·중단 복구 결함 해소 (독립 배포·검증) |
| 3b | 월간 자금 카드 (funding_workflow_id 기반 입금·예산 통합, 3a 위에 구축) | 월초 경험 개선 |
| 4 | 예외 마법사 (재주문·cash drift·복구·안전 정지) | 장애 대응 경험 개선 |
| 5 | 조회 카드 5종 + 구 명령어 메뉴 숨김 + 구 알림 경로 제거 | 마무리 |

**단계 2의 안전망**: 개별 주문 알림·미체결 경고·halt·정산 불일치 알림의
**기존 전송 경로는 단계 2에서 제거하지 않고 카드와 병행 유지**한다.
카드 전달 성공률이 실운영에서 검증된 후(아래 승인 조건 충족) 단계 5에서
구 경로를 제거한다.

**단계 3a는 roll-forward-only**: 단계 1·2·3b·4·5는 UI 전용이므로 코드
롤백만으로 안전하게 되돌릴 수 있지만, 단계 3a는 다르다. 새 코드가 남긴
`funding_workflow_claim`을 구 handlers는 확인하지 않으므로, claim 이후
`completed` 이전 상태에서 구버전으로 롤백하면
`_load_pending_funding_request()`가 요청을 다시 pending으로 간주해
`run_signal()`을 중복 실행할 수 있다 — 즉 장애 시점의 롤백이 새 불변식을
제거한다. 따라서 단계 3a 이후에는 **버그 대응도 수정 배포(roll-forward)로만
진행**하고, 부득이하게 롤백해야 하면 **quiesce 장벽 아래에서 롤백
preflight**를 통과한 뒤에만 롤백한다. preflight를 정지 없이 실행하면 검사
직후 구버전 기동 전까지 callback·sweep·예약 run이 새 미완 상태를 만들 수
있어 통과가 무의미해지므로, 롤백은 다음 순서를 강제한다:

1. **quiesce**: telegram-operator 서비스와 per-market signal 타이머 등
   state store에 쓰는 모든 systemd unit을 정지해 writer와 callback 유입을
   차단한다.
2. **최종 preflight**: 정지 상태에서 DB 배타 lock을 잡고 실행한다.
   아래 미완 상태가 모두 0건임을 확인한다 (하나라도 있으면 먼저 수동
   종결 후 재검사):

- 미완 funding/budget 전이 (claim은 있으나 `completed` 없음)
- consumed이지만 dispatch 완료(`signal_approval_pending`)가 없는
  signal package — 구버전은 이를 영구 consumed로 취급해 승인 카드가
  유실된다
- `decision_recorded`는 있으나 `resolution_completed`가 없는 승인 —
  구 handler는 ack만으로 영구 종결로 취급해 승인된 주문이 유실된다
- `completed`이지만 대응하는 legacy 종결 이벤트(funding ack / budget
  decision)가 없는 요청 — dual-write 규약 위반이며, 발견 시 롤백 CLI가
  legacy 이벤트를 멱등 backfill한 뒤 재검사한다

3. **구버전 배포**: preflight 통과 후, unit이 정지된 상태 그대로 구버전
   코드를 배포한다. preflight와 배포 사이에 어떤 unit도 재시작하지
   않는다 — 장벽은 구버전 기동이 완료될 때까지 유지된다.
4. **재개**: 구버전 기동을 확인한 뒤에만 타이머·서비스를 재개한다.

preflight 검사와 미완 상태의 수동 종결을 수행하는 운영 도구(CLI 점검
명령)를 3a에 포함하고, 각 미완 상태에서의 롤백 시나리오와 quiesce 순서
위반(정지 없이 preflight만 통과) 시의 위험을 테스트로 문서화한다.
이 절차는 운영 문서에 명시한다.

**3a 업그레이드 backfill**: 새 불변식은 3a 이전에 쌓인 상태를 모르므로,
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
  재개 시 approval이 중복 생성되지 않고, message_id가 알려진 중복 복사본은
  정리되며 유실 복사본은 클릭 시 self-heal되고 승인 callback은 멱등하며,
  같은 scope/month의 병행 pending 요청 경쟁에서 head인 요청만 처리된다.
- claim-only 상태의 워크플로우가 복구 카드 [재개]로 정확히 한 번 재개되고,
  orphan 요청/dangling head가 sweep에서 수렴된다.
- 승인 ack 이후 resolution 실패/중단이 영구 유실로 남지 않는다: sweep이
  기록된 결정으로 재개하고, 주문은 중복 생성되지 않으며, 반복 실패는
  ⚠️ 복구 카드로 노출된다.
- (단계 3a) claim-only 상태에서 구버전으로 롤백하는 시나리오를 검증해
  중복 실행 위험을 확인하고, roll-forward-only 운영 절차가 문서화되어 있다.
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
