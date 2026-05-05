from datetime import timedelta

from maestro.approval.models import ApprovalDecision, ApprovalRequest
from maestro.config.models import ApprovalConfig
from maestro.core.clock import utc_now
from maestro.core.ids import new_approval_id
from maestro.execution.base import OrderIntent
from maestro.integrations.telegram.bot import TelegramApprovalNotifier


class ApprovalManager:
    def __init__(self, config: ApprovalConfig) -> None:
        self.config = config
        self.notifier = TelegramApprovalNotifier()

    def is_user_allowed(self, user_id: int) -> bool:
        return not self.config.whitelisted_user_ids or user_id in self.config.whitelisted_user_ids

    def request_approval(
        self,
        run_id: str,
        orders: list[OrderIntent],
        risk_modifications: list[str],
        risk_violations: list[str],
    ) -> tuple[ApprovalRequest | None, ApprovalDecision | None, str | None]:
        if not self.config.enabled or not self.config.require_approval:
            return None, None, None

        now = utc_now()
        request = ApprovalRequest(
            approval_id=new_approval_id(),
            run_id=run_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.timeout_seconds),
            channel=self.config.provider,
            order_count=len(orders),
            estimated_notional=sum(order.notional for order in orders),
            proposed_orders=[order.model_dump(mode="json") for order in orders],
            risk_modifications=risk_modifications,
            risk_violations=risk_violations,
        )
        message = self.notifier.send_approval_request(request)
        decision = ApprovalDecision(
            approval_id=request.approval_id,
            run_id=run_id,
            status=self.config.default_decision,
            decided_at=utc_now(),
            decided_by=f"{self.config.provider}:default_decision",
            reason="Configured Phase 2 approval stub decision.",
        )
        return request, decision, message
