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
    allowed_symbols: list[str] = Field(default_factory=list)
    currency_sleeves: dict[str, CurrencySleeveConfig] = Field(default_factory=dict)

    def derive_allowed_symbols(self, universe_symbols: list[str]) -> list[str]:
        if self.allowed_symbols:
            return self.allowed_symbols
        if self.allocation_mode == "currency_sleeves" and self.currency_sleeves:
            symbols = []
            for sleeve in self.currency_sleeves.values():
                symbols.append(sleeve.cash_symbol)
                symbols.extend(sleeve.symbols)
            return list(dict.fromkeys(symbols))
        return universe_symbols


__all__ = ["CurrencySleeveConfig", "PortfolioConfig"]
