from pydantic import Field

from maestro.config.base import StrictConfigModel
from maestro.core.enums import BrokerProduct


class KISConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "mock"
    broker_product: BrokerProduct = BrokerProduct.KIS_OVERSEAS_STOCK
    broker_products: list[BrokerProduct] = Field(default_factory=list)
    account_id: str | None = None
    account_id_env: str | None = "KIS_ACCOUNT_ID"
    app_key_env: str = "KIS_APP_KEY"
    app_secret_env: str = "KIS_APP_SECRET"
    access_token_env: str = "KIS_ACCESS_TOKEN"
    approval_key_env: str = "KIS_APPROVAL_KEY"
    token_cache_path: str | None = None
    base_url: str | None = None
    paper_trading: bool = False
    timeout_seconds: float = Field(default=10.0, gt=0)
    quote_market_code: str = "J"

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.paper_trading:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    def effective_broker_products(self) -> list[BrokerProduct]:
        return self.broker_products or [self.broker_product]


__all__ = ["KISConfig"]
