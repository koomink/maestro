from pydantic import BaseModel, Field

from maestro.core.enums import AssetType, BrokerProduct, Currency, ExchangeCode, MarketRegion


class CanonicalSymbol(BaseModel):
    symbol: str = Field(min_length=1)


class BrokerSymbolMapping(BaseModel):
    broker: str
    broker_product: BrokerProduct
    broker_symbol: str = Field(min_length=1)
    exchange_code: ExchangeCode | None = None


class TradableInstrument(BaseModel):
    symbol: str = Field(min_length=1)
    name: str | None = None
    asset_type: AssetType
    region: MarketRegion
    currency: Currency
    broker: str
    broker_product: BrokerProduct
    broker_symbol: str = Field(min_length=1)
    exchange_code: ExchangeCode | None = None
    quantity_step: float = Field(gt=0)
    price_tick: float = Field(gt=0)
    min_order_quantity: float = Field(default=1.0, gt=0)
    min_order_notional: float = Field(default=0.0, ge=0)
    asset_tags: list[str] = Field(default_factory=list)

    def broker_mapping(self) -> BrokerSymbolMapping:
        return BrokerSymbolMapping(
            broker=self.broker,
            broker_product=self.broker_product,
            broker_symbol=self.broker_symbol,
            exchange_code=self.exchange_code,
        )

    @property
    def price_precision(self) -> float:
        return self.price_tick

    @property
    def quantity_precision(self) -> float:
        return self.quantity_step
