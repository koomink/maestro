from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.sdk import DataBundle, DataRequest


class MockDataHub(BaseDataProvider):
    _prices = {
        "CASH": 1.0,
        "MOCK_ETF_A": 100.0,
        "MOCK_ETF_B": 50.0,
    }

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        data = {}
        for request in requests:
            if request.symbol not in self._prices:
                raise ValueError(f"Unknown mock symbol: {request.symbol}")
            price = self._prices[request.symbol]
            data[request.symbol] = {
                "price": price,
                "ohlcv": [
                    {"open": price, "high": price, "low": price, "close": price, "volume": 1000}
                ],
            }
        return DataBundle(requests=requests, data=data, generated_at=utc_now(), source="mock")
