"""카드 렌더러: 상태 데이터 → (text, reply_markup). 순수 함수만."""

from dataclasses import dataclass
from typing import Any

from maestro.approval.models import ApprovalRequest
from maestro.core.strategy_names import strategy_display_label
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.format import (
    deadline_kr,
    money_full,
    money_kr,
    quantity_kr,
)

_MAX_COLLAPSED_ORDER_LINES = 6
_CALLBACK_PREFIX = "operator:"


@dataclass(frozen=True)
class RenderedCard:
    text: str
    reply_markup: dict[str, Any]


def render_approval_card(request: ApprovalRequest, *, expanded: bool) -> RenderedCard:
    lines = [catalog.APPROVAL_TITLE, ""]
    lines.append(
        catalog.APPROVAL_SUMMARY.format(
            strategy=strategy_display_label(request.source_strategy_ids),
            market=_market_summary(request.proposed_orders),
            count=len(request.proposed_orders),
            total=_total_label(request),
        )
    )
    lines.append("")
    lines.extend(_order_lines(request.proposed_orders, expanded=expanded))
    if request.risk_violations:
        lines.append("")
        if expanded:
            lines.append("⚠️ 위험 점검 원문")
            lines.extend(f"- {item}" for item in request.risk_violations)
        else:
            lines.append(
                catalog.APPROVAL_RISK_SUMMARY.format(count=len(request.risk_violations))
            )
    lines.append("")
    lines.append(
        catalog.APPROVAL_DEADLINE.format(deadline=deadline_kr(request.expires_at))
    )
    if expanded:
        lines.append("")
        lines.append(catalog.EXPANDED_HEADER)
        lines.append(f"- 승인 ID: {request.approval_id}")
        lines.append(f"- 실행 ID: {request.run_id}")
        lines.append(f"- 마감(ISO): {request.expires_at.isoformat()}")
    return RenderedCard(
        text="\n".join(lines),
        reply_markup=approval_markup(request.approval_id, expanded=expanded),
    )


def approval_markup(approval_id: str, *, expanded: bool) -> dict[str, Any]:
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
            [toggle],
        ]
    }


def approval_decision_text(status: str, approval_id: str, orders_created: int) -> str:
    if status == "approved":
        return catalog.DECISION_APPROVED.format(count=orders_created)
    return catalog.DECISION_REJECTED


def approval_reminder_text(minutes: int, card_text: str) -> str:
    return f"{catalog.REMINDER.format(minutes=minutes)}\n\n{card_text}"


def _order_lines(orders: list[dict], *, expanded: bool) -> list[str]:
    lines: list[str] = []
    visible = orders if expanded else orders[:_MAX_COLLAPSED_ORDER_LINES]
    for order in visible:
        name = str(order.get("name") or order.get("symbol") or "unknown")
        quantity = _float_or_none(order.get("quantity"))
        notional = _float_or_none(order.get("notional"))
        currency = order.get("currency") if isinstance(order.get("currency"), str) else None
        quantity_label = f" {quantity_kr(quantity)}" if quantity is not None else ""
        amount = money_kr(notional, currency) if notional is not None else "-"
        lines.append(f"• {name}{quantity_label} — {amount}")
        if expanded:
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
    hidden = len(orders) - len(visible)
    if hidden > 0:
        lines.append(catalog.APPROVAL_MORE_ORDERS.format(count=hidden))
    return lines


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
