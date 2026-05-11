from pydantic import BaseModel, Field, model_validator

from maestro.core.exceptions import MissingPriceError


class PortfolioState(BaseModel):
    cash: float
    cash_by_currency: dict[str, float] = Field(default_factory=dict)
    positions: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def sync_legacy_cash(self) -> "PortfolioState":
        if self.cash_by_currency:
            base_cash = self.cash_by_currency.get("KRW")
            self.cash = float(
                base_cash if base_cash is not None else sum(self.cash_by_currency.values())
            )
        return self

    def total_value(self, prices: dict[str, float]) -> float:
        missing_symbols = sorted(symbol for symbol in self.positions if symbol not in prices)
        if missing_symbols:
            raise MissingPriceError(
                f"Missing prices for position symbols: {', '.join(missing_symbols)}"
            )
        cash_value = sum(self.cash_by_currency.values()) if self.cash_by_currency else self.cash
        return cash_value + sum(
            quantity * prices[symbol] for symbol, quantity in self.positions.items()
        )

    def sleeve_value(
        self,
        *,
        symbols: list[str],
        cash_symbol: str,
        currency: str,
        prices: dict[str, float],
    ) -> float:
        missing_symbols = sorted(
            symbol for symbol in symbols if self.positions.get(symbol, 0.0) and symbol not in prices
        )
        if missing_symbols:
            raise MissingPriceError(
                f"Missing prices for position symbols: {', '.join(missing_symbols)}"
            )
        cash = self.cash_by_currency.get(currency, self.cash if cash_symbol == "CASH" else 0.0)
        return cash + sum(
            self.positions.get(symbol, 0.0) * prices[symbol]
            for symbol in symbols
            if symbol in prices
        )

    def summary(self, prices: dict[str, float]) -> dict[str, float | dict[str, float]]:
        return {
            "cash": self.cash,
            "cash_by_currency": self.cash_by_currency,
            "positions": self.positions,
            "total_value": self.total_value(prices),
        }
