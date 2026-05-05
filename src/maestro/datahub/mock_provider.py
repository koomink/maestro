from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.schemas import OHLCVBar, PricePoint, SymbolData
from maestro.sdk import DataBundle, DataRequest


class MockDataHub(BaseDataProvider):
    _prices = {
        "CASH": 1.0,
        "MOCK_ETF_A": 100.0,
        "MOCK_ETF_B": 50.0,
    }

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        data = {}
        generated_at = utc_now()
        for request in requests:
            if request.symbol not in self._prices:
                raise ValueError(f"Unknown mock symbol: {request.symbol}")
            price = self._prices[request.symbol]
            bars = []
            if request.symbol != "CASH":
                bars.append(
                    OHLCVBar(
                        symbol=request.symbol,
                        timestamp=generated_at,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=1000,
                        source="mock",
                    )
                )
            symbol_data = SymbolData(
                symbol=request.symbol,
                latest_price=PricePoint(
                    symbol=request.symbol,
                    timestamp=generated_at,
                    price=price,
                    source="mock",
                ),
                bars=bars,
            )
            data[request.symbol] = symbol_data.model_dump(mode="json")
        return DataBundle(requests=requests, data=data, generated_at=generated_at, source="mock")
