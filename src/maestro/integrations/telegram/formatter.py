from maestro.approval.models import ApprovalRequest


def format_approval_request(request: ApprovalRequest) -> str:
    lines = [
        "Maestro approval request",
        f"approval_id: {request.approval_id}",
        f"run_id: {request.run_id}",
        f"orders: {request.order_count}",
        f"estimated_notional: {request.estimated_notional:.2f}",
        f"expires_at: {request.expires_at.isoformat()}",
    ]
    if request.risk_modifications:
        lines.append("risk_modifications:")
        lines.extend(f"- {item}" for item in request.risk_modifications)
    if request.risk_violations:
        lines.append("risk_violations:")
        lines.extend(f"- {item}" for item in request.risk_violations)
    if request.proposed_orders:
        lines.append("proposed_orders:")
        for order in request.proposed_orders:
            side = order.get("side", "unknown")
            notional = float(order.get("notional", 0.0))
            label = _order_label(order)
            lines.append(f"- {side} {label} notional={notional:.2f}")
    lines.extend(
        [
            "",
            "Tap Approve or Reject, or reply manually:",
            f"Reply with: approve {request.approval_id}",
            f"Or reply with: reject {request.approval_id}",
        ]
    )
    return "\n".join(lines)


def _order_label(order: dict) -> str:
    symbol = str(order.get("symbol") or "unknown")
    name = order.get("name")
    broker_symbol = order.get("broker_symbol")
    if isinstance(name, str) and name:
        return f"{symbol} {name}"
    if isinstance(broker_symbol, str) and broker_symbol and broker_symbol != symbol:
        return f"{symbol} ({broker_symbol})"
    return symbol
