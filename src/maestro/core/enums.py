from enum import Enum


class RunMode(str, Enum):
    PAPER = "paper"


class StrategyMode(str, Enum):
    PAPER = "paper"
    DISABLED = "disabled"


class AssetType(str, Enum):
    CASH = "cash"
    DOMESTIC_ETF = "domestic_etf"
    US_ETF = "us_etf"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"


class OrderStatus(str, Enum):
    CREATED = "created"
    FILLED = "filled"
