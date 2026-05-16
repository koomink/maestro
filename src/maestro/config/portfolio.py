from typing import Literal

from pydantic import Field

from maestro.config.base import StrictConfigModel


class CurrencySleeveConfig(StrictConfigModel):
    cash_symbol: str
    symbols: list[str] = Field(default_factory=list)


class PortfolioConfig(StrictConfigModel):
    base_currency: str = "KRW"
    allocation_mode: Literal["single", "currency_sleeves"] = "single"
    initial_cash: float | None = Field(default=None, gt=0)
    cash_by_currency: dict[str, float] = Field(default_factory=dict)
    allowed_symbols: list[str]
    currency_sleeves: dict[str, CurrencySleeveConfig] = Field(default_factory=dict)


__all__ = ["CurrencySleeveConfig", "PortfolioConfig"]
