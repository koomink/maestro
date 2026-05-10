from datetime import UTC, datetime
from typing import Any

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.yahoo_provider import YahooDataProvider
from maestro.sdk import DataRequest


class FakeYahooClient:
    def __init__(self, payloads: dict[str, Any], error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def history(
        self,
        symbol: str,
        *,
        period: str,
        interval: str,
        timeout_seconds: float,
    ) -> Any:
        self.calls.append(
            {
                "symbol": symbol,
                "period": period,
                "interval": interval,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.payloads.get(symbol, [])


def request(
    symbol: str = "SPY",
    data_type: str = "price",
    lookback: int | None = 2,
    timeframe: str | None = "1d",
) -> DataRequest:
    return DataRequest(
        symbol=symbol,
        asset_type="us_etf",
        data_type=data_type,
        lookback=lookback,
        timeframe=timeframe,
    )


def yahoo_rows() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1000,
        },
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "open": 101.0,
            "high": 104.0,
            "low": 100.0,
            "close": 103.0,
            "volume": 1200,
        },
        {
            "timestamp": "2026-01-03T00:00:00Z",
            "open": 103.0,
            "high": 105.0,
            "low": 102.0,
            "close": 104.0,
            "volume": 900,
        },
    ]


def test_yahoo_provider_normalizes_price_and_ohlcv_from_fixture_rows():
    client = FakeYahooClient({"SPY": yahoo_rows()})
    provider = YahooDataProvider(client=client, timeout_seconds=3.0)

    bundle = provider.get_data([request(data_type="ohlcv")])

    payload = bundle.data["SPY"]
    assert bundle.source == "yahoo"
    assert payload["latest_price"]["price"] == 104.0
    assert payload["latest_price"]["source"] == "yahoo"
    assert len(payload["bars"]) == 2
    assert payload["bars"][0]["close"] == 103.0
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
    assert client.calls == [
        {
            "symbol": "SPY",
            "period": "2d",
            "interval": "1d",
            "timeout_seconds": 3.0,
        }
    ]


def test_yahoo_provider_uses_symbol_map_for_provider_symbol():
    client = FakeYahooClient({"005930.KS": yahoo_rows()})
    provider = YahooDataProvider(client=client, symbol_map={"SAMSUNG": "005930.KS"})

    bundle = provider.get_data([request(symbol="SAMSUNG")])

    assert bundle.data["SAMSUNG"]["latest_price"]["price"] == 104.0
    assert client.calls[0]["symbol"] == "005930.KS"


def test_yahoo_provider_handles_cash_usd_without_external_call():
    client = FakeYahooClient({})
    provider = YahooDataProvider(client=client, symbol_map={"AAPL": "AAPL"})

    bundle = provider.get_data([request(symbol="CASH_USD")])

    assert bundle.data["CASH_USD"]["latest_price"]["price"] == 1.0
    assert bundle.data["CASH_USD"]["latest_price"]["source"] == "cash_reference"
    assert client.calls == []


def test_yahoo_provider_rejects_missing_symbol():
    provider = YahooDataProvider(client=FakeYahooClient({"MISSING": []}))

    with pytest.raises(ValueError, match="No Yahoo data for symbol: MISSING"):
        provider.get_data([request(symbol="MISSING")])


def test_yahoo_provider_rejects_malformed_payload():
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "volume": 1000,
                }
            ]
        }
    )
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="Malformed Yahoo payload"):
        provider.get_data([request()])


def test_yahoo_provider_marks_stale_payloads():
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": "2020-01-01T00:00:00Z",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000,
                }
            ]
        }
    )
    provider = YahooDataProvider(client=client, stale_after_seconds=60)

    payload = provider.get_data([request(lookback=1)]).data["SPY"]

    assert payload["is_stale"] is True
    assert "Yahoo data is stale" in payload["warnings"]


def test_yahoo_provider_preserves_fresh_metadata():
    now = datetime.now(UTC)
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": now,
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000,
                }
            ]
        }
    )
    provider = YahooDataProvider(client=client, stale_after_seconds=3600)

    payload = provider.get_data([request(lookback=1)]).data["SPY"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_yahoo_provider_maps_timeout_to_provider_unavailable():
    provider = YahooDataProvider(client=FakeYahooClient({}, error=TimeoutError("slow")))

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        provider.get_data([request()])
