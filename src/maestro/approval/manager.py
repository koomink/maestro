from datetime import timedelta

from maestro.approval.models import ApprovalDecision, ApprovalRequest
from maestro.config.models import ApprovalConfig
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.core.ids import new_approval_id
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
from maestro.integrations.telegram.bot import (
    TelegramApprovalNotifier,
    TelegramApprovalService,
    TelegramBotAPIClient,
    TelegramBotClient,
)


class ApprovalManager:
    def __init__(
        self,
        config: ApprovalConfig,
        *,
        run_mode: RunMode = RunMode.PAPER,
        instruments: list[TradableInstrument] | None = None,
        telegram_client: TelegramBotClient | None = None,
    ) -> None:
        self.config = config
        self.run_mode = run_mode
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self.notifier = TelegramApprovalNotifier()
        self.telegram_client = telegram_client

    def is_user_allowed(self, user_id: int) -> bool:
        return not self.config.whitelisted_user_ids or user_id in self.config.whitelisted_user_ids

    def request_approval(
        self,
        run_id: str,
        orders: list[OrderIntent],
        risk_modifications: list[str],
        risk_violations: list[str],
        source_strategy_ids: list[str] | None = None,
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
            source_strategy_ids=list(source_strategy_ids or []),
            order_count=len(orders),
            estimated_notional=sum(order.notional for order in orders),
            proposed_orders=[self._proposed_order_payload(order) for order in orders],
            risk_modifications=risk_modifications,
            risk_violations=risk_violations,
        )
        if self.config.provider == "telegram":
            if self.run_mode not in {RunMode.PAPER, RunMode.LIVE_APPROVAL}:
                raise ValueError("Telegram approval requires paper or live_approval mode")
            client = self.telegram_client or TelegramBotAPIClient(
                token_env=self.config.telegram_bot_token_env,
                timeout_seconds=self.config.timeout_seconds,
            )
            service = TelegramApprovalService(
                client=client,
                chat_ids=self.config.telegram_allowed_chat_ids,
                allowed_user_ids=self.config.whitelisted_user_ids,
                poll_interval_seconds=self.config.telegram_poll_interval_seconds,
            )
            return request, *service.request_decision(request)

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

    def _proposed_order_payload(self, order: OrderIntent) -> dict:
        payload = order.model_dump(mode="json")
        instrument = self.instruments.get(order.symbol)
        if instrument is None:
            return payload
        if instrument.name:
            payload["name"] = instrument.name
        payload["broker_symbol"] = instrument.broker_symbol
        payload["currency"] = instrument.currency.value
        payload["broker_product"] = instrument.broker_product.value
        if instrument.exchange_code is not None:
            payload["exchange_code"] = instrument.exchange_code.value
        return payload
