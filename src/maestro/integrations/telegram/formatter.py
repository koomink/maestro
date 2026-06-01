from maestro.approval.models import ApprovalRequest
from maestro.core.strategy_names import strategy_display_label


def format_approval_request(request: ApprovalRequest) -> str:
    lines = [
        "🔔 Maestro Approval",
        "",
        f"📁 Profile: {request.profile_name}" if request.profile_name else None,
        f"🧠 Strategy: {_strategy_label(request.source_strategy_ids)}",
        f"📦 Orders: {request.order_count}",
        f"💰 Total: {_approval_total_label(request)}",
        f"⏳ Expires: {request.expires_at.isoformat()}",
        f"🆔 Approval: {request.approval_id}",
    ]
    lines = [line for line in lines if line is not None]
    if request.risk_violations:
        lines.append("")
        lines.append("⚠️ Risk violations")
        lines.extend(f"- {item}" for item in request.risk_violations)
    if request.proposed_orders:
        lines.append("")
        lines.append("📋 Order Details")
        for index, order in enumerate(request.proposed_orders, start=1):
            if index > 1:
                lines.append("")
            lines.extend(_format_order(index, order))
    lines.extend(
        [
            "",
            "✅ Tap Approve to submit, or Reject to stop this proposal.",
        ]
    )
    return "\n".join(lines)


def _format_order(index: int, order: dict) -> list[str]:
    side = str(order.get("side") or "unknown").upper()
    symbol = str(order.get("symbol") or "unknown")
    name = order.get("name")
    broker_symbol = order.get("broker_symbol")
    quantity = _optional_float(order.get("quantity"))
    price = _optional_float(order.get("limit_price", order.get("price")))
    notional = _optional_float(order.get("notional"))
    currency = _optional_str(order.get("currency"))
    exchange_code = _optional_str(order.get("exchange_code"))
    broker_product = _optional_str(order.get("broker_product"))

    instrument = f"{symbol}"
    if isinstance(name, str) and name:
        instrument = f"{instrument} {name}"

    market = _market_label(exchange_code, broker_product)
    action = f"{_side_icon(side)} {side}"
    ticker = _ticker_label(symbol, broker_symbol)
    code_label = ticker if ticker != symbol else symbol

    if quantity is not None and price is not None:
        quantity_line = f"수량: {_number(quantity)}"
        price_line = f"지정가: {_money(price, currency)}"
    elif price is not None:
        quantity_line = "수량: -"
        price_line = f"지정가: {_money(price, currency)}"
    elif quantity is not None:
        quantity_line = f"수량: {_number(quantity)}"
        price_line = "지정가: -"
    else:
        quantity_line = "수량: -"
        price_line = "지정가: -"
    notional_label = _money(notional, currency) if notional is not None else "-"
    return [
        "━━━━━━━━━━━━",
        f"{index}. {action} · {market}",
        f"종목: {instrument}",
        f"코드: {code_label}",
        quantity_line,
        price_line,
        f"금액: {notional_label}",
    ]


def _strategy_label(strategy_ids: list[str]) -> str:
    return strategy_display_label(strategy_ids)


def _approval_total_label(request: ApprovalRequest) -> str:
    totals: dict[str, float] = {}
    for order in request.proposed_orders:
        notional = _optional_float(order.get("notional"))
        currency = _optional_str(order.get("currency"))
        if notional is None or currency is None:
            continue
        totals[currency] = totals.get(currency, 0.0) + notional
    if not totals:
        return _money(request.estimated_notional, None)
    return ", ".join(_money(value, currency) for currency, value in sorted(totals.items()))


def _market_label(exchange_code: str | None, broker_product: str | None) -> str:
    if broker_product == "kis_domestic_stock" or exchange_code == "KRX":
        return f"🇰🇷 국내 {exchange_code or 'KRX'}"
    if broker_product == "kis_overseas_stock":
        return f"🌐 해외 {exchange_code or 'unknown'}"
    if exchange_code:
        return exchange_code
    return "unknown"


def _ticker_label(symbol: str, broker_symbol: object) -> str:
    if isinstance(broker_symbol, str) and broker_symbol and broker_symbol != symbol:
        return f"{symbol} (broker: {broker_symbol})"
    return symbol


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _money(value: float, currency: str | None) -> str:
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def _side_icon(side: str) -> str:
    if side == "BUY":
        return "🟢"
    if side == "SELL":
        return "🔴"
    return "⚪"
