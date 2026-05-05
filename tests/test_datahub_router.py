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
from maestro.datahub.registry import DataHubRegistry
from maestro.datahub.router import DataHubRouter
from maestro.datahub.schemas import PricePoint, SymbolData
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
