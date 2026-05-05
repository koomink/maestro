from enum import StrEnum


class RunMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"


class StrategyMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    DISABLED = "disabled"


class AssetType(StrEnum):
    CASH = "cash"
    DOMESTIC_ETF = "domestic_etf"
    US_ETF = "us_etf"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"


class OrderStatus(StrEnum):
    CREATED = "created"
    FILLED = "filled"
