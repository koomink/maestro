from maestro.core.clock import utc_now
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
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
        return KISAccountSnapshot(
            account_id=self.account_id,
            cash=5_000_000.0,
            buying_power=5_000_000.0,
            positions=[
                KISPosition(
                    symbol="MOCK_ETF_A",
                    quantity=30000.0,
                    average_price=100.0,
                    current_price=self.prices["MOCK_ETF_A"],
                ),
                KISPosition(
                    symbol="MOCK_ETF_B",
                    quantity=40000.0,
                    average_price=50.0,
                    current_price=self.prices["MOCK_ETF_B"],
                ),
            ],
            fetched_at=utc_now(),
            source="kis_mock",
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        missing = [symbol for symbol in symbols if symbol not in self.prices]
        if missing:
            raise ValueError(f"Mock KIS prices missing symbols: {missing}")
        return {symbol: self.prices[symbol] for symbol in symbols}

    def get_daily_orders(self) -> list[KISOrderSummary]:
        return []

    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        return []
