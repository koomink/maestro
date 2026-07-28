from typing import Any

from pydantic import BaseModel, Field

from maestro.core.enums import BrokerProduct, Currency, OrderSide, OrderStatus, OrderType


class OrderIntent(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    notional: float
    order_type: OrderType = OrderType.MARKET
    currency: Currency | None = None
    sleeve: str | None = None
    account_id: str | None = None
    broker_product: BrokerProduct | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def execution_sleeve(self) -> str | None:
        """Attribution bucket for this order, distinct from the currency `sleeve`."""
        execution_sleeve = self.metadata.get("execution_sleeve")
        return str(execution_sleeve) if execution_sleeve is not None else None


class ExecutionResult(BaseModel):
    order_id: str
    status: OrderStatus
    filled_quantity: float
    fill_price: float
    filled_notional: float
