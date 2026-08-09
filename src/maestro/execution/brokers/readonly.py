from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import date, datetime, timedelta

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


class BuyingPowerCurrencyUnavailable(ValueError):
    """No buying-power figure exists for the currency an order is denominated in.

    A multi-currency account holds a separate pot per currency, and one pot says
    nothing about another: a KRW balance cannot fund a dollar purchase. Rather
    than let a caller stand in the account's representative figure for the
    currency it actually needs, adapters raise this and the capacity gate blocks.
    """

    def __init__(self, currency: str | None, available_currencies: Collection[str]) -> None:
        self.currency = currency
        self.available_currencies = tuple(available_currencies)
        named = currency or "an unnamed currency"
        reported = ", ".join(self.available_currencies) or "nothing"
        super().__init__(
            f"Broker buying power for {named} is unavailable; the account reports {reported}"
        )


class BrokerBuyingPower(BaseModel):
    symbol: str | None = None
    order_price: float | None = None
    cash_buying_power: float
    # Which currency `cash_buying_power` is denominated in. A figure without one
    # cannot be checked against an order's currency, so leaving this None means
    # "trust the caller's routing" — every adapter here fills it in.
    currency: str | None = None
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
    # Legacy cash fields remain serialized for compatibility.  Accounting
    # consumers must use ledger_cash_by_currency when it is present; a null
    # value explicitly means that the broker exposes no authoritative ledger.
    ledger_cash_by_currency: dict[str, float] | None = None
    buying_power_by_currency: dict[str, float] = Field(default_factory=dict)
    broker_cash_verification: str = "unavailable"
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
    cumulative_commission: float | None = None
    cumulative_tax: float | None = None


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
        currency: str | None = None,
    ) -> BrokerBuyingPower:
        """Capacity for a buy of `symbol`, denominated in `currency`.

        Implementations that hold more than one currency must answer for the one
        asked for and raise `BuyingPowerCurrencyUnavailable` when they cannot.
        """
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
        if self.client.__class__.__name__ == "TossReadOnlyClient":
            # Import locally to avoid a model/backfill import cycle.
            from maestro.execution.brokers.toss.order_history_backfill import (
                TossOrderHistoryBackfillService,
            )

            account_id = self.logical_account_id or account.account_id
            from_date = date.today() - timedelta(days=7)
            baseline_date: date | None = None
            for row in self.state_store.list_system_events_by_type(
                "ledger_opening_baseline",
                limit=2000,
            ):
                baseline = row.get("payload") or {}
                if str(baseline.get("account_id") or "") != account_id:
                    continue
                value = baseline.get("effective_at") or row.get("created_at")
                if value:
                    baseline_date = datetime.fromisoformat(
                        str(value).replace("Z", "+00:00")
                    ).date()
                    from_date = baseline_date
                break
            for row in self.state_store.list_system_events_by_type(
                "broker_order_history_backfill",
                limit=2000,
            ):
                previous = row.get("payload") or {}
                if str(previous.get("account_id") or "") != account_id:
                    continue
                previous_to = previous.get("to_date")
                if previous_to:
                    overlap_start = date.fromisoformat(str(previous_to)) - timedelta(days=2)
                    from_date = max(baseline_date or from_date, overlap_start)
                break
            TossOrderHistoryBackfillService(
                self.client,
                self.state_store,
                self.audit_logger,
            ).backfill(
                account_id,
                from_date=from_date,
                to_date=date.today(),
                run_id=run_id,
            )
            payload["order_history_backfill_run_id"] = run_id
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


def buying_power_for_currency(
    account: BrokerAccountSnapshot,
    currency: str | None,
    *,
    symbol: str | None = None,
    order_price: float | None = None,
) -> BrokerBuyingPower:
    """Pick one currency's buying power out of a multi-currency account.

    The account-level `buying_power_detail` carries whichever currency the
    adapter treats as primary, which is the wrong number for every other
    currency the account holds. Read the per-currency breakdown instead, and
    fail closed rather than substitute a figure from a currency nobody asked
    about.

    An unnamed currency is only answerable when the account holds exactly one —
    with two pots there is no way to tell which the caller meant.
    """
    by_currency = account.buying_power_by_currency
    if currency is None:
        if len(by_currency) != 1:
            raise BuyingPowerCurrencyUnavailable(None, by_currency)
        currency = next(iter(by_currency))
    if currency not in by_currency:
        raise BuyingPowerCurrencyUnavailable(currency, by_currency)
    return BrokerBuyingPower(
        symbol=symbol,
        order_price=order_price,
        cash_buying_power=by_currency[currency],
        currency=currency,
        source=account.source,
    )


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
    "BuyingPowerCurrencyUnavailable",
    "buying_power_for_currency",
]
