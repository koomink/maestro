from datetime import datetime

from pydantic import BaseModel, Field


class KISPosition(BaseModel):
    symbol: str
    quantity: float
    average_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price


class KISAccountSnapshot(BaseModel):
    account_id: str
    cash: float
    buying_power: float
    positions: list[KISPosition] = Field(default_factory=list)
    fetched_at: datetime
    source: str

    @property
    def total_value(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions)


class KISOrderSummary(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: float
    status: str
    submitted_at: datetime


class KISReadOnlySnapshot(BaseModel):
    account: KISAccountSnapshot
    current_prices: dict[str, float]
    daily_orders: list[KISOrderSummary] = Field(default_factory=list)
    unfilled_orders: list[KISOrderSummary] = Field(default_factory=list)
