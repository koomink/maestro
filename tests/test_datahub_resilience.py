from datetime import UTC, datetime

import pytest

from maestro.config.models import DataHubConfig, DataHubProviderConfig
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.resilience import ResilientDataProvider
from maestro.sdk import DataBundle, DataRequest


def request(symbol: str = "SPY") -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="us_etf", data_type="price")


def test_resilient_provider_retries_provider_unavailable_once():
    provider = FlakyProvider(failures_before_success=1)
    resilient = ResilientDataProvider(provider, retry_max_attempts=2)

    bundle = resilient.get_data([request()])

    assert bundle.data["SPY"]["price"] == 101.0
    assert provider.calls == 2


def test_resilient_provider_does_not_retry_validation_errors():
    provider = ValidationErrorProvider()
    resilient = ResilientDataProvider(provider, retry_max_attempts=3)

    with pytest.raises(ValueError, match="bad payload"):
        resilient.get_data([request()])

    assert provider.calls == 1


def test_resilient_provider_uses_ttl_cache_until_expiry():
    clock = FakeClock()
    provider = CountingProvider()
    resilient = ResilientDataProvider(
        provider,
        cache_ttl_seconds=60,
        monotonic_fn=clock.monotonic,
    )

    first = resilient.get_data([request()])
    second = resilient.get_data([request()])
    clock.advance(61)
    third = resilient.get_data([request()])

    assert first.data["SPY"]["price"] == 101.0
    assert second.data["SPY"]["price"] == 101.0
    assert third.data["SPY"]["price"] == 102.0
    assert provider.calls == 2


def test_resilient_provider_enforces_min_request_interval():
    clock = FakeClock()
    provider = CountingProvider()
    resilient = ResilientDataProvider(
        provider,
        min_request_interval_seconds=5,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    resilient.get_data([request("SPY")])
    resilient.get_data([request("QQQ")])

    assert clock.sleeps == [5.0]
    assert provider.calls == 2


def test_build_data_provider_wraps_external_providers_only():
    yahoo_router = build_data_provider(
        DataHubConfig(provider="yahoo", retry_max_attempts=2, cache_ttl_seconds=30)
    )
    csv_router = build_data_provider(
        DataHubConfig(provider="csv", csv_path="data/sample_prices.csv")
    )
    multi_router = build_data_provider(
        DataHubConfig(
            providers=[
                DataHubProviderConfig(
                    name="rss",
                    provider="rss",
                    data_types=["news"],
                    feed_urls=["https://example.test/rss"],
                    retry_max_attempts=2,
                    cache_ttl_seconds=30,
                )
            ]
        )
    )

    assert isinstance(
        yahoo_router.registry.registrations_for(request())[0].provider, ResilientDataProvider
    )
    assert not isinstance(
        csv_router.registry.registrations_for(request())[0].provider, ResilientDataProvider
    )
    rss_request = DataRequest(symbol="MARKET", asset_type="cash", data_type="news")
    assert isinstance(
        multi_router.registry.registrations_for(rss_request)[0].provider,
        ResilientDataProvider,
    )


class FlakyProvider(BaseDataProvider):
    def __init__(self, *, failures_before_success: int) -> None:
        self.failures_before_success = failures_before_success
        self.calls = 0

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise ProviderUnavailableError("temporary outage")
        return _bundle(requests, price=101.0)


class ValidationErrorProvider(BaseDataProvider):
    def __init__(self) -> None:
        self.calls = 0

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        self.calls += 1
        raise ValueError("bad payload")


class CountingProvider(BaseDataProvider):
    def __init__(self) -> None:
        self.calls = 0

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        self.calls += 1
        return _bundle(requests, price=100.0 + self.calls)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _bundle(requests: list[DataRequest], *, price: float) -> DataBundle:
    return DataBundle(
        requests=requests,
        data={request.symbol: {"price": price} for request in requests},
        generated_at=datetime.now(UTC),
        source="fixture",
    )
