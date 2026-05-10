from pydantic import Field, model_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import AssetType, BrokerProduct, Currency, ExchangeCode, MarketRegion
from maestro.core.instruments import TradableInstrument


class UniversePolicyConfig(StrictConfigModel):
    allowed_asset_types: list[AssetType] = Field(
        default_factory=lambda: [AssetType.STOCK, AssetType.ETF, AssetType.US_ETF]
    )
    allowed_regions: list[MarketRegion] = Field(default_factory=lambda: [MarketRegion.US])
    allowed_currencies: list[Currency] = Field(default_factory=lambda: [Currency.USD])
    allowed_broker_products: list[BrokerProduct] = Field(
        default_factory=lambda: [BrokerProduct.KIS_OVERSEAS_STOCK]
    )
    allowed_exchange_codes: list[ExchangeCode] = Field(
        default_factory=lambda: [ExchangeCode.NASD, ExchangeCode.NYSE, ExchangeCode.AMEX]
    )
    denied_symbols: list[str] = Field(default_factory=list)
    denied_asset_tags: list[str] = Field(default_factory=list)
    max_new_symbols_per_run: int = Field(default=1, ge=0)
    require_operator_approval_for_tradable: bool = True
    require_broker_tradability_check: bool = True
    require_data_freshness_check: bool = True


class UniverseConfig(StrictConfigModel):
    instruments: list[TradableInstrument] = Field(default_factory=list)
    research_symbols: list[str] = Field(default_factory=list)
    policy: UniversePolicyConfig = Field(default_factory=UniversePolicyConfig)

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> "UniverseConfig":
        symbols = [instrument.symbol for instrument in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("universe instruments must use unique canonical symbols")
        return self

    def get(self, symbol: str) -> TradableInstrument | None:
        for instrument in self.instruments:
            if instrument.symbol == symbol:
                return instrument
        return None


__all__ = ["UniverseConfig", "UniversePolicyConfig"]
