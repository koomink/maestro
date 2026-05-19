from enum import StrEnum


class RunMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL = "live_approval"


class ProfileStage(StrEnum):
    PAPER = "paper"
    PAPER_REAL_DATA = "paper_real_data"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL_DISABLED = "live_approval_disabled"
    LIVE_APPROVAL_DRY_RUN = "live_approval_dry_run"
    KIS_PAPER_TRADING = "kis_paper_trading"
    PRODUCTION_ARMED = "production_armed"


class StrategyMode(StrEnum):
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL = "live_approval"
    DISABLED = "disabled"


class AssetType(StrEnum):
    CASH = "cash"
    STOCK = "stock"
    ETF = "etf"
    DOMESTIC_ETF = "domestic_etf"
    US_ETF = "us_etf"


class MarketRegion(StrEnum):
    US = "US"
    KR = "KR"
    GLOBAL = "GLOBAL"


class BrokerProduct(StrEnum):
    KIS_DOMESTIC_STOCK = "kis_domestic_stock"
    KIS_OVERSEAS_STOCK = "kis_overseas_stock"


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class ExchangeCode(StrEnum):
    NASD = "NASD"
    NYSE = "NYSE"
    AMEX = "AMEX"
    KRX = "KRX"


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


class SafetyState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    KILLED = "killed"
    HALTED = "halted"
