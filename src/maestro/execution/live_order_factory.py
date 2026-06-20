from dataclasses import dataclass

from maestro.config.models import MaestroConfig
from maestro.core.enums import BrokerProduct, Currency
from maestro.core.instruments import TradableInstrument
from maestro.execution.broker_router import BrokerAccountRouter
from maestro.execution.brokers.kis.live_order_client import build_kis_rest_live_order_client
from maestro.execution.brokers.readonly_factory import build_broker_readonly_service
from maestro.execution.brokers.toss.live_order_client import TossLiveOrderClient
from maestro.execution.live_order_modification import LiveOrderModificationService
from maestro.execution.live_order_ports import LiveOrderModifyClient
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
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
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
    modify_service: LiveOrderModificationService | None = None


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
    account_id: str | None = None,
    signal_run_id: str | None = None,
) -> LiveApprovalDependencies:
    broker_client = live_order_client or _build_live_order_client(config, account_id=account_id)
    order_status_client = status_client or _build_status_client(
        config,
        broker_client,
        account_id=account_id,
    )
    notifier = notification_client or _build_notification_client(config, telegram_client)
    broker_reconciliation = broker_reconciliation_service
    if broker_reconciliation is None and config.execution.require_reconciliation_pass:
        snapshot_refresher = _build_broker_snapshot_refresher(
            config,
            state_store,
            audit_logger,
            account_id=account_id,
        )
        broker_reconciliation = BrokerReconciliationService(
            config.reconciliation,
            state_store,
            audit_logger,
            snapshot_refresher=snapshot_refresher,
            signal_run_id=signal_run_id,
            account_ids=[account_id] if account_id else None,
        )

    account = BrokerAccountRouter(config).account(account_id)
    is_toss = account is not None and account.broker == "toss"
    attribution_validator = _build_attribution_validator(
        config,
        state_store,
        audit_logger,
        account_id=account_id,
    )
    safety_service = LiveOrderSafetyService(
        config.execution,
        state_store,
        audit_logger,
        broker_client,
        instruments=config.universe.instruments,
        broker_products=(
            []
            if is_toss
            else _kis_config_for_account(config, account_id).effective_broker_products()
        ),
        base_currency=None if is_toss else Currency(config.portfolio.base_currency),
        account_attribution_validator=(
            (lambda request: attribution_validator(request.account_id))
            if attribution_validator is not None
            else None
        ),
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
    cancel_adapter = cancel_client or _build_cancel_client(
        config,
        broker_client,
        account_id=account_id,
    )
    cancel_service = (
        LiveOrderCancellationService(state_store, audit_logger, cancel_adapter)
        if cancel_adapter is not None
        else None
    )
    modify_adapter = (
        broker_client if isinstance(broker_client, LiveOrderModifyClient) else None
    )
    modify_service = (
        LiveOrderModificationService(
            config.execution,
            state_store,
            audit_logger,
            modify_adapter,
            attribution_validator=attribution_validator,
        )
        if modify_adapter is not None
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
        modify_service=modify_service,
    )


def _build_live_order_client(
    config: MaestroConfig,
    *,
    account_id: str | None = None,
) -> LiveOrderClient:
    account = BrokerAccountRouter(config).account(account_id)
    if account is not None and account.broker == "toss":
        return TossLiveOrderClient(account, config.universe.instruments)
    kis_config = _kis_config_for_account(config, account_id)
    if kis_config.provider == "kis":
        products = kis_config.effective_broker_products()
        if len(products) > 1:
            return ProductRoutingKISLiveOrderClient(config, kis_config=kis_config)
        return build_kis_rest_live_order_client(
            kis_config,
            config.universe.instruments,
            broker_product=products[0],
        )
    raise ValueError(
        "live_approval requires a real KIS live order client or an injected fake client"
    )


def _build_broker_snapshot_refresher(
    config: MaestroConfig,
    state_store: StateStore,
    audit_logger: AuditLogger,
    *,
    account_id: str | None = None,
):
    try:
        service = build_broker_readonly_service(
            config,
            state_store,
            audit_logger,
            account_id=account_id,
        )
    except Exception:
        return None

    def refresh() -> None:
        service.fetch_and_store_snapshot(config.portfolio.allowed_symbols)

    return refresh


def _build_attribution_validator(
    config: MaestroConfig,
    state_store: StateStore,
    audit_logger: AuditLogger,
    *,
    account_id: str | None,
):
    if not account_id or account_id not in config.account_strategy_targets:
        return None

    def validate(request_account_id: str | None) -> None:
        if request_account_id != account_id:
            raise ValueError(
                f"Account attribution validation expected account_id={account_id}"
            )
        snapshot = None
        for row in state_store.list_broker_account_snapshots(limit=1000):
            payload = row.get("payload") or {}
            logical_account_id = str(payload.get("account_id") or row.get("account_id") or "")
            if logical_account_id == account_id:
                snapshot = row
                break
        if snapshot is None:
            raise ValueError(
                f"Account attribution requires a broker snapshot for account_id={account_id}"
            )
        account = snapshot["payload"]["account"]
        AccountAttributionReconciliationService(
            state_store,
            audit_logger,
        ).require_ready(
            account_id=account_id,
            broker_snapshot_id=int(snapshot["id"]),
            broker_positions={
                str(position["symbol"]): float(position["quantity"])
                for position in account.get("positions", [])
            },
        )

    return validate


def _build_status_client(
    config: MaestroConfig,
    live_order_client: LiveOrderClient,
    *,
    account_id: str | None = None,
) -> LiveOrderStatusClient:
    if isinstance(live_order_client, LiveOrderStatusClient):
        return live_order_client
    kis_config = _kis_config_for_account(config, account_id)
    if kis_config.provider == "kis":
        if len(kis_config.effective_broker_products()) > 1:
            return ProductRoutingKISLiveOrderClient(config, kis_config=kis_config)
        products = kis_config.effective_broker_products()
        status_client = build_kis_rest_live_order_client(
            kis_config,
            config.universe.instruments,
            broker_product=products[0],
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
    *,
    account_id: str | None = None,
) -> LiveOrderCancelClient | None:
    if isinstance(live_order_client, LiveOrderCancelClient):
        return live_order_client
    if not live_order_client.__class__.__module__.startswith("maestro.execution.brokers.kis"):
        return None
    kis_config = _kis_config_for_account(config, account_id)
    if kis_config.provider != "kis":
        return None
    if len(kis_config.effective_broker_products()) > 1:
        return None
    products = kis_config.effective_broker_products()
    candidate = build_kis_rest_live_order_client(
        kis_config,
        config.universe.instruments,
        broker_product=products[0],
    )
    if isinstance(candidate, LiveOrderCancelClient):
        return candidate
    return None


class ProductRoutingKISLiveOrderClient(
    LiveOrderClient,
    LiveOrderStatusClient,
    LiveOrderPreSubmitValidator,
):
    def __init__(self, config: MaestroConfig, *, kis_config=None) -> None:
        self.config = config
        self.kis_config = kis_config or config.kis
        self.instruments = {
            instrument.symbol: instrument for instrument in config.universe.instruments
        }
        self.clients = {
            product: build_kis_rest_live_order_client(
                self.kis_config,
                _instruments_for_product(config.universe.instruments, product),
                broker_product=product,
            )
            for product in self.kis_config.effective_broker_products()
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


def _kis_config_for_account(config: MaestroConfig, account_id: str | None = None):
    return BrokerAccountRouter(config).kis_config(account_id)
