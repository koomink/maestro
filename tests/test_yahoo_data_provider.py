from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.resilience import ResilientDataProvider
from maestro.datahub.yahoo_provider import YahooDataProvider
from maestro.sdk import DataRequest


class FakeYahooClient:
    def __init__(
        self,
        payloads: dict[str, Any],
        error: Exception | None = None,
        info_payloads: dict[str, Any] | None = None,
        statement_payloads: dict[tuple[str, str], Any] | None = None,
        dividends_payloads: dict[str, Any] | None = None,
    ) -> None:
        self.payloads = payloads
        self.error = error
        self.info_payloads = info_payloads or {}
        self.statement_payloads = statement_payloads or {}
        self.dividends_payloads = dividends_payloads or {}
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

    def info(self, symbol: str, *, timeout_seconds: float) -> Any:
        self.calls.append(
            {
                "symbol": symbol,
                "method": "info",
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.info_payloads.get(symbol, {})

    def financial_statement(
        self,
        symbol: str,
        *,
        statement_type: str,
        timeout_seconds: float,
    ) -> Any:
        self.calls.append(
            {
                "symbol": symbol,
                "method": "financial_statement",
                "statement_type": statement_type,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.statement_payloads.get((symbol, statement_type), {})

    def dividends(self, symbol: str, *, timeout_seconds: float) -> Any:
        self.calls.append(
            {
                "symbol": symbol,
                "method": "dividends",
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.dividends_payloads.get(symbol, [])


class FlakyEmptyYahooClient(FakeYahooClient):
    """Returns an empty history for the first calls, the way Yahoo does under throttling."""

    def __init__(self, payloads: dict[str, Any], *, empty_responses: int) -> None:
        super().__init__(payloads)
        self.empty_responses = empty_responses

    def history(self, symbol: str, **kwargs: Any) -> Any:
        rows = super().history(symbol, **kwargs)
        if self.empty_responses > 0:
            self.empty_responses -= 1
            return []
        return rows


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


def test_yahoo_provider_treats_empty_history_as_unavailable():
    provider = YahooDataProvider(client=FakeYahooClient({"MISSING": []}))

    with pytest.raises(ProviderUnavailableError, match="No Yahoo data for symbol: MISSING"):
        provider.get_data([request(symbol="MISSING")])


def test_yahoo_provider_empty_history_is_retried_by_the_resilient_wrapper():
    now = datetime.now(UTC)
    client = FlakyEmptyYahooClient(
        {"SPY": [_bar_row(now - timedelta(days=2)), _bar_row(now - timedelta(days=1))]},
        empty_responses=1,
    )
    provider = ResilientDataProvider(
        YahooDataProvider(client=client),
        retry_max_attempts=2,
    )

    bundle = provider.get_data([request()])

    assert bundle.data["SPY"]["latest_price"]["price"] == 101.0
    assert len(client.calls) == 2


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


def _bar_row(timestamp: datetime, **overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp": timestamp.isoformat(),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 1000,
    }
    row.update(overrides)
    return row


def test_yahoo_provider_drops_malformed_forming_bar():
    now = datetime.now(UTC)
    client = FakeYahooClient(
        {
            "SPY": [
                _bar_row(now - timedelta(days=2), close=103.0, high=104.0),
                _bar_row(now - timedelta(days=1), close=104.0, high=105.0),
                # Still-forming current-session bar with inconsistent OHLC.
                _bar_row(now - timedelta(hours=6), low=105.0, close=101.0, high=106.0),
            ]
        }
    )
    provider = YahooDataProvider(client=client, stale_after_seconds=604800)

    payload = provider.get_data([request(data_type="ohlcv", lookback=3)]).data["SPY"]

    assert len(payload["bars"]) == 2
    assert payload["latest_price"]["price"] == 104.0
    assert payload["is_stale"] is False
    assert any("Dropped malformed forming bar for SPY" in w for w in payload["warnings"])


def test_yahoo_provider_rejects_malformed_completed_bar():
    now = datetime.now(UTC)
    client = FakeYahooClient(
        {
            "SPY": [
                _bar_row(now - timedelta(days=3), close=103.0, high=104.0),
                # Completed bar (older than the forming window) stays fail-closed.
                _bar_row(now - timedelta(days=2), low=105.0, close=101.0, high=106.0),
            ]
        }
    )
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="Malformed Yahoo payload for SPY"):
        provider.get_data([request(data_type="ohlcv", lookback=2)])


def test_yahoo_provider_rejects_malformed_forming_bar_before_last_row():
    now = datetime.now(UTC)
    client = FakeYahooClient(
        {
            "SPY": [
                _bar_row(now - timedelta(hours=12), low=105.0, close=101.0, high=106.0),
                _bar_row(now - timedelta(hours=6), close=104.0, high=105.0),
            ]
        }
    )
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="Malformed Yahoo payload for SPY"):
        provider.get_data([request(data_type="ohlcv", lookback=2)])


def test_yahoo_provider_fails_when_only_bar_is_malformed_forming_bar():
    now = datetime.now(UTC)
    client = FakeYahooClient(
        {"SPY": [_bar_row(now - timedelta(hours=6), low=105.0, close=101.0, high=106.0)]}
    )
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="No Yahoo data for symbol: SPY"):
        provider.get_data([request(lookback=1)])


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


def test_yahoo_provider_uses_monthly_freshness_for_monthly_ohlcv():
    now = datetime.now(UTC)
    monthly_bar_timestamp = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": monthly_bar_timestamp,
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000,
                }
            ]
        }
    )
    provider = YahooDataProvider(client=client, stale_after_seconds=604800)

    payload = provider.get_data([request(data_type="ohlcv", lookback=1, timeframe="1mo")]).data[
        "SPY"
    ]

    assert payload["latest_price"]["timestamp"] == monthly_bar_timestamp.isoformat().replace(
        "+00:00", "Z"
    )
    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_yahoo_provider_maps_timeout_to_provider_unavailable():
    provider = YahooDataProvider(client=FakeYahooClient({}, error=TimeoutError("slow")))

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        provider.get_data([request()])


def test_yahoo_provider_returns_fundamental_metrics_from_info_payload():
    client = FakeYahooClient(
        {},
        info_payloads={
            "AAPL": {
                "trailingPE": 28.5,
                "forwardPE": 25.1,
                "priceToBook": 39.2,
                "marketCap": 3_000_000_000_000,
                "dividendYield": 0.004,
            }
        },
    )
    provider = YahooDataProvider(client=client, timeout_seconds=4.0)

    bundle = provider.get_data(
        [
            DataRequest(
                symbol="AAPL",
                asset_type="stock",
                data_type="fundamental",
                fields=["trailing_pe", "market_cap", "dividend_yield"],
            )
        ]
    )

    payload = bundle.data["AAPL"]
    assert payload["data_type"] == "fundamental"
    assert payload["metrics"] == {
        "trailing_pe": 28.5,
        "market_cap": 3_000_000_000_000,
        "dividend_yield": 0.004,
    }
    assert payload["fundamental"]["metrics"]["trailing_pe"] == 28.5
    assert payload["source"] == "yahoo"
    assert client.calls == [{"symbol": "AAPL", "method": "info", "timeout_seconds": 4.0}]


def test_yahoo_provider_returns_as_of_dividend_yield_from_dividend_history():
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": "2026-04-30T00:00:00Z",
                    "open": 109.0,
                    "high": 109.0,
                    "low": 108.0,
                    "close": 110.0,
                    "volume": 1000,
                },
                {
                    "timestamp": "2026-05-01T00:00:00Z",
                    "open": 999.0,
                    "high": 1001.0,
                    "low": 998.0,
                    "close": 1000.0,
                    "volume": 1000,
                },
            ]
        },
        dividends_payloads={
            "SPY": [
                {"timestamp": "2025-04-30T00:00:00Z", "dividend": 10.0},
                {"timestamp": "2025-05-01T00:00:00Z", "dividend": 1.0},
                {"timestamp": "2025-12-15T00:00:00Z", "dividend": 1.2},
                {"timestamp": "2026-05-01T00:00:00Z", "dividend": 99.0},
            ]
        },
    )
    provider = YahooDataProvider(client=client, timeout_seconds=4.0)

    bundle = provider.get_data(
        [
            DataRequest(
                symbol="SPY",
                asset_type="us_etf",
                data_type="fundamental",
                fields=["dividend_yield"],
                as_of=datetime(2026, 5, 1, tzinfo=UTC),
            )
        ]
    )

    payload = bundle.data["SPY"]
    assert payload["metrics"] == {"dividend_yield": 0.02}
    assert payload["fundamental"]["metrics"] == {"dividend_yield": 0.02}
    assert {call.get("method", "history") for call in client.calls} == {"dividends", "history"}


def test_yahoo_provider_excludes_as_of_date_for_dividend_yield_price():
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": "2026-04-30T00:00:00Z",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1000,
                },
                {
                    "timestamp": "2026-05-01T00:00:00Z",
                    "open": 50.0,
                    "high": 50.0,
                    "low": 50.0,
                    "close": 50.0,
                    "volume": 1000,
                },
            ]
        },
        dividends_payloads={
            "SPY": [
                {"timestamp": "2026-04-30T00:00:00Z", "dividend": 2.0},
                {"timestamp": "2026-05-01T00:00:00Z", "dividend": 100.0},
            ]
        },
    )
    provider = YahooDataProvider(client=client)

    payload = provider.get_data(
        [
            DataRequest(
                symbol="SPY",
                asset_type="us_etf",
                data_type="fundamental",
                fields=["dividend_yield"],
                as_of=datetime(2026, 5, 1, tzinfo=UTC),
            )
        ]
    ).data["SPY"]

    assert payload["metrics"]["dividend_yield"] == 0.02


def test_yahoo_provider_fails_as_of_dividend_yield_without_dividends():
    client = FakeYahooClient({"SPY": yahoo_rows()}, dividends_payloads={"SPY": []})
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="dividend history"):
        provider.get_data(
            [
                DataRequest(
                    symbol="SPY",
                    asset_type="us_etf",
                    data_type="fundamental",
                    fields=["dividend_yield"],
                    as_of=datetime(2026, 5, 1, tzinfo=UTC),
                )
            ]
        )


def test_yahoo_provider_fails_as_of_dividend_yield_without_prior_price():
    client = FakeYahooClient(
        {
            "SPY": [
                {
                    "timestamp": "2026-05-01T00:00:00Z",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1000,
                }
            ]
        },
        dividends_payloads={"SPY": [{"timestamp": "2026-04-01T00:00:00Z", "dividend": 1.0}]},
    )
    provider = YahooDataProvider(client=client)

    with pytest.raises(ValueError, match="price history"):
        provider.get_data(
            [
                DataRequest(
                    symbol="SPY",
                    asset_type="us_etf",
                    data_type="fundamental",
                    fields=["dividend_yield"],
                    as_of=datetime(2026, 5, 1, tzinfo=UTC),
                )
            ]
        )


def test_yahoo_provider_returns_financial_statement_rows():
    client = FakeYahooClient(
        {},
        statement_payloads={
            (
                "AAPL",
                "income_statement",
            ): {
                "Total Revenue": {
                    datetime(2025, 12, 31, tzinfo=UTC): 391_000_000_000,
                    datetime(2024, 12, 31, tzinfo=UTC): 383_000_000_000,
                },
                "Net Income": {
                    datetime(2025, 12, 31, tzinfo=UTC): 96_000_000_000,
                    datetime(2024, 12, 31, tzinfo=UTC): 94_000_000_000,
                },
            }
        },
    )
    provider = YahooDataProvider(client=client)

    bundle = provider.get_data(
        [
            DataRequest(
                symbol="AAPL",
                asset_type="stock",
                data_type="financial_statements",
                statement_type="income_statement",
                frequency="annual",
            )
        ]
    )

    payload = bundle.data["AAPL"]
    assert payload["data_type"] == "financial_statements"
    assert payload["statement_type"] == "income_statement"
    assert payload["statement"][0]["period"] == "2025-12-31T00:00:00+00:00"
    assert payload["statement"][0]["Total Revenue"] == 391_000_000_000
    assert payload["statement"][0]["Net Income"] == 96_000_000_000
    assert (
        payload["financial_statements"]["income_statement"]["statement"][0]["Net Income"]
        == 96_000_000_000
    )


def test_yahoo_provider_requires_statement_type_for_financial_statements():
    provider = YahooDataProvider(client=FakeYahooClient({}))

    with pytest.raises(ValueError, match="require statement_type"):
        provider.get_data(
            [
                DataRequest(
                    symbol="AAPL",
                    asset_type="stock",
                    data_type="financial_statements",
                )
            ]
        )
