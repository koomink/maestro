from datetime import UTC, datetime
from typing import Any

import pytest

from maestro.config.models import DataHubConfig, DataHubProviderConfig
from maestro.core.enums import RunMode
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.datahub.errors import (
    NoProviderError,
    ProviderUnavailableError,
    StaleDataError,
    UnsupportedDataTypeError,
)
from maestro.datahub.fred_provider import FREDDataProvider
from maestro.datahub.registry import DataHubRegistry
from maestro.datahub.router import DataHubRouter
from maestro.datahub.rss_provider import RSSNewsProvider
from maestro.datahub.schemas import PricePoint, SymbolData
from maestro.datahub.sentiment_provider import RuleBasedSentimentProvider
from maestro.datahub.yahoo_provider import YahooDataProvider
from maestro.sdk import DataBundle, DataRequest


class FixtureProvider(BaseDataProvider):
    def __init__(self, source: str, payloads: dict[str, Any]) -> None:
        self.source = source
        self.payloads = payloads
        self.received_requests: list[DataRequest] = []

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        self.received_requests.extend(requests)
        return DataBundle(
            requests=requests,
            data={request.symbol: self.payloads[request.symbol] for request in requests},
            generated_at=datetime.now(UTC),
            source=self.source,
        )


class UnavailableProvider(BaseDataProvider):
    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        raise ProviderUnavailableError("fixture unavailable")


def request(symbol: str = "MOCK_ETF_A", data_type: str = "price") -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="domestic_etf", data_type=data_type)


def test_router_routes_requests_by_data_type():
    price_provider = FixtureProvider("price_fixture", {"MOCK_ETF_A": {"price": 100.0}})
    macro_provider = FixtureProvider("macro_fixture", {"GDP": {"value": 1.2}})
    registry = DataHubRegistry()
    registry.register("prices", price_provider, {"price"})
    registry.register("macro", macro_provider, {"macro"})
    router = DataHubRouter(registry)

    bundle = router.get_data(
        [
            request("MOCK_ETF_A", "price"),
            DataRequest(symbol="GDP", asset_type="cash", data_type="macro"),
        ]
    )

    assert bundle.source == "router"
    assert bundle.data["MOCK_ETF_A"]["price"] == 100.0
    assert bundle.data["GDP"]["value"] == 1.2
    assert [item.symbol for item in price_provider.received_requests] == ["MOCK_ETF_A"]
    assert [item.symbol for item in macro_provider.received_requests] == ["GDP"]


def test_router_merges_payloads_for_same_symbol_from_multiple_providers():
    price_provider = FixtureProvider(
        "price_fixture",
        {"MOCK_ETF_A": {"latest_price": {"price": 100.0}}},
    )
    bars_provider = FixtureProvider(
        "bars_fixture",
        {"MOCK_ETF_A": {"bars": [{"close": 100.0}]}},
    )
    registry = DataHubRegistry()
    registry.register("prices", price_provider, {"price"})
    registry.register("bars", bars_provider, {"ohlcv"})
    router = DataHubRouter(registry)

    payload = router.get_data(
        [
            request("MOCK_ETF_A", "price"),
            request("MOCK_ETF_A", "ohlcv"),
        ]
    ).data["MOCK_ETF_A"]

    assert payload["latest_price"]["price"] == 100.0
    assert payload["bars"][0]["close"] == 100.0


def test_router_raises_no_provider_for_supported_unregistered_type():
    registry = DataHubRegistry()
    registry.register("prices", FixtureProvider("price_fixture", {}), {"price"})
    router = DataHubRouter(registry)

    with pytest.raises(NoProviderError, match="No DataHub provider"):
        router.get_data([request("MOCK_ETF_A", "news")])


def test_router_raises_for_unsupported_data_type():
    router = DataHubRouter(DataHubRegistry())

    with pytest.raises(UnsupportedDataTypeError, match="Unsupported DataHub data_type"):
        router.get_data([request("MOCK_ETF_A", "unknown_type")])


def test_router_raises_provider_unavailable_when_only_matches_are_unavailable():
    registry = DataHubRegistry()
    registry.register(
        "prices",
        FixtureProvider("price_fixture", {"MOCK_ETF_A": {"price": 100.0}}),
        {"price"},
        available=False,
    )
    router = DataHubRouter(registry)

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        router.get_data([request()])


def test_router_normalizes_pydantic_payloads_and_preserves_fresh_metadata():
    generated_at = datetime.now(UTC)
    provider = FixtureProvider(
        "fixture",
        {
            "MOCK_ETF_A": SymbolData(
                symbol="MOCK_ETF_A",
                latest_price=PricePoint(
                    symbol="MOCK_ETF_A",
                    timestamp=generated_at,
                    price=100.0,
                    source="fixture",
                ),
                is_stale=False,
                warnings=[],
            )
        },
    )
    registry = DataHubRegistry()
    registry.register("prices", provider, {"price"})

    payload = DataHubRouter(registry).get_data([request()]).data["MOCK_ETF_A"]

    assert payload["latest_price"]["price"] == 100.0
    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_router_rejects_stale_data_when_stale_is_not_allowed():
    generated_at = datetime.now(UTC)
    provider = FixtureProvider(
        "fixture",
        {
            "MOCK_ETF_A": SymbolData(
                symbol="MOCK_ETF_A",
                latest_price=PricePoint(
                    symbol="MOCK_ETF_A",
                    timestamp=generated_at,
                    price=100.0,
                    source="fixture",
                ),
                is_stale=True,
            )
        },
    )
    registry = DataHubRegistry()
    registry.register("prices", provider, {"price"})

    with pytest.raises(StaleDataError, match="Stale data"):
        DataHubRouter(registry, allow_stale=False).get_data([request()])


def test_router_preserves_legacy_price_payloads_for_backward_compatibility():
    provider = FixtureProvider("legacy", {"MOCK_ETF_A": {"price": 100.0}})
    registry = DataHubRegistry()
    registry.register("legacy", provider, {"price"})

    payload = DataHubRouter(registry).get_data([request()]).data["MOCK_ETF_A"]

    assert payload == {"price": 100.0}


def test_router_respects_run_mode_constraints():
    registry = DataHubRegistry()
    registry.register(
        "paper_prices",
        FixtureProvider("paper", {"MOCK_ETF_A": {"price": 100.0}}),
        {"price"},
        run_modes={RunMode.PAPER},
    )

    assert DataHubRouter(registry, run_mode=RunMode.PAPER).get_data([request()]).source == "paper"
    with pytest.raises(NoProviderError):
        DataHubRouter(registry, run_mode=RunMode.LIVE_READONLY).get_data([request()])


def test_router_prefers_lower_priority_provider():
    fallback_provider = FixtureProvider("fallback", {"MOCK_ETF_A": {"price": 99.0}})
    preferred_provider = FixtureProvider("preferred", {"MOCK_ETF_A": {"price": 101.0}})
    registry = DataHubRegistry()
    registry.register("fallback", fallback_provider, {"price"}, priority=100)
    registry.register("preferred", preferred_provider, {"price"}, priority=10)

    bundle = DataHubRouter(registry).get_data([request()])

    assert bundle.source == "preferred"
    assert bundle.data["MOCK_ETF_A"]["price"] == 101.0
    assert fallback_provider.received_requests == []


def test_router_uses_registration_order_when_priority_matches():
    first_provider = FixtureProvider("first", {"MOCK_ETF_A": {"price": 100.0}})
    second_provider = FixtureProvider("second", {"MOCK_ETF_A": {"price": 101.0}})
    registry = DataHubRegistry()
    registry.register("first", first_provider, {"price"}, priority=50)
    registry.register("second", second_provider, {"price"}, priority=50)

    bundle = DataHubRouter(registry).get_data([request()])

    assert bundle.source == "first"
    assert bundle.data["MOCK_ETF_A"]["price"] == 100.0
    assert second_provider.received_requests == []


def test_router_tries_next_provider_when_preferred_provider_is_unavailable():
    fallback_provider = FixtureProvider("fallback", {"MOCK_ETF_A": {"price": 100.0}})
    registry = DataHubRegistry()
    registry.register("yahoo", UnavailableProvider(), {"price"}, priority=10)
    registry.register("fallback", fallback_provider, {"price"}, priority=100)

    bundle = DataHubRouter(registry).get_data([request()])

    assert bundle.source == "fallback"
    assert bundle.data["MOCK_ETF_A"]["price"] == 100.0


def test_build_data_provider_keeps_mock_and_csv_configs_working():
    mock_config = DataHubConfig(provider="mock")
    mock_provider = build_data_provider(mock_config)
    mock_bundle = mock_provider.get_data([request()])

    csv_config = DataHubConfig(provider="csv", csv_path="data/sample_prices.csv")
    csv_provider = build_data_provider(csv_config)
    csv_bundle = csv_provider.get_data([request()])

    assert mock_bundle.data["MOCK_ETF_A"]["latest_price"]["price"] == 100.0
    assert csv_bundle.data["MOCK_ETF_A"]["latest_price"]["price"] == 103.0


def test_build_data_provider_supports_multi_provider_config_with_priority():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="csv_prices",
                provider="csv",
                priority=20,
                data_types=["price", "ohlcv"],
                csv_path="data/sample_prices.csv",
            ),
            DataHubProviderConfig(
                name="mock_fallback",
                provider="mock",
                priority=100,
                data_types=["price", "ohlcv"],
            ),
        ]
    )

    bundle = build_data_provider(config).get_data([request()])

    assert bundle.source == "csv"
    assert bundle.data["MOCK_ETF_A"]["latest_price"]["price"] == 103.0


def test_build_data_provider_accepts_yahoo_config_without_network_call():
    provider = build_data_provider(DataHubConfig(provider="yahoo"))

    assert isinstance(provider, DataHubRouter)


def test_single_datahub_config_normalizes_to_provider_config():
    config = DataHubConfig(
        provider="newsapi",
        stale_after_seconds=300,
        symbol_map={"FED": "Federal Reserve"},
        newsapi_language="en",
    )

    provider = config.effective_providers()[0]

    assert provider.name == "newsapi"
    assert provider.provider == "newsapi"
    assert provider.data_types == ["news"]
    assert provider.stale_after_seconds == 300
    assert provider.symbol_map == {"FED": "Federal Reserve"}
    assert provider.newsapi_language == "en"


def test_single_yahoo_config_preserves_research_and_technical_data_types():
    provider = build_data_provider(DataHubConfig(provider="yahoo"))

    assert provider.registry.registrations_for(request("MOCK_ETF_A", "fundamental"))
    assert provider.registry.registrations_for(request("MOCK_ETF_A", "financial_statements"))
    assert provider.registry.registrations_for(request("MOCK_ETF_A", "technical_indicators"))


def test_build_data_provider_accepts_yahoo_llm_research_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="yahoo",
                provider="yahoo",
                data_types=[
                    "price",
                    "ohlcv",
                    "fundamental",
                    "financial_statements",
                    "technical_indicators",
                ],
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)
    assert provider.registry.registrations_for(request("MOCK_ETF_A", "fundamental"))
    assert provider.registry.registrations_for(request("MOCK_ETF_A", "financial_statements"))
    assert provider.registry.registrations_for(request("MOCK_ETF_A", "technical_indicators"))


def test_build_data_provider_rejects_unsupported_yahoo_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="yahoo",
                provider="yahoo",
                data_types=["price", "news"],
            )
        ]
    )

    with pytest.raises(ValueError, match="technical_indicators"):
        build_data_provider(config)


def test_build_data_provider_accepts_fred_config_without_network_call():
    provider = build_data_provider(DataHubConfig(provider="fred", api_key_env="FRED_TEST_API_KEY"))

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_defaults_fred_multi_provider_to_macro():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="fred",
                provider="fred",
                api_key_env="FRED_TEST_API_KEY",
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_rejects_unsupported_fred_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="fred",
                provider="fred",
                data_types=["macro", "price"],
            )
        ]
    )

    with pytest.raises(ValueError, match="supports only macro"):
        build_data_provider(config)


def test_build_data_provider_accepts_rss_config_without_network_call():
    provider = build_data_provider(
        DataHubConfig(provider="rss", feed_urls=["https://example.test/rss"])
    )

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_defaults_rss_multi_provider_to_news():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="rss",
                provider="rss",
                feed_urls=["https://example.test/rss"],
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_requires_rss_feed_urls():
    with pytest.raises(ValueError, match="requires at least one feed URL"):
        build_data_provider(DataHubConfig(provider="rss"))


def test_build_data_provider_rejects_blank_rss_feed_url():
    with pytest.raises(ValueError, match="feed URLs must not be blank"):
        build_data_provider(DataHubConfig(provider="rss", feed_urls=[" "]))


def test_build_data_provider_rejects_unsupported_rss_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="rss",
                provider="rss",
                data_types=["news", "sentiment"],
                feed_urls=["https://example.test/rss"],
            )
        ]
    )

    with pytest.raises(ValueError, match="supports only news"):
        build_data_provider(config)


def test_build_data_provider_accepts_sentiment_config_without_network_call():
    provider = build_data_provider(
        DataHubConfig(
            provider="sentiment",
            sentiment_texts=["SPY posts strong gains"],
            symbol_map={"SPY": "SPY"},
        )
    )

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_defaults_sentiment_multi_provider_to_sentiment():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="sentiment",
                provider="sentiment",
                sentiment_texts=["SPY posts strong gains"],
                symbol_map={"SPY": "SPY"},
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)


def test_build_data_provider_rejects_unsupported_sentiment_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="sentiment",
                provider="sentiment",
                data_types=["sentiment", "news"],
                sentiment_texts=["SPY posts strong gains"],
                symbol_map={"SPY": "SPY"},
            )
        ]
    )

    with pytest.raises(ValueError, match="supports only sentiment"):
        build_data_provider(config)


def test_router_integrates_yahoo_provider_with_fixture_client():
    class FakeYahooClient:
        def history(
            self,
            symbol: str,
            *,
            period: str,
            interval: str,
            timeout_seconds: float,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000,
                }
            ]

    registry = DataHubRegistry()
    registry.register("yahoo", YahooDataProvider(client=FakeYahooClient()), {"price", "ohlcv"})

    bundle = DataHubRouter(registry).get_data(
        [DataRequest(symbol="SPY", asset_type="us_etf", data_type="price")]
    )

    assert bundle.source == "yahoo"
    assert bundle.data["SPY"]["latest_price"]["price"] == 100.5


def test_router_integrates_fred_provider_with_fixture_client(monkeypatch: pytest.MonkeyPatch):
    class FakeFREDClient:
        def observations(
            self,
            series_id: str,
            *,
            api_key: str,
            timeout_seconds: float,
        ) -> dict[str, Any]:
            return {"observations": [{"date": "2025-01-01", "value": "1.2"}]}

    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    registry = DataHubRegistry()
    registry.register(
        "fred",
        FREDDataProvider(client=FakeFREDClient(), api_key_env="FRED_TEST_API_KEY"),
        {"macro"},
    )

    bundle = DataHubRouter(registry).get_data(
        [DataRequest(symbol="GDP", asset_type="cash", data_type="macro")]
    )

    assert bundle.source == "fred"
    assert bundle.data["GDP"]["latest"]["value"] == 1.2


def test_router_integrates_rss_provider_with_fixture_client():
    class FakeRSSClient:
        def fetch(self, url: str, *, timeout_seconds: float) -> str:
            return """
            <rss version="2.0">
              <channel>
                <title>Fixture News</title>
                <item>
                  <title>Market story</title>
                  <link>https://example.test/story</link>
                  <pubDate>Wed, 01 Jan 2026 00:00:00 GMT</pubDate>
                </item>
              </channel>
            </rss>
            """

    registry = DataHubRegistry()
    registry.register(
        "rss",
        RSSNewsProvider(feed_urls=["https://example.test/rss"], client=FakeRSSClient()),
        {"news"},
    )

    bundle = DataHubRouter(registry).get_data(
        [DataRequest(symbol="MARKET", asset_type="cash", data_type="news")]
    )

    assert bundle.source == "rss"
    assert bundle.data["MARKET"]["latest"]["title"] == "Market story"


def test_router_integrates_sentiment_provider_with_fixture_text():
    registry = DataHubRegistry()
    registry.register(
        "sentiment",
        RuleBasedSentimentProvider(
            texts=["SPY posts strong gains"],
            symbol_map={"SPY": "SPY"},
        ),
        {"sentiment"},
    )

    bundle = DataHubRouter(registry).get_data(
        [DataRequest(symbol="SPY", asset_type="us_etf", data_type="sentiment")]
    )

    assert bundle.source == "sentiment"
    assert bundle.data["SPY"]["label"] == "positive"
