from dataclasses import dataclass

from maestro.config.models import MaestroConfig
from maestro.core.enums import BrokerProduct, Currency
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.live_order_client import build_kis_rest_live_order_client
from maestro.execution.live_orders import (
    BrokerReconciliationRunner,
    LiveOrderCancelClient,
    LiveOrderCancellationService,
    LiveOrderClient,
    LiveOrderLifecycleService,
    LiveOrderNotificationClient,
    LiveOrderPreSubmitValidator,
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
        broker_products=config.kis.effective_broker_products(),
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
    cancel_adapter = cancel_client or _build_cancel_client(config, broker_client)
    cancel_service = (
        LiveOrderCancellationService(state_store, audit_logger, cancel_adapter)
        if cancel_adapter is not None
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
        products = config.kis.effective_broker_products()
        if len(products) > 1:
            return ProductRoutingKISLiveOrderClient(config)
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
        if len(config.kis.effective_broker_products()) > 1:
            return ProductRoutingKISLiveOrderClient(config)
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


def _build_cancel_client(
    config: MaestroConfig,
    live_order_client: LiveOrderClient,
) -> LiveOrderCancelClient | None:
    if isinstance(live_order_client, LiveOrderCancelClient):
        return live_order_client
    if not live_order_client.__class__.__module__.startswith("maestro.execution.brokers.kis"):
        return None
    if config.kis.provider != "kis":
        return None
    if len(config.kis.effective_broker_products()) > 1:
        return None
    candidate = build_kis_rest_live_order_client(config.kis, config.universe.instruments)
    if isinstance(candidate, LiveOrderCancelClient):
        return candidate
    return None


class ProductRoutingKISLiveOrderClient(
    LiveOrderClient,
    LiveOrderStatusClient,
    LiveOrderPreSubmitValidator,
):
    def __init__(self, config: MaestroConfig) -> None:
        self.config = config
        self.instruments = {
            instrument.symbol: instrument for instrument in config.universe.instruments
        }
        self.clients = {
            product: build_kis_rest_live_order_client(
                config.kis.model_copy(update={"broker_product": product, "broker_products": []}),
                _instruments_for_product(config.universe.instruments, product),
            )
            for product in config.kis.effective_broker_products()
        }
        self.submitted_products: dict[str, BrokerProduct] = {}

    def submit_limit_order(self, request) -> object:
        client = self._client_for_request(request)
        result = client.submit_limit_order(request)
        if result.broker_order is not None and result.broker_order.broker_product is not None:
            self.submitted_products[result.broker_order.broker_order_id] = (
                result.broker_order.broker_product
            )
        return result

    def validate_pre_submit_order(self, request) -> None:
        client = self._client_for_request(request)
        if isinstance(client, LiveOrderPreSubmitValidator):
            client.validate_pre_submit_order(request)

    def get_order_status(self, broker_order_id):
        product = broker_order_id.broker_product or self.submitted_products.get(
            broker_order_id.broker_order_id
        )
        if product is None:
            raise ValueError("KIS multi-product status requires broker_product")
        client = self.clients[product]
        if not isinstance(client, LiveOrderStatusClient):
            raise ValueError(f"KIS client does not support order status: {product}")
        return client.get_order_status(broker_order_id)

    def _client_for_request(self, request):
        product = request.broker_product
        if product is None:
            instrument = self.instruments.get(request.symbol)
            product = instrument.broker_product if instrument else None
        if product is None:
            raise ValueError(f"Cannot route KIS order without broker product: {request.symbol}")
        if product not in self.clients:
            raise ValueError(f"KIS broker product is not enabled: {product}")
        return self.clients[product]


def _instruments_for_product(
    instruments: list[TradableInstrument],
    product: BrokerProduct,
) -> list[TradableInstrument]:
    return [instrument for instrument in instruments if instrument.broker_product == product]
