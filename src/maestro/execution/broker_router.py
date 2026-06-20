from typing import TYPE_CHECKING

from maestro.config.broker import BrokerAccountConfig, KISConfig
from maestro.config.strategy import StrategyPluginConfig

if TYPE_CHECKING:
    from maestro.config.models import MaestroConfig

PAPER_DEFAULT_ACCOUNT_ID = "paper_default"


class UnsupportedBrokerOperation(RuntimeError):
    pass


class BrokerAccountRouter:
    def __init__(self, config: "MaestroConfig") -> None:
        self.config = config
        self.accounts = {account.id: account for account in config.accounts if account.enabled}

    def account_id_for_strategy(self, strategy: StrategyPluginConfig) -> str:
        if strategy.account_id:
            return strategy.account_id
        return PAPER_DEFAULT_ACCOUNT_ID

    def account(self, account_id: str | None) -> BrokerAccountConfig | None:
        if account_id is None:
            return self._single_enabled_account()
        return self.accounts.get(account_id)

    def kis_config(self, account_id: str | None = None) -> KISConfig:
        if account_id is None:
            return self.config.kis
        account = self.account(account_id)
        if account is None:
            raise ValueError(f"Unknown broker account_id: {account_id}")
        if account.broker == "toss":
            raise UnsupportedBrokerOperation(
                "Toss accounts do not expose KIS configuration; use the Toss broker adapter."
            )
        if account.broker == "sandbox":
            raise UnsupportedBrokerOperation(
                "Sandbox broker accounts are approval rehearsal only and do not support "
                "read-only or live broker operations."
            )
        return account.to_kis_config()

    def broker_products(self, account_id: str | None = None):
        account = self.account(account_id)
        if account is None:
            return self.config.kis.effective_broker_products()
        return account.effective_broker_products()

    def _single_enabled_account(self) -> BrokerAccountConfig | None:
        if len(self.accounts) == 1:
            return next(iter(self.accounts.values()))
        return None


__all__ = [
    "BrokerAccountRouter",
    "PAPER_DEFAULT_ACCOUNT_ID",
    "UnsupportedBrokerOperation",
]
