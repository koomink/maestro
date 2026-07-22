from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class BrokerCashBalance(BaseModel):
    currency: str = "KRW"
    cash: float
    total_asset_value: float | None = None
    withdrawable_cash: float | None = None
    settled_cash: float | None = None
    next_day_cash: float | None = None
    projected_settlement_cash: float | None = None
    transaction_costs_today: float | None = None
    available_cash: float | None = None
    reserved_cash: float | None = None
    available_cash_by_currency: dict[str, float] = Field(default_factory=dict)
    reserved_cash_by_currency: dict[str, float] = Field(default_factory=dict)
    cash_semantics: str | None = None


class BrokerPosition(BaseModel):
    symbol: str
    quantity: float
    average_price: float
    current_price: float
    currency: str | None = None
    name: str | None = None
    unrealized_pnl: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price


class BrokerBuyingPower(BaseModel):
    symbol: str | None = None
    order_price: float | None = None
    cash_buying_power: float
    max_buy_quantity: float | None = None
    source: str


class BrokerAccountSnapshot(BaseModel):
    account_id: str
    cash: float
    cash_by_currency: dict[str, float] = Field(default_factory=dict)
    buying_power: float
    positions: list[BrokerPosition] = Field(default_factory=list)
    cash_balance: BrokerCashBalance | None = None
    buying_power_detail: BrokerBuyingPower | None = None
    # Account-level daily PnL as reported by the broker. The live daily-loss
    # gate (`maestro.orchestration.live_gates`) reads these keys from the
    # stored snapshot payload; without them an all-cash account has no
    # per-position unrealized PnL to fall back on and the gate fails closed
    # with `broker_pnl_unavailable`.
    daily_pnl: float | None = None
    daily_pnl_by_currency: dict[str, float] | None = None
    fetched_at: datetime
    source: str

    @property
    def total_value(self) -> float:
        if self.cash_balance is not None and self.cash_balance.total_asset_value is not None:
            return self.cash_balance.total_asset_value
        return self.cash + sum(position.market_value for position in self.positions)


class BrokerOrderSummary(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: float
    status: str
    submitted_at: datetime
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    name: str | None = None
    raw_status: str | None = None
    currency: str | None = None
    remaining_quantity: float | None = None
    limit_price: float | None = None
    reserved_cash: float | None = None
    estimated_commission: float | None = None


class BrokerReadOnlySnapshot(BaseModel):
    account: BrokerAccountSnapshot
    current_prices: dict[str, float]
    order_fills: list[BrokerOrderSummary] = Field(default_factory=list)
    unfilled_orders: list[BrokerOrderSummary] = Field(default_factory=list)

    @property
    def daily_orders(self) -> list[BrokerOrderSummary]:
        return self.order_fills


class BrokerReadOnlyClient(ABC):
    @abstractmethod
    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError

    @abstractmethod
    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
    ) -> BrokerBuyingPower:
        raise NotImplementedError

    @abstractmethod
    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError

    @abstractmethod
    def get_order_fills(self) -> list[BrokerOrderSummary]:
        raise NotImplementedError

    @abstractmethod
    def get_unfilled_orders(self) -> list[BrokerOrderSummary]:
        raise NotImplementedError


class BrokerReadOnlyService:
    def __init__(
        self,
        client: BrokerReadOnlyClient,
        state_store: StateStore,
        audit_logger: AuditLogger,
        *,
        logical_account_id: str | None = None,
        audit_event_type: str = "broker_readonly_snapshot",
    ) -> None:
        self.client = client
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.logical_account_id = logical_account_id
        self.audit_event_type = audit_event_type

    def fetch_and_store_snapshot(
        self,
        symbols: list[str],
        run_id: str | None = None,
    ) -> BrokerReadOnlySnapshot:
        run_id = run_id or new_run_id()
        account = self.client.get_account_snapshot()
        current_prices = _prices_with_position_prices(
            account,
            self.client.get_current_prices(symbols),
        )
        snapshot = BrokerReadOnlySnapshot(
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
        self.audit_logger.log(run_id, self.audit_event_type, payload)
        return snapshot

    def _snapshot_payload(self, snapshot: BrokerReadOnlySnapshot) -> dict:
        payload = snapshot.model_dump(mode="json")
        if self.logical_account_id:
            payload["account_id"] = self.logical_account_id
            payload["broker_account_id"] = snapshot.account.account_id
        return payload


def _prices_with_position_prices(
    account: BrokerAccountSnapshot,
    current_prices: dict[str, float],
) -> dict[str, float]:
    prices = dict(current_prices)
    for position in account.positions:
        if position.current_price > 0:
            prices.setdefault(position.symbol, position.current_price)
    return prices


__all__ = [
    "BrokerAccountSnapshot",
    "BrokerBuyingPower",
    "BrokerCashBalance",
    "BrokerOrderSummary",
    "BrokerPosition",
    "BrokerReadOnlyClient",
    "BrokerReadOnlyService",
    "BrokerReadOnlySnapshot",
]
