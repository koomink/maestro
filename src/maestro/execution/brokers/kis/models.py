from datetime import datetime

from pydantic import BaseModel, Field


class KISCashBalance(BaseModel):
    currency: str = "KRW"
    cash: float
    total_asset_value: float | None = None
    withdrawable_cash: float | None = None


class KISPosition(BaseModel):
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


class KISBuyingPower(BaseModel):
    symbol: str | None = None
    order_price: float | None = None
    cash_buying_power: float
    max_buy_quantity: float | None = None
    source: str


class KISAccountSnapshot(BaseModel):
    account_id: str
    cash: float
    cash_by_currency: dict[str, float] = Field(default_factory=dict)
    buying_power: float
    positions: list[KISPosition] = Field(default_factory=list)
    cash_balance: KISCashBalance | None = None
    buying_power_detail: KISBuyingPower | None = None
    fetched_at: datetime
    source: str

    @property
    def total_value(self) -> float:
        if self.cash_balance is not None and self.cash_balance.total_asset_value is not None:
            return self.cash_balance.total_asset_value
        return self.cash + sum(position.market_value for position in self.positions)


class KISOrderSummary(BaseModel):
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


class KISReadOnlySnapshot(BaseModel):
    account: KISAccountSnapshot
    current_prices: dict[str, float]
    order_fills: list[KISOrderSummary] = Field(default_factory=list)
    unfilled_orders: list[KISOrderSummary] = Field(default_factory=list)

    @property
    def daily_orders(self) -> list[KISOrderSummary]:
        return self.order_fills
