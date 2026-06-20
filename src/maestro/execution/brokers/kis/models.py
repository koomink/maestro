from pydantic import Field

from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerBuyingPower,
    BrokerCashBalance,
    BrokerOrderSummary,
    BrokerPosition,
    BrokerReadOnlySnapshot,
)


class KISCashBalance(BrokerCashBalance):
    pass


class KISPosition(BrokerPosition):
    pass


class KISBuyingPower(BrokerBuyingPower):
    pass


class KISAccountSnapshot(BrokerAccountSnapshot):
    positions: list[KISPosition] = Field(default_factory=list)
    cash_balance: KISCashBalance | None = None
    buying_power_detail: KISBuyingPower | None = None


class KISOrderSummary(BrokerOrderSummary):
    pass


class KISReadOnlySnapshot(BrokerReadOnlySnapshot):
    account: KISAccountSnapshot
    order_fills: list[KISOrderSummary] = Field(default_factory=list)
    unfilled_orders: list[KISOrderSummary] = Field(default_factory=list)
