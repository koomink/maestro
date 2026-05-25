from typing import Literal

from pydantic import Field

from maestro.config.base import StrictConfigModel
from maestro.core.enums import BrokerProduct


class KISConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "mock"
    broker_product: BrokerProduct = BrokerProduct.KIS_OVERSEAS_STOCK
    broker_products: list[BrokerProduct] = Field(default_factory=list)
    account_id: str | None = None
    account_id_env: str | None = "KIS_MOCK_ACCOUNT_ID"
    app_key_env: str = "KIS_MOCK_APP_KEY"
    app_secret_env: str = "KIS_MOCK_APP_SECRET"
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


class BrokerAccountConfig(StrictConfigModel):
    id: str
    broker: Literal["kis", "toss", "sandbox"] = "kis"
    environment: Literal["real", "paper_trading"] = "real"
    enabled: bool = True
    provider: str | None = None
    broker_product: BrokerProduct = BrokerProduct.KIS_OVERSEAS_STOCK
    broker_products: list[BrokerProduct] = Field(default_factory=list)
    account_id: str | None = None
    account_id_env: str | None = None
    app_key_env: str = "KIS_MOCK_APP_KEY"
    app_secret_env: str = "KIS_MOCK_APP_SECRET"
    access_token_env: str = "KIS_ACCESS_TOKEN"
    approval_key_env: str = "KIS_APPROVAL_KEY"
    token_cache_path: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)
    quote_market_code: str = "J"

    def effective_broker_products(self) -> list[BrokerProduct]:
        return self.broker_products or [self.broker_product]

    def to_kis_config(self) -> KISConfig:
        if self.broker != "kis":
            raise ValueError(f"Account {self.id} is not a KIS account")
        return KISConfig(
            enabled=self.enabled,
            provider=self.provider or "kis",
            broker_product=self.broker_product,
            broker_products=list(self.broker_products),
            account_id=self.account_id,
            account_id_env=self.account_id_env or "KIS_MOCK_ACCOUNT_ID",
            app_key_env=self.app_key_env,
            app_secret_env=self.app_secret_env,
            access_token_env=self.access_token_env,
            approval_key_env=self.approval_key_env,
            token_cache_path=self.token_cache_path,
            base_url=self.base_url,
            paper_trading=self.environment == "paper_trading",
            timeout_seconds=self.timeout_seconds,
            quote_market_code=self.quote_market_code,
        )


__all__ = ["BrokerAccountConfig", "KISConfig"]
