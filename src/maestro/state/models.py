from pydantic import BaseModel, Field


class PortfolioState(BaseModel):
    cash: float
    positions: dict[str, float] = Field(default_factory=dict)

    def total_value(self, prices: dict[str, float]) -> float:
        return self.cash + sum(quantity * prices[symbol] for symbol, quantity in self.positions.items())

    def summary(self, prices: dict[str, float]) -> dict[str, float | dict[str, float]]:
        return {
            "cash": self.cash,
            "positions": self.positions,
            "total_value": self.total_value(prices),
        }
