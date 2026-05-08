from enum import StrEnum


class RunMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL = "live_approval"


class StrategyMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL = "live_approval"
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
    LIMIT = "limit"


class OrderStatus(StrEnum):
    CREATED = "created"
    PENDING_APPROVAL = "pending_approval"
    SUBMITTED = "submitted"
    ACCEPTED_BY_BROKER = "accepted_by_broker"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    HALTED = "halted"
    FAILED = "failed"
