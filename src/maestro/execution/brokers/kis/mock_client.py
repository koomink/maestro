from maestro.core.clock import utc_now
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISBuyingPower,
    KISCashBalance,
    KISOrderSummary,
    KISPosition,
)


class MockKISReadOnlyClient(KISReadOnlyClient):
    def __init__(self, account_id: str = "MOCK-ACCOUNT") -> None:
        self.account_id = account_id
        self.prices = {
            "CASH": 1.0,
            "MOCK_ETF_A": 100.0,
            "MOCK_ETF_B": 50.0,
        }

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions = self.get_positions()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.account_id,
            cash=5_000_000.0,
            cash_by_currency={"KRW": 5_000_000.0},
            buying_power=buying_power.cash_buying_power,
            buying_power_by_currency={"KRW": buying_power.cash_buying_power},
            positions=positions,
            cash_balance=KISCashBalance(cash=5_000_000.0, withdrawable_cash=5_000_000.0),
            buying_power_detail=buying_power,
            fetched_at=utc_now(),
            source="kis_mock",
        )

    def get_positions(self) -> list[KISPosition]:
        return [
            KISPosition(
                symbol="MOCK_ETF_A",
                quantity=30000.0,
                average_price=100.0,
                current_price=self.prices["MOCK_ETF_A"],
                currency="KRW",
            ),
            KISPosition(
                symbol="MOCK_ETF_B",
                quantity=40000.0,
                average_price=50.0,
                current_price=self.prices["MOCK_ETF_B"],
                currency="KRW",
            ),
        ]

    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
        currency: str | None = None,
    ) -> KISBuyingPower:
        # Mirrors the live domestic adapter: one market, one currency, named so
        # that a mis-routed order in another currency is caught.
        return KISBuyingPower(
            symbol=symbol,
            order_price=order_price,
            cash_buying_power=5_000_000.0,
            currency="KRW",
            max_buy_quantity=None,
            source="kis_mock",
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        missing = [symbol for symbol in symbols if symbol not in self.prices]
        if missing:
            raise ValueError(f"Mock KIS prices missing symbols: {missing}")
        return {symbol: self.prices[symbol] for symbol in symbols}

    def get_order_fills(self) -> list[KISOrderSummary]:
        return []

    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        return []
