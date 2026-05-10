from maestro.config.models import KISConfig
from maestro.core.ids import new_run_id
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.mock_client import MockKISReadOnlyClient
from maestro.execution.brokers.kis.models import KISReadOnlySnapshot
from maestro.execution.brokers.kis.rest_client import build_kis_rest_readonly_client
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class KISReadOnlyService:
    def __init__(
        self,
        config: KISConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        client: KISReadOnlyClient | None = None,
        instruments: list[TradableInstrument] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.instruments = instruments or []
        self.client = client or self._build_client(config)

    def fetch_and_store_snapshot(self, symbols: list[str]) -> KISReadOnlySnapshot:
        if not self.config.enabled:
            raise ValueError("KIS integration is disabled")
        KISAuthManager(self.config).validate_readonly_credentials()

        run_id = new_run_id()
        account = self.client.get_account_snapshot()
        snapshot = KISReadOnlySnapshot(
            account=account,
            current_prices=self.client.get_current_prices(symbols),
            order_fills=self.client.get_order_fills(),
            unfilled_orders=self.client.get_unfilled_orders(),
        )
        self.state_store.save_broker_account_snapshot(
            run_id,
            account.account_id,
            snapshot.model_dump(mode="json"),
        )
        self.audit_logger.log(
            run_id,
            "kis_readonly_snapshot",
            snapshot.model_dump(mode="json"),
        )
        return snapshot

    def _build_client(self, config: KISConfig) -> KISReadOnlyClient:
        if config.provider == "mock":
            return MockKISReadOnlyClient(config.account_id or "MOCK-ACCOUNT")
        if config.provider == "kis":
            return build_kis_rest_readonly_client(config, self.instruments)
        raise ValueError(f"Unsupported KIS provider: {config.provider}")
