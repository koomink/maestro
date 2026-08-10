"""모든 한글 문구·템플릿. 순수 데이터만 — 로직 금지.

문구 수정은 이 파일만 수정하면 끝난다.
"""

APPROVAL_TITLE = "📩 투자 주문을 진행할까요?"
APPROVAL_DEADLINE = "⏰ {deadline}까지 응답해 주세요."
APPROVAL_SUMMARY = "{strategy} 전략 · {market} {count}종목 · {sides} · 총 {total}"
APPROVAL_RISK_SUMMARY = "⚠️ 위험 점검에서 확인할 내용이 {count}건 있어요."
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
REMINDER = "⏰ 아직 응답을 기다리고 있어요 ({minutes}분 경과)"

STALE_CALLBACK_TEXT = "이미 처리됐거나 만료된 요청이에요."
CALLBACK_FAILED_TEXT = "처리하지 못했어요. 잠시 후 다시 시도해 주세요."
ANSWER_APPROVED = "승인했어요."
ANSWER_REJECTED = "거절했어요."

BUTTON_APPROVE = "✅ 승인"
BUTTON_REJECT = "❌ 거절"
BUTTON_DETAIL = "🔍 자세히"
BUTTON_FOLD = "접기"

MARKET_DOMESTIC = "국내 주식"
MARKET_OVERSEAS = "해외 주식"
MARKET_MIXED = "국내·해외 주식"

EXPANDED_HEADER = "자세한 내용"
