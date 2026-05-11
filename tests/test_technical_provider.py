from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from maestro.datahub.base import BaseDataProvider
from maestro.datahub.technical_provider import TechnicalIndicatorProvider
from maestro.sdk import DataBundle, DataRequest


class FixtureOHLCVProvider(BaseDataProvider):
    def __init__(self, bars: list[dict[str, Any]]) -> None:
        self.bars = bars
        self.received_requests: list[DataRequest] = []

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        self.received_requests.extend(requests)
        return DataBundle(
            requests=requests,
            data={
                request.symbol: {
                    "symbol": request.symbol,
                    "bars": self.bars,
                    "source": "fixture",
                    "is_stale": False,
                    "warnings": [],
                }
                for request in requests
            },
            generated_at=datetime.now(UTC),
            source="fixture",
        )


def request(indicator: str = "sma", lookback: int | None = 3) -> DataRequest:
    return DataRequest(
        symbol="SPY",
        asset_type="us_etf",
        data_type="technical_indicators",
        indicator=indicator,
        lookback=lookback,
    )


def bars(count: int = 30) -> list[dict[str, Any]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "close": float(index + 1),
        }
        for index in range(count)
    ]


def test_technical_provider_calculates_sma_from_ohlcv_provider():
    fixture = FixtureOHLCVProvider(bars(5))
    provider = TechnicalIndicatorProvider(ohlcv_provider=fixture)

    bundle = provider.get_data([request("sma", lookback=3)])

    payload = bundle.data["SPY"]
    assert bundle.source == "technical_indicators"
    assert payload["indicator"] == "sma"
    assert [item["value"] for item in payload["values"]] == [2.0, 3.0, 4.0]
    assert payload["technical_indicators"]["sma"]["values"][0]["value"] == 2.0
    assert fixture.received_requests[0].data_type == "ohlcv"


def test_technical_provider_calculates_rsi():
    provider = TechnicalIndicatorProvider(ohlcv_provider=FixtureOHLCVProvider(bars(20)))

    payload = provider.get_data([request("RSI", lookback=14)]).data["SPY"]

    assert payload["indicator"] == "rsi"
    assert payload["values"]
    assert payload["values"][-1]["value"] == 100.0


def test_technical_provider_calculates_bollinger_bands():
    provider = TechnicalIndicatorProvider(ohlcv_provider=FixtureOHLCVProvider(bars(5)))

    payload = provider.get_data([request("BOLLINGER", lookback=3)]).data["SPY"]

    latest = payload["values"][-1]
    assert set(latest) == {"timestamp", "middle", "upper", "lower"}
    assert latest["middle"] == 4.0
    assert latest["upper"] > latest["middle"] > latest["lower"]


def test_technical_provider_rejects_missing_indicator():
    provider = TechnicalIndicatorProvider(ohlcv_provider=FixtureOHLCVProvider(bars()))

    with pytest.raises(ValueError, match="require indicator"):
        provider.get_data([request(indicator="", lookback=3)])


def test_technical_provider_rejects_unsupported_indicator():
    provider = TechnicalIndicatorProvider(ohlcv_provider=FixtureOHLCVProvider(bars()))

    with pytest.raises(ValueError, match="Unsupported technical indicator"):
        provider.get_data([request(indicator="stochastic", lookback=3)])
