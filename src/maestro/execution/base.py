from pydantic import BaseModel

from maestro.core.enums import OrderSide, OrderStatus, OrderType


class OrderIntent(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    notional: float
    order_type: OrderType = OrderType.MARKET


class ExecutionResult(BaseModel):
    order_id: str
    status: OrderStatus
    filled_quantity: float
    fill_price: float
    filled_notional: float
