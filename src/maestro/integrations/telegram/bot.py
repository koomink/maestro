from maestro.approval.models import ApprovalRequest
from maestro.integrations.telegram.formatter import format_approval_request


class TelegramApprovalNotifier:
    """Phase 2 notifier stub.

    It formats the Telegram approval payload without making network calls.
    Real Bot API delivery can replace this class without changing orchestration.
    """

    def send_approval_request(self, request: ApprovalRequest) -> str:
        return format_approval_request(request)
