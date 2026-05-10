from pydantic import Field

from maestro.config.base import StrictConfigModel


class PortfolioConfig(StrictConfigModel):
    base_currency: str = "KRW"
    initial_cash: float = Field(gt=0)
    allowed_symbols: list[str]


__all__ = ["PortfolioConfig"]
