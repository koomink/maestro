from pydantic import BaseModel, Field

from maestro.core.exceptions import MissingPriceError


class PortfolioState(BaseModel):
    cash: float
    positions: dict[str, float] = Field(default_factory=dict)

    def total_value(self, prices: dict[str, float]) -> float:
        missing_symbols = sorted(symbol for symbol in self.positions if symbol not in prices)
        if missing_symbols:
            raise MissingPriceError(
                f"Missing prices for position symbols: {', '.join(missing_symbols)}"
            )
        return self.cash + sum(
            quantity * prices[symbol] for symbol, quantity in self.positions.items()
        )

    def summary(self, prices: dict[str, float]) -> dict[str, float | dict[str, float]]:
        return {
            "cash": self.cash,
            "positions": self.positions,
            "total_value": self.total_value(prices),
        }
