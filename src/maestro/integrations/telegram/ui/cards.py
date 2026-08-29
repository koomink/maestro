"""카드 렌더러: 상태 데이터 → (text, reply_markup). 순수 함수만."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from maestro.approval.models import ApprovalRequest
from maestro.core.strategy_names import strategy_display_label
from maestro.execution.budget_requests import budget_request_reply_markup
from maestro.execution.funding_requests import funding_request_reply_markup
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.format import (
    deadline_kr,
    money_full,
    money_kr,
    quantity_kr,
)
from maestro.integrations.telegram.ui.funding_workflow import FundingWorkflowCardModel

_MAX_COLLAPSED_ORDER_LINES = 6
_MAX_COLLAPSED_RISK_LINES = 3
_MAX_COLLAPSED_ACCOUNTS = 3
_CALLBACK_PREFIX = "operator:"

#: Telegram sendMessage/editMessageText 본문 한도 (UTF-16 코드 유닛).
TELEGRAM_TEXT_LIMIT = 4096
#: 쪽 표시 줄을 위해 미리 떼어 두는 여유분.
_PAGE_INDICATOR_BUDGET = 24


def telegram_text_length(text: str) -> int:
    """Telegram이 세는 길이 = UTF-16 코드 유닛 수 (BMP 밖 이모지는 2)."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class RenderedCard:
    text: str
    # None where a card is deliberately read-only: the daily summary carries no
    # buttons because each group is approved against its own approval_id.
    reply_markup: dict[str, Any] | None
    page: int = 0
    page_count: int = 1


def render_approval_card(
    request: ApprovalRequest,
    *,
    expanded: bool,
    page: int = 0,
) -> RenderedCard:
    if not expanded:
        return RenderedCard(
            text=_clamp(_collapsed_text(request)),
            reply_markup=approval_markup(request.approval_id, expanded=False),
        )
    pages = approval_detail_pages(request)
    index = max(0, min(page, len(pages) - 1))
    return RenderedCard(
        text=pages[index],
        reply_markup=approval_markup(
            request.approval_id,
            expanded=True,
            page=index,
            page_count=len(pages),
        ),
        page=index,
        page_count=len(pages),
    )


def render_approval_stage_card(request: ApprovalRequest, stage: str) -> RenderedCard:
    """The one approval card, rendered at whatever stage it has reached.

    Before a decision this is exactly the card production already sends --
    identical bytes, buttons and all -- so the lifecycle taking over delivery
    changes nothing the operator sees. Afterwards the body stays and a stage
    line goes on top: the card is meant to replace a stream of notifications,
    and a bare result line would lose what was actually ordered.

    The buttons go away with the decision. Leaving them on a resolved approval
    offers a choice that is no longer there -- the callback is refused as
    stale, but only after the operator believed it was still theirs to make.
    """
    label = catalog.CARD_STAGE_LABELS.get(stage)
    if stage == "pending" or label is None:
        # An unrecognised stage must not strip the buttons off a live approval,
        # so anything we cannot name renders as the actionable card.
        return render_approval_card(request, expanded=False)
    return RenderedCard(
        text=_clamp(f"{label}\n\n{_collapsed_text(request)}"),
        reply_markup=None,
    )


def approval_detail_pages(request: ApprovalRequest) -> list[str]:
    """펼친 뷰 전체 내용을 Telegram 길이 한도 이하의 쪽들로 나눈다."""
    header = _header_lines(request)
    footer = _detail_footer_lines(request)
    fixed = telegram_text_length("\n".join([*header, *footer])) + _PAGE_INDICATOR_BUDGET
    capacity = max(TELEGRAM_TEXT_LIMIT - fixed, 1)

    grouped: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for block in _detail_blocks(request):
        # 한 쪽보다 큰 블록도 버리지 않고 조각내어 다음 쪽으로 이어 붙인다.
        for chunk in _split_block(block, capacity):
            length = telegram_text_length("\n".join(chunk)) + 1
            if current and current_length + length > capacity:
                grouped.append(current)
                current = []
                current_length = 0
            current.extend(chunk)
            current_length += length
    grouped.append(current)

    total = len(grouped)
    pages: list[str] = []
    for index, body in enumerate(grouped, start=1):
        lines = [*header, *body, *footer]
        if total > 1:
            lines.extend(["", catalog.PAGE_INDICATOR.format(page=index, total=total)])
        pages.append(_clamp("\n".join(lines)))
    return pages


def _collapsed_text(request: ApprovalRequest) -> str:
    lines = [*_header_lines(request)]
    lines.extend(_order_lines(request.proposed_orders, expanded=False))
    if request.risk_violations:
        lines.append("")
        lines.extend(_collapsed_risk_lines(request.risk_violations))
    lines.append("")
    lines.append(catalog.APPROVAL_DEADLINE.format(deadline=deadline_kr(request.expires_at)))
    return "\n".join(lines)


def _account_scope(orders: list[dict]) -> str:
    """어느 계좌에서 돈이 나가는지는 승인 결정 자체를 바꾸므로 요약줄에 남긴다.
    주문별 계좌 줄은 그대로 펼친 뷰에만 둔다."""
    accounts = sorted(
        {
            order["account_id"]
            for order in orders
            if isinstance(order.get("account_id"), str) and order["account_id"]
        }
    )
    if not accounts:
        return ""
    shown = ", ".join(accounts[:_MAX_COLLAPSED_ACCOUNTS])
    hidden = len(accounts) - _MAX_COLLAPSED_ACCOUNTS
    if hidden > 0:
        shown = catalog.APPROVAL_MORE_ACCOUNTS.format(accounts=shown, count=hidden)
    return catalog.APPROVAL_ACCOUNT_SCOPE.format(accounts=shown)


def _collapsed_risk_lines(violations: list[str]) -> list[str]:
    """승인 버튼이 붙는 첫 화면에도 위험 사유 원문을 남긴다 (건수 요약만으로는
    무엇이 걸렸는지 모른 채 승인하게 된다)."""
    lines = [catalog.APPROVAL_RISK_SUMMARY.format(count=len(violations))]
    lines.extend(
        catalog.APPROVAL_RISK_REASON.format(reason=violation)
        for violation in violations[:_MAX_COLLAPSED_RISK_LINES]
    )
    hidden = len(violations) - _MAX_COLLAPSED_RISK_LINES
    if hidden > 0:
        lines.append(catalog.APPROVAL_MORE_RISKS.format(count=hidden))
    return lines


def _header_lines(request: ApprovalRequest) -> list[str]:
    return [
        catalog.APPROVAL_TITLE,
        "",
        catalog.APPROVAL_SUMMARY.format(
            strategy=strategy_display_label(request.source_strategy_ids),
            market=_market_summary(request.proposed_orders),
            count=len(request.proposed_orders),
            sides=_side_summary(request.proposed_orders),
            accounts=_account_scope(request.proposed_orders),
            total=_total_label(request),
        ),
        "",
    ]


def _detail_footer_lines(request: ApprovalRequest) -> list[str]:
    return [
        "",
        catalog.APPROVAL_DEADLINE.format(deadline=deadline_kr(request.expires_at)),
        "",
        catalog.EXPANDED_HEADER,
        f"- 승인 ID: {request.approval_id}",
        f"- 실행 ID: {request.run_id}",
        f"- 마감(ISO): {request.expires_at.isoformat()}",
    ]


def _detail_blocks(request: ApprovalRequest) -> list[list[str]]:
    blocks = [_order_block(order) for order in request.proposed_orders]
    for index, violation in enumerate(request.risk_violations):
        if index == 0:
            blocks.append(["", catalog.RISK_DETAIL_HEADER, f"- {violation}"])
        else:
            blocks.append([f"- {violation}"])
    return blocks


def _clamp(text: str) -> str:
    return _clamp_text(text, TELEGRAM_TEXT_LIMIT)


def _split_block(block: list[str], capacity: int) -> list[list[str]]:
    """블록을 한 쪽 용량 이하 조각들로 나눈다 (내용 손실 없음)."""
    if telegram_text_length("\n".join(block)) + 1 <= capacity:
        return [block]
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in block:
        for piece in _split_line(line, max(capacity - 1, 1)):
            length = telegram_text_length(piece) + 1
            if current and current_length + length > capacity:
                chunks.append(current)
                current = []
                current_length = 0
            current.append(piece)
            current_length += length
    if current:
        chunks.append(current)
    return chunks or [[]]


def _split_line(line: str, limit: int) -> list[str]:
    """긴 한 줄을 UTF-16 안전하게(코드 포인트 단위로) 여러 줄로 나눈다."""
    if telegram_text_length(line) <= limit:
        return [line]
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for char in line:
        width = telegram_text_length(char)
        if used + width > limit:
            pieces.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += width
    if current:
        pieces.append("".join(current))
    return pieces


def _clamp_text(text: str, limit: int) -> str:
    if telegram_text_length(text) <= limit:
        return text
    budget = limit - telegram_text_length(catalog.TRUNCATED_MARK)
    kept: list[str] = []
    used = 0
    for char in text:
        width = telegram_text_length(char)
        if used + width > budget:
            break
        kept.append(char)
        used += width
    return "".join(kept) + catalog.TRUNCATED_MARK


def approval_markup(
    approval_id: str,
    *,
    expanded: bool,
    page: int = 0,
    page_count: int = 1,
) -> dict[str, Any]:
    toggle = (
        {
            "text": catalog.BUTTON_FOLD,
            "callback_data": f"{_CALLBACK_PREFIX}ui:f:{approval_id}",
        }
        if expanded
        else {
            "text": catalog.BUTTON_DETAIL,
            "callback_data": f"{_CALLBACK_PREFIX}ui:d:{approval_id}",
        }
    )
    navigation: list[dict[str, str]] = []
    if expanded and page_count > 1:
        if page > 0:
            navigation.append(
                {
                    "text": catalog.BUTTON_PREV_PAGE,
                    "callback_data": f"{_CALLBACK_PREFIX}ui:p:{approval_id}:{page - 1}",
                }
            )
        if page < page_count - 1:
            navigation.append(
                {
                    "text": catalog.BUTTON_NEXT_PAGE,
                    "callback_data": f"{_CALLBACK_PREFIX}ui:p:{approval_id}:{page + 1}",
                }
            )
    return {
        "inline_keyboard": [
            [
                {
                    "text": catalog.BUTTON_APPROVE,
                    "callback_data": f"{_CALLBACK_PREFIX}appr:a:{approval_id}",
                },
                {
                    "text": catalog.BUTTON_REJECT,
                    "callback_data": f"{_CALLBACK_PREFIX}appr:r:{approval_id}",
                },
            ],
            *([navigation] if navigation else []),
            [toggle],
        ]
    }


def approval_decision_text(
    status: str,
    approval_id: str,
    *,
    orders_submitted: int,
    orders_failed: int,
) -> str:
    """승인 결과 문구. 실제로 브로커에 접수된 주문 수만 성공으로 보고한다."""
    if status == "expired":
        # 만료를 거절로 뭉뚱그리면, 보지도 못한 제안을 운영자가 직접 거절했다고
        # 알리게 된다. 실행되지 않은 것은 같지만 원인이 사실과 다르다.
        return catalog.DECISION_EXPIRED
    if status != "approved":
        return catalog.DECISION_REJECTED
    if orders_submitted and orders_failed:
        return catalog.DECISION_APPROVED_PARTIAL.format(
            submitted=orders_submitted, failed=orders_failed
        )
    if orders_submitted:
        return catalog.DECISION_APPROVED.format(count=orders_submitted)
    if orders_failed:
        return catalog.DECISION_APPROVED_ALL_FAILED.format(failed=orders_failed)
    return catalog.DECISION_APPROVED_NONE


def approval_reminder_text(minutes: int, card_text: str) -> str:
    # 카드 본문은 이미 한도까지 찰 수 있으므로 접두어를 붙인 뒤 다시 잘라낸다.
    return _clamp(f"{catalog.REMINDER.format(minutes=minutes)}\n\n{card_text}")


def _order_lines(orders: list[dict], *, expanded: bool) -> list[str]:
    lines: list[str] = []
    visible = orders if expanded else orders[:_MAX_COLLAPSED_ORDER_LINES]
    for order in visible:
        lines.extend(_order_block(order) if expanded else [_order_summary_line(order)])
    hidden = len(orders) - len(visible)
    if hidden > 0:
        lines.append(catalog.APPROVAL_MORE_ORDERS.format(count=hidden))
    return lines


def _order_summary_line(order: dict) -> str:
    name = str(order.get("name") or order.get("symbol") or "unknown")
    quantity = _float_or_none(order.get("quantity"))
    notional = _float_or_none(order.get("notional"))
    currency = order.get("currency") if isinstance(order.get("currency"), str) else None
    quantity_label = f" {quantity_kr(quantity)}" if quantity is not None else ""
    amount = money_kr(notional, currency) if notional is not None else "-"
    return f"• {_side_label(order)} {name}{quantity_label} — {amount}"


def _order_block(order: dict) -> list[str]:
    """펼친 뷰의 주문 1건 블록 (쪽 나눌 때 쪼개지지 않는 최소 단위)."""
    currency = order.get("currency") if isinstance(order.get("currency"), str) else None
    notional = _float_or_none(order.get("notional"))
    lines = [_order_summary_line(order)]
    symbol = str(order.get("symbol") or "unknown")
    broker_symbol = order.get("broker_symbol")
    code = (
        f"{symbol} (브로커: {broker_symbol})"
        if isinstance(broker_symbol, str) and broker_symbol and broker_symbol != symbol
        else symbol
    )
    lines.append(f"  코드: {code}")
    price = _float_or_none(order.get("limit_price", order.get("price")))
    if price is not None:
        lines.append(f"  지정가: {money_full(price, currency)}")
    if notional is not None:
        lines.append(f"  금액: {money_full(notional, currency)}")
    account_id = order.get("account_id")
    if isinstance(account_id, str) and account_id:
        lines.append(f"  계좌: {account_id}")
    return lines


def _side_label(order: dict) -> str:
    side = str(order.get("side") or "").lower()
    if side == "buy":
        return catalog.SIDE_BUY
    if side == "sell":
        return catalog.SIDE_SELL
    return catalog.SIDE_UNKNOWN


def _side_summary(orders: list[dict]) -> str:
    counts = {catalog.SIDE_BUY: 0, catalog.SIDE_SELL: 0, catalog.SIDE_UNKNOWN: 0}
    for order in orders:
        counts[_side_label(order)] += 1
    templates = (
        (catalog.SIDE_BUY, catalog.SIDE_SUMMARY_BUY),
        (catalog.SIDE_SELL, catalog.SIDE_SUMMARY_SELL),
        (catalog.SIDE_UNKNOWN, catalog.SIDE_SUMMARY_UNKNOWN),
    )
    return " · ".join(
        template.format(count=counts[label]) for label, template in templates if counts[label]
    )


def _market_summary(orders: list[dict]) -> str:
    domestic = 0
    overseas = 0
    for order in orders:
        if (
            order.get("broker_product") == "kis_domestic_stock"
            or order.get("exchange_code") == "KRX"
        ):
            domestic += 1
        else:
            overseas += 1
    if domestic and overseas:
        return catalog.MARKET_MIXED
    if overseas:
        return catalog.MARKET_OVERSEAS
    return catalog.MARKET_DOMESTIC


def _total_label(request: ApprovalRequest) -> str:
    totals: dict[str | None, float] = {}
    for order in request.proposed_orders:
        notional = _float_or_none(order.get("notional"))
        if notional is None:
            continue
        currency = order.get("currency") if isinstance(order.get("currency"), str) else None
        totals[currency] = totals.get(currency, 0.0) + notional
    if not totals:
        return money_kr(request.estimated_notional, None)
    return ", ".join(
        money_kr(value, currency) for currency, value in sorted(
            totals.items(), key=lambda item: str(item[0])
        )
    )


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_daily_card(
    signal_run_id: str,
    groups: Sequence[Mapping[str, Any]],
) -> RenderedCard:
    """Read-only parent card: one line per approval group.

    No buttons. Each group is approved independently against its own
    approval_id, so a button here would have nothing unambiguous to bind to.
    The signal_run_id is the card's identity, not part of its text -- operators
    read strategy names, not run ids.
    """
    lines = [catalog.DAILY_CARD_TITLE, ""]
    for group in groups:
        label = str(group.get("label") or "?")
        stage = catalog.CARD_STAGE_LABELS.get(
            str(group.get("stage") or ""), catalog.CARD_STAGE_LABELS["pending"]
        )
        lines.append(catalog.DAILY_CARD_GROUP.format(label=label, stage=stage))
    return RenderedCard(text=_clamp("\n".join(lines)), reply_markup=None)


def render_funding_workflow_card(model: FundingWorkflowCardModel) -> RenderedCard:
    """Render a unified monthly funding workflow card from its ephemeral read model."""
    if model.attention == "predecessor_incomplete":
        first_line = catalog.FUNDING_WORKFLOW_ATTENTION_PREDECESSOR_INCOMPLETE
    else:
        default_stage = (
            "funding_pending" if model.phase == "funding" else "budget_pending"
        )
        first_line = catalog.FUNDING_WORKFLOW_STAGE_COPY.get(
            model.stage,
            catalog.FUNDING_WORKFLOW_STAGE_COPY[default_stage],
        )

    lines: list[str] = [first_line]
    if model.attention == "incomplete_transition":
        lines.append(catalog.FUNDING_WORKFLOW_ATTENTION_INCOMPLETE_TRANSITION)

    lines.append("")

    if model.attention == "predecessor_incomplete":
        lines.append(catalog.FUNDING_WORKFLOW_PREDECESSOR_INCOMPLETE_BODY)
        lines.append("")

    req = model.request
    strategy_ids = req.get("strategy_ids") or []
    if isinstance(strategy_ids, list) and strategy_ids:
        lines.append(f"전략: {strategy_display_label(strategy_ids)}")

    account_id = req.get("account_id")
    if isinstance(account_id, str) and account_id:
        lines.append(f"계좌: {account_id}")

    execution_sleeve = req.get("execution_sleeve")
    if isinstance(execution_sleeve, str) and execution_sleeve:
        lines.append(f"슬리브: {execution_sleeve}")

    currency = req.get("currency") if isinstance(req.get("currency"), str) else "KRW"
    available_cash = _float_or_none(req.get("available_cash"))
    if available_cash is not None:
        lines.append(f"예수금: {money_kr(available_cash, currency)}")

    if model.phase == "funding":
        shortfall = _float_or_none(req.get("required_shortfall"))
        if shortfall is not None:
            lines.append(f"부족 금액: {money_kr(shortfall, currency)}")
        min_budget = _float_or_none(req.get("min_monthly_budget"))
        if min_budget is not None:
            lines.append(f"최소 필요액: {money_kr(min_budget, currency)}")
        recommended_top_up = _float_or_none(req.get("recommended_top_up"))
        if recommended_top_up is not None and (
            shortfall is None or recommended_top_up != shortfall
        ):
            lines.append(f"권장 입금액: {money_kr(recommended_top_up, currency)}")
    else:  # budget
        if model.selected_budget is not None:
            lines.append(f"선택 예산: {money_kr(model.selected_budget, currency)}")
        else:
            min_budget = _float_or_none(req.get("min_monthly_budget"))
            if min_budget is not None:
                lines.append(f"최소 예산: {money_kr(min_budget, currency)}")
            rec_budget = _float_or_none(req.get("recommended_budget"))
            if rec_budget is not None:
                lines.append(f"추천 예산: {money_kr(rec_budget, currency)}")
            max_budget = _float_or_none(req.get("selectable_max_budget"))
            if max_budget is not None:
                lines.append(f"최대 예산: {money_kr(max_budget, currency)}")

    reply_markup: dict[str, Any] | None = None
    if model.financial_actions_allowed:
        if model.phase == "funding":
            reply_markup = funding_request_reply_markup(model.request_id)
        else:
            reply_markup = budget_request_reply_markup(dict(model.request))

    return RenderedCard(text=_clamp("\n".join(lines)), reply_markup=reply_markup)

