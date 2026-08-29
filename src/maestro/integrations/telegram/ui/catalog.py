"""모든 한글 문구·템플릿. 순수 데이터만 — 로직 금지.

문구 수정은 이 파일만 수정하면 끝난다.
"""

APPROVAL_TITLE = "📩 투자 주문을 진행할까요?"
APPROVAL_DEADLINE = "⏰ {deadline}까지 응답해 주세요."
APPROVAL_SUMMARY = "{strategy} 전략 · {market} {count}종목 · {sides} · {accounts}총 {total}"
APPROVAL_ACCOUNT_SCOPE = "계좌 {accounts} · "
APPROVAL_MORE_ACCOUNTS = "{accounts} 외 {count}곳"
APPROVAL_RISK_SUMMARY = "⚠️ 위험 점검에서 확인할 내용이 {count}건 있어요."
APPROVAL_RISK_REASON = "- {reason}"
APPROVAL_MORE_RISKS = "- 외 {count}건"
APPROVAL_MORE_ORDERS = "• 외 {count}건"

SIDE_BUY = "🟢 매수"
SIDE_SELL = "🔴 매도"
SIDE_UNKNOWN = "⚠️ 방향 미상"
SIDE_SUMMARY_BUY = "매수 {count}건"
SIDE_SUMMARY_SELL = "매도 {count}건"
SIDE_SUMMARY_UNKNOWN = "⚠️ 방향 미상 {count}건"

DECISION_APPROVED = "✅ 승인 완료 — 주문 {count}건을 접수했어요."
DECISION_APPROVED_PARTIAL = (
    "⚠️ 승인 완료 — 주문 {submitted}건 접수, {failed}건은 실패했어요. /history에서 확인해 주세요."
)
DECISION_APPROVED_ALL_FAILED = (
    "⚠️ 승인했지만 주문 {failed}건이 모두 실패했어요. /history에서 확인해 주세요."
)
DECISION_APPROVED_NONE = "⚠️ 승인했지만 접수된 주문이 없어요. /history에서 확인해 주세요."
DECISION_REJECTED = "❌ 거절했어요 — 이번 제안은 실행되지 않아요."
DECISION_EXPIRED = "⏳ 응답 시간이 지나 이번 제안은 실행되지 않았어요."
REMINDER = "⏰ 아직 응답을 기다리고 있어요 ({minutes}분 경과)"

APPROVAL_NEEDS_ATTENTION = (
    "⚠️ 확인이 필요해요 — 승인은 접수됐지만 주문이 만들어지지 않았어요.\n"
    "주문이 나가기 전에 중단된 상태예요.\n"
    "/history에서 상태를 확인해 주세요."
)
APPROVAL_NEEDS_RECONCILIATION = (
    "⚠️ 확인이 필요해요 — 승인 후 주문 처리가 끝나지 않았어요.\n"
    "일부 주문이 이미 브로커로 나갔을 수 있어 자동으로 다시 시도하지 않아요.\n"
    "증권사 앱에서 체결 내역을 확인해 주세요."
)

STALE_CALLBACK_TEXT = "이미 처리됐거나 만료된 요청이에요."
#: 마이그레이션이 진행 중이라 금융 경로가 잠긴 동안의 응답. 실패가 아니라
#: 보류이므로, 운영자가 다시 눌러야 할 일이라는 것을 알 수 있게 말한다.
MIGRATION_IN_PROGRESS_TEXT = "상태 마이그레이션이 진행 중이에요. 끝난 뒤에 다시 눌러 주세요."
CALLBACK_FAILED_TEXT = "처리하지 못했어요. 잠시 후 다시 시도해 주세요."
ANSWER_APPROVED = "승인했어요."
ANSWER_REJECTED = "거절했어요."

BUTTON_APPROVE = "✅ 승인"
BUTTON_REJECT = "❌ 거절"
BUTTON_DETAIL = "🔍 자세히"
BUTTON_FOLD = "접기"
BUTTON_PREV_PAGE = "◀ 이전"
BUTTON_NEXT_PAGE = "다음 ▶"

PAGE_INDICATOR = "({page}/{total}쪽)"
RISK_DETAIL_HEADER = "⚠️ 위험 점검 원문"
TRUNCATED_MARK = "…"

MARKET_DOMESTIC = "국내 주식"
MARKET_OVERSEAS = "해외 주식"
MARKET_MIXED = "국내·해외 주식"

EXPANDED_HEADER = "자세한 내용"

CARD_STAGE_LABELS = {
    "pending": "⏳ 대기",
    "in_progress": "🔵 진행 중",
    "done": "✅ 완료",
    "attention": "⚠️ 확인 필요",
}
DAILY_CARD_TITLE = "📊 오늘의 투자 현황"
DAILY_CARD_GROUP = "• {label} — {stage}"
NO_ACTION_NOTICE = "오늘은 매매할 것이 없어요."
CARD_FALLBACK_TEMPLATE = (
    "⚠️ 카드를 표시하지 못했어요 ({card_key}, 단계 {stage}). "
    "카드 없이 알려드리니 /history에서 확인해 주세요."
)
CARD_AMBIGUOUS_TEMPLATE = (
    "⚠️ 카드 전송 결과를 확인하지 못했어요 ({card_key}, 단계 {stage}). "
    "같은 카드가 두 장 보이거나 갱신되지 않을 수 있어요. "
    "/history에서 실제 상태를 확인해 주세요."
)
#: 요청 카드가 운영자에게 닿지 못했다. 카드 자체를 다시 보내지 않는 자리이므로
#: (전달 여부를 모르는 사본을 재전송하면 버튼이 두 장이 된다) 이 한 줄이 그
#: 요청에 대한 유일한 통지다 -- 무엇을 확인해야 하는지까지 말해야 한다.
#: 재개 예산을 다 쓴 전이. 여기서부터는 같은 탭을 한 번 더 하는 것이 아니라
#: 사람이 봐야 한다.
WORKFLOW_NEEDS_ATTENTION_TEMPLATE = (
    "⚠️ 재개를 여러 번 시도했지만 끝내지 못했어요 ({phase} 요청 {request_id}). "
    "자동 재개를 멈췄으니 /history에서 확인해 주세요."
)
REQUEST_CARD_UNDELIVERED_TEMPLATE = (
    "⚠️ 입금/예산 요청 카드를 전달하지 못했어요 ({phase} 요청 {request_id}). "
    "이 요청은 카드 없이 남아 있어요. /history에서 확인해 주세요."
)

FUNDING_WORKFLOW_STAGE_COPY: dict[str, str] = {
    "funding_pending": "📥 입금이 필요해요",
    "funding_confirming": "⏳ 입금을 확인하고 있어요",
    "funding_canceling": "⏳ 취소를 처리하고 있어요",
    "budget_pending": "💰 이번 달 예산을 선택해 주세요",
    "budget_applying": "⏳ 예산을 적용하고 있어요",
    "budget_canceling": "⏳ 취소를 처리하고 있어요",
    "funding_canceled": "🛑 이번 달 입금 요청을 취소했어요",
    "budget_canceled": "🛑 이번 달 예산 선택을 취소했어요",
    "budget_completed": "✅ 이번 달 예산을 확정했어요",
    "funding_completed": "✅ 자금 확인을 마쳤어요",
}
FUNDING_WORKFLOW_ATTENTION_PREDECESSOR_INCOMPLETE = "⚠️ 자금 확인을 마무리하고 있어요"
FUNDING_WORKFLOW_ATTENTION_INCOMPLETE_TRANSITION = "⚠️ 처리가 끝나지 않아 확인이 필요해요."
FUNDING_WORKFLOW_PREDECESSOR_INCOMPLETE_BODY = (
    "이번 달 예산 요청이 준비되었지만, 입금 확인이 끝나기 전에는 선택할 수 없어요."
)
