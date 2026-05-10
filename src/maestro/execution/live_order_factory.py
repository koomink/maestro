from dataclasses import dataclass

from maestro.config.models import MaestroConfig
from maestro.core.enums import Currency
from maestro.execution.brokers.kis.rest_client import build_kis_rest_live_order_client
from maestro.execution.live_orders import (
    BrokerReconciliationRunner,
    LiveOrderCancelClient,
    LiveOrderCancellationService,
    LiveOrderClient,
    LiveOrderLifecycleService,
    LiveOrderNotificationClient,
    LiveOrderSafetyService,
    LiveOrderStatusClient,
    LiveOrderStatusService,
    LiveOrderWorkflowService,
    PartialFillReconciliationService,
)
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.integrations.telegram.bot import (
    TelegramBotAPIClient,
    TelegramBotClient,
    TelegramLiveOrderNotificationClient,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


@dataclass
class LiveApprovalDependencies:
    state_store: StateStore
    audit_logger: AuditLogger
    safety_service: LiveOrderSafetyService
    status_service: LiveOrderStatusService
    fill_reconciliation_service: PartialFillReconciliationService
    workflow_service: LiveOrderWorkflowService
    lifecycle_service: LiveOrderLifecycleService
    broker_reconciliation_service: BrokerReconciliationRunner | None = None
    notification_client: LiveOrderNotificationClient | None = None
    cancel_service: LiveOrderCancellationService | None = None


def build_live_approval_dependencies(
    config: MaestroConfig,
    state_store: StateStore,
    audit_logger: AuditLogger,
    *,
    live_order_client: LiveOrderClient | None = None,
    status_client: LiveOrderStatusClient | None = None,
    cancel_client: LiveOrderCancelClient | None = None,
    broker_reconciliation_service: BrokerReconciliationRunner | None = None,
    notification_client: LiveOrderNotificationClient | None = None,
    telegram_client: TelegramBotClient | None = None,
) -> LiveApprovalDependencies:
    broker_client = live_order_client or _build_live_order_client(config)
    order_status_client = status_client or _build_status_client(config, broker_client)
    notifier = notification_client or _build_notification_client(config, telegram_client)
    broker_reconciliation = broker_reconciliation_service
    if broker_reconciliation is None and config.execution.require_reconciliation_pass:
        broker_reconciliation = BrokerReconciliationService(
            config.reconciliation,
            state_store,
            audit_logger,
        )

    safety_service = LiveOrderSafetyService(
        config.execution,
        state_store,
        audit_logger,
        broker_client,
        instruments=config.universe.instruments,
        broker_product=config.kis.broker_product,
        base_currency=Currency(config.portfolio.base_currency),
    )
    status_service = LiveOrderStatusService(state_store, audit_logger, order_status_client)
    fill_reconciliation_service = PartialFillReconciliationService(state_store, audit_logger)
    workflow_service = LiveOrderWorkflowService(
        state_store,
        audit_logger,
        safety_service,
        status_service,
        fill_reconciliation_service,
        broker_reconciliation,
    )
    lifecycle_service = LiveOrderLifecycleService(
        config.execution,
        state_store,
        audit_logger,
        safety_service,
        status_service,
        fill_reconciliation_service,
        broker_reconciliation,
        notifier,
    )
    cancel_service = (
        LiveOrderCancellationService(state_store, audit_logger, cancel_client)
        if cancel_client is not None
        else None
    )
    return LiveApprovalDependencies(
        state_store=state_store,
        audit_logger=audit_logger,
        safety_service=safety_service,
        status_service=status_service,
        fill_reconciliation_service=fill_reconciliation_service,
        workflow_service=workflow_service,
        lifecycle_service=lifecycle_service,
        broker_reconciliation_service=broker_reconciliation,
        notification_client=notifier,
        cancel_service=cancel_service,
    )


def _build_live_order_client(config: MaestroConfig) -> LiveOrderClient:
    if config.kis.provider == "kis":
        return build_kis_rest_live_order_client(config.kis, config.universe.instruments)
    raise ValueError(
        "live_approval requires a real KIS live order client or an injected fake client"
    )


def _build_status_client(
    config: MaestroConfig,
    live_order_client: LiveOrderClient,
) -> LiveOrderStatusClient:
    if isinstance(live_order_client, LiveOrderStatusClient):
        return live_order_client
    if config.kis.provider == "kis":
        status_client = build_kis_rest_live_order_client(
            config.kis,
            config.universe.instruments,
        )
        if isinstance(status_client, LiveOrderStatusClient):
            return status_client
    raise ValueError("live_approval requires an injected fake status client")


def _build_notification_client(
    config: MaestroConfig,
    telegram_client: TelegramBotClient | None,
) -> LiveOrderNotificationClient | None:
    if config.approval.provider != "telegram":
        return None
    client = telegram_client or TelegramBotAPIClient(
        token_env=config.approval.telegram_bot_token_env,
        timeout_seconds=config.approval.timeout_seconds,
    )
    return TelegramLiveOrderNotificationClient(
        client=client,
        chat_ids=config.approval.telegram_allowed_chat_ids,
    )
