from maestro.config.models import KISConfig
from maestro.core.ids import new_run_id
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.mock_client import MockKISReadOnlyClient
from maestro.execution.brokers.kis.models import KISAccountSnapshot, KISReadOnlySnapshot
from maestro.execution.brokers.kis.readonly_client import build_kis_rest_readonly_client
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
        logical_account_id: str | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.instruments = instruments or []
        self.logical_account_id = logical_account_id
        self.client = client or self._build_client(config)

    def fetch_and_store_snapshot(
        self,
        symbols: list[str],
        run_id: str | None = None,
    ) -> KISReadOnlySnapshot:
        if not self.config.enabled:
            raise ValueError("KIS integration is disabled")
        KISAuthManager(self.config).validate_readonly_credentials()

        if self.client is None and len(self.config.effective_broker_products()) > 1:
            return self._fetch_multi_product_snapshot(symbols, run_id=run_id)

        run_id = run_id or new_run_id()
        account = self.client.get_account_snapshot()
        current_prices = _prices_with_position_prices(
            account,
            self.client.get_current_prices(symbols),
        )
        snapshot = KISReadOnlySnapshot(
            account=account,
            current_prices=current_prices,
            order_fills=self.client.get_order_fills(),
            unfilled_orders=self.client.get_unfilled_orders(),
        )
        payload = self._snapshot_payload(snapshot)
        self.state_store.save_broker_account_snapshot(
            run_id,
            self.logical_account_id or account.account_id,
            payload,
        )
        self.audit_logger.log(
            run_id,
            "kis_readonly_snapshot",
            payload,
        )
        return snapshot

    def _build_client(
        self,
        config: KISConfig,
        instruments: list[TradableInstrument] | None = None,
        broker_product=None,
    ) -> KISReadOnlyClient:
        products = (
            [broker_product]
            if broker_product is not None
            else config.effective_broker_products()
        )
        if len(products) != 1:
            return None
        if config.provider == "mock":
            return MockKISReadOnlyClient(config.account_id or "MOCK-ACCOUNT")
        if config.provider == "kis":
            return build_kis_rest_readonly_client(
                config,
                instruments or self.instruments,
                broker_product=products[0],
            )
        raise ValueError(f"Unsupported KIS provider: {config.provider}")

    def _fetch_multi_product_snapshot(
        self,
        symbols: list[str],
        run_id: str | None = None,
    ) -> KISReadOnlySnapshot:
        run_id = run_id or new_run_id()
        snapshots = []
        for product in self.config.effective_broker_products():
            product_instruments = self._instruments_for_product(product)
            client = self._build_client(
                self.config,
                product_instruments,
                broker_product=product,
            )
            product_symbols = self._symbols_for_product(symbols, product)
            account = client.get_account_snapshot()
            current_prices = _prices_with_position_prices(
                account,
                client.get_current_prices(product_symbols),
            )
            snapshots.append(
                KISReadOnlySnapshot(
                    account=account,
                    current_prices=current_prices,
                    order_fills=client.get_order_fills(),
                    unfilled_orders=client.get_unfilled_orders(),
                )
            )
        snapshot = _merge_snapshots(snapshots)
        payload = self._snapshot_payload(snapshot)
        self.state_store.save_broker_account_snapshot(
            run_id,
            self.logical_account_id or snapshot.account.account_id,
            payload,
        )
        self.audit_logger.log(run_id, "kis_readonly_snapshot", payload)
        return snapshot

    def _symbols_for_product(self, symbols: list[str], product) -> list[str]:
        by_symbol = {instrument.symbol: instrument for instrument in self.instruments}
        selected = [
            symbol
            for symbol in symbols
            if by_symbol.get(symbol) is None or by_symbol[symbol].broker_product == product
        ]
        return selected

    def _instruments_for_product(self, product) -> list[TradableInstrument]:
        return [
            instrument for instrument in self.instruments if instrument.broker_product == product
        ]

    def _snapshot_payload(self, snapshot: KISReadOnlySnapshot) -> dict:
        payload = snapshot.model_dump(mode="json")
        if self.logical_account_id:
            payload["account_id"] = self.logical_account_id
            payload["broker_account_id"] = snapshot.account.account_id
        return payload


def _merge_snapshots(snapshots: list[KISReadOnlySnapshot]) -> KISReadOnlySnapshot:
    if not snapshots:
        raise ValueError("No KIS snapshots to merge")
    cash_by_currency: dict[str, float] = {}
    positions = []
    current_prices = {}
    order_fills = []
    unfilled_orders = []
    for snapshot in snapshots:
        cash_by_currency.update(snapshot.account.cash_by_currency)
        positions.extend(snapshot.account.positions)
        current_prices.update(snapshot.current_prices)
        order_fills.extend(snapshot.order_fills)
        unfilled_orders.extend(snapshot.unfilled_orders)
    account = KISAccountSnapshot(
        account_id=snapshots[0].account.account_id,
        cash=sum(cash_by_currency.values()),
        cash_by_currency=cash_by_currency,
        buying_power=sum(snapshot.account.buying_power for snapshot in snapshots),
        positions=positions,
        cash_balance=None,
        buying_power_detail=None,
        fetched_at=snapshots[0].account.fetched_at,
        source="kis_multi_product_readonly",
    )
    return KISReadOnlySnapshot(
        account=account,
        current_prices=current_prices,
        order_fills=order_fills,
        unfilled_orders=unfilled_orders,
    )


def _prices_with_position_prices(
    account: KISAccountSnapshot,
    current_prices: dict[str, float],
) -> dict[str, float]:
    prices = dict(current_prices)
    for position in account.positions:
        if position.current_price > 0:
            prices.setdefault(position.symbol, position.current_price)
    return prices
