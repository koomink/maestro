from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from maestro.core.enums import AssetType

SUPPORTED_DATA_TYPES = frozenset(
    {
        "price",
        "ohlcv",
        "macro",
        "news",
        "sentiment",
        "fundamental",
        "broker_quote",
    }
)


class PricePoint(BaseModel):
    symbol: str
    timestamp: datetime
    price: float = Field(gt=0)
    source: str


class OHLCVBar(BaseModel):
    symbol: str
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    source: str

    @model_validator(mode="after")
    def validate_ohlcv_shape(self) -> "OHLCVBar":
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.high < self.open or self.high < self.close:
            raise ValueError("high must be greater than or equal to open and close")
        if self.low > self.open or self.low > self.close:
            raise ValueError("low must be less than or equal to open and close")
        return self


class SymbolData(BaseModel):
    symbol: str
    latest_price: PricePoint | None = None
    bars: list[OHLCVBar] = Field(default_factory=list)
    is_stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class SymbolMetadata(BaseModel):
    symbol: str
    asset_type: AssetType
    currency: str = "KRW"
    tradable: bool = True
    quantity_step: float | None = None
    min_order_quantity: float | None = None
    min_order_notional: float | None = None

    @field_validator("quantity_step", "min_order_quantity", "min_order_notional")
    @classmethod
    def validate_optional_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("metadata numeric constraints must be positive when set")
        return value
