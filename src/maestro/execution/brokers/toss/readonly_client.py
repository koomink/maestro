import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from maestro.config.broker import BrokerAccountConfig
from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerBuyingPower,
    BrokerOrderSummary,
    BrokerPosition,
    BrokerReadOnlyClient,
    buying_power_for_currency,
)
from maestro.execution.brokers.toss.auth import TossAuthManager
from maestro.execution.brokers.toss.order_history import list_toss_orders
from maestro.execution.brokers.toss.parsers import toss_order_summaries, toss_snapshot_from_payloads
from maestro.execution.brokers.toss.transport import TossRestTransport


class TossReadOnlyClient(BrokerReadOnlyClient):
    def __init__(
        self,
        config: BrokerAccountConfig,
        *,
        transport: Any | None = None,
    ) -> None:
        if config.broker != "toss":
            raise ValueError(f"Account {config.id} is not a Toss account")
        self.config = config
        self.account_seq = _resolve_account_seq(config)
        self.transport = transport or TossRestTransport(
            base_url=config.base_url or "https://openapi.tossinvest.com",
            access_token_provider=TossAuthManager(config).access_token,
            timeout_seconds=config.timeout_seconds,
        )
        self._snapshot = None

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        return self._read_snapshot().account

    def get_positions(self) -> list[BrokerPosition]:
        return list(self._read_snapshot().account.positions)

    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
        currency: str | None = None,
    ) -> BrokerBuyingPower:
        # One Toss account holds a KRW pot and a USD pot. `buying_power_detail`
        # reports whichever the parser named primary, so answering with it would
        # judge a dollar order against the won balance.
        return buying_power_for_currency(
            self._read_snapshot().account,
            currency,
            symbol=symbol,
            order_price=order_price,
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices = dict(self._read_snapshot().current_prices)
        missing = [
            symbol
            for symbol in symbols
            if symbol not in prices
            and not symbol.startswith("CASH")
            and _is_toss_price_symbol(symbol)
        ]
        if missing:
            payload = self.transport.get("/api/v1/prices", {"symbols": ",".join(missing)})
            for item in payload.get("result", []):
                prices[str(item["symbol"])] = float(str(item["lastPrice"]))
        for symbol in symbols:
            if symbol.startswith("CASH"):
                prices.setdefault(symbol, 1.0)
        return {symbol: prices[symbol] for symbol in symbols if symbol in prices}

    def get_order_fills(self) -> list[BrokerOrderSummary]:
        return list(self._read_snapshot().order_fills)

    def get_unfilled_orders(self) -> list[BrokerOrderSummary]:
        return list(self._read_snapshot().unfilled_orders)

    def list_orders(
        self,
        *,
        status: str,
        symbol: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[BrokerOrderSummary]:
        if status not in {"OPEN", "CLOSED"}:
            raise ValueError("Toss order history status must be OPEN or CLOSED")
        orders = list_toss_orders(
            self.transport,
            self.account_seq,
            status=status,
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
        )
        return toss_order_summaries(orders, [])

    def _read_snapshot(self):
        if self._snapshot is None:
            account = self._selected_account()
            holdings = self.transport.get("/api/v1/holdings", account_seq=self.account_seq)[
                "result"
            ]
            buying_powers = [
                self.transport.get(
                    "/api/v1/buying-power",
                    {"currency": currency},
                    account_seq=self.account_seq,
                )["result"]
                for currency in ("KRW", "USD")
            ]
            today = datetime.now(ZoneInfo("Asia/Seoul")).date()
            open_orders = list_toss_orders(
                self.transport,
                self.account_seq,
                status="OPEN",
            )
            closed_orders = list_toss_orders(
                self.transport,
                self.account_seq,
                status="CLOSED",
                from_date=today,
                to_date=today,
            )
            commissions = self.transport.get(
                "/api/v1/commissions",
                account_seq=self.account_seq,
            ).get("result", [])
            symbols = [
                str(item["symbol"])
                for item in holdings.get("items", [])
                if item.get("symbol") is not None
            ]
            prices = []
            if symbols:
                prices_payload = self.transport.get(
                    "/api/v1/prices",
                    {"symbols": ",".join(symbols)},
                )
                prices = list(prices_payload.get("result", []))
            self._snapshot = toss_snapshot_from_payloads(
                account=account,
                holdings=holdings,
                buying_powers=buying_powers,
                open_orders=open_orders,
                closed_orders=closed_orders,
                commissions=list(commissions),
                prices=prices,
            )
        return self._snapshot

    def _selected_account(self) -> dict[str, Any]:
        payload = self.transport.get("/api/v1/accounts")
        accounts = payload.get("result", [])
        for account in accounts:
            if int(account["accountSeq"]) == self.account_seq:
                return account
        raise ValueError(f"Toss account_seq not found: {self.account_seq}")


def _resolve_account_seq(config: BrokerAccountConfig) -> int:
    if config.account_seq is not None:
        return config.account_seq
    if config.account_seq_env:
        value = os.getenv(config.account_seq_env)
        if value:
            return int(value)
    raise ValueError("Toss account_seq or account_seq_env is required")


def _is_toss_price_symbol(symbol: str) -> bool:
    return all(char.isalnum() or char in ".-" for char in symbol)


__all__ = ["TossReadOnlyClient"]
