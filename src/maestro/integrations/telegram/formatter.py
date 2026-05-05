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
    return "\n".join(lines)
