import urllib.error
from datetime import UTC, datetime
from typing import Any

import pytest

from maestro.config.models import DataHubConfig, DataHubProviderConfig
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.gdelt_provider import GDELTNewsProvider, StdlibGDELTClient
from maestro.datahub.registry import DataHubRegistry
from maestro.datahub.resilience import ResilientDataProvider
from maestro.datahub.router import DataHubRouter
from maestro.sdk import DataBundle, DataRequest


class FakeGDELTClient:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"articles": []}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def articles(
        self,
        query: str,
        *,
        base_url: str,
        timespan: str,
        max_records: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "base_url": base_url,
                "timespan": timespan,
                "max_records": max_records,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.payload


class FixtureProvider(BaseDataProvider):
    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        return DataBundle(
            requests=requests,
            data={
                request.symbol: {
                    "symbol": request.symbol,
                    "items": [{"title": "fallback", "url": "https://example.test/fallback"}],
                    "latest": {"title": "fallback", "url": "https://example.test/fallback"},
                    "is_stale": False,
                    "warnings": [],
                    "source": "fixture",
                }
                for request in requests
            },
            generated_at=datetime.now(UTC),
            source="fixture",
        )


class UnavailableProvider(BaseDataProvider):
    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        raise ProviderUnavailableError("gdelt unavailable")


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def request(symbol: str = "FED", lookback: int | None = 2) -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="cash", data_type="news", lookback=lookback)


def gdelt_payload(*articles: dict[str, Any]) -> dict[str, Any]:
    return {"articles": list(articles)}


def gdelt_article(
    title: str = "Federal Reserve decision",
    url: str = "https://example.test/fed",
    seendate: str = "20260101010000",
    domain: str = "example.test",
) -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "seendate": seendate,
        "domain": domain,
        "language": "English",
        "sourcecountry": "United States",
        "socialimage": "https://example.test/image.jpg",
    }


def test_gdelt_provider_normalizes_article_list_from_fixture_payload():
    client = FakeGDELTClient(
        gdelt_payload(
            gdelt_article(
                title="Older Fed story",
                url="https://example.test/older",
                seendate="20260101000000",
            ),
            gdelt_article(
                title="Latest Fed story",
                url="https://example.test/latest",
                seendate="20260101010000",
            ),
        )
    )
    provider = GDELTNewsProvider(
        client=client,
        base_url="https://example.test/gdelt",
        timespan="12h",
        max_records=10,
        timeout_seconds=3.0,
    )

    bundle = provider.get_data([request()])

    payload = bundle.data["FED"]
    assert bundle.source == "gdelt"
    assert payload["symbol"] == "FED"
    assert payload["latest"]["title"] == "Latest Fed story"
    assert payload["latest"]["published_at"] == "2026-01-01T01:00:00+00:00"
    assert payload["latest"]["summary"] is None
    assert payload["latest"]["source"] == "example.test"
    assert payload["latest"]["domain"] == "example.test"
    assert payload["latest"]["language"] == "English"
    assert payload["latest"]["source_country"] == "United States"
    assert payload["latest"]["social_image"] == "https://example.test/image.jpg"
    assert len(payload["items"]) == 2
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
    assert client.calls == [
        {
            "query": "FED",
            "base_url": "https://example.test/gdelt",
            "timespan": "12h",
            "max_records": 10,
            "timeout_seconds": 3.0,
        }
    ]


def test_gdelt_provider_uses_symbol_map_as_query():
    client = FakeGDELTClient(gdelt_payload(gdelt_article()))
    provider = GDELTNewsProvider(client=client, symbol_map={"FED": "Federal Reserve"})

    provider.get_data([request("FED", lookback=1)])

    assert client.calls[0]["query"] == "Federal Reserve"


def test_gdelt_provider_applies_lookback_after_max_records_request_limit():
    client = FakeGDELTClient(
        gdelt_payload(
            gdelt_article("Newest", "https://example.test/newest", "20260101020000"),
            gdelt_article("Middle", "https://example.test/middle", "20260101010000"),
            gdelt_article("Oldest", "https://example.test/oldest", "20260101000000"),
        )
    )
    provider = GDELTNewsProvider(client=client, max_records=3)

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert [item["title"] for item in payload["items"]] == ["Newest"]
    assert client.calls[0]["max_records"] == 3


def test_gdelt_provider_rejects_empty_article_list():
    provider = GDELTNewsProvider(client=FakeGDELTClient({"articles": []}))

    with pytest.raises(ValueError, match="No GDELT news items"):
        provider.get_data([request()])


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "articles must be a list"),
        ({"articles": {"title": "bad"}}, "articles must be a list"),
        ({"articles": ["bad"]}, "article must be object"),
        (gdelt_payload({"url": "https://example.test/missing-title"}), "invalid article title"),
        (gdelt_payload(gdelt_article(seendate="not-a-date")), "invalid article date"),
    ],
)
def test_gdelt_provider_rejects_malformed_schema_or_dates(
    payload: dict[str, Any],
    message: str,
):
    provider = GDELTNewsProvider(client=FakeGDELTClient(payload))

    with pytest.raises(ValueError, match=message):
        provider.get_data([request()])


def test_stdlib_gdelt_client_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch):
    def fake_urlopen(request: object, *, timeout: float) -> FakeHTTPResponse:
        return FakeHTTPResponse(b"{not-json")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="invalid JSON"):
        StdlibGDELTClient().articles(
            "Federal Reserve",
            base_url="https://example.test/gdelt",
            timespan="24h",
            max_records=25,
            timeout_seconds=1.0,
        )


def test_stdlib_gdelt_client_preserves_http_status_in_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_urlopen(request: object, *, timeout: float) -> FakeHTTPResponse:
        raise urllib.error.HTTPError(
            url="https://example.test/gdelt",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ProviderUnavailableError, match="HTTP 429 Too Many Requests"):
        StdlibGDELTClient().articles(
            "Federal Reserve",
            base_url="https://example.test/gdelt",
            timespan="24h",
            max_records=25,
            timeout_seconds=1.0,
        )


@pytest.mark.parametrize(
    "error",
    [TimeoutError("slow"), urllib.error.URLError("down"), RuntimeError("down")],
)
def test_gdelt_provider_maps_timeout_and_transport_errors_to_unavailable(error: Exception):
    provider = GDELTNewsProvider(client=FakeGDELTClient(error=error))

    with pytest.raises(ProviderUnavailableError, match="GDELT provider"):
        provider.get_data([request()])


def test_gdelt_provider_marks_stale_payloads():
    provider = GDELTNewsProvider(
        client=FakeGDELTClient(gdelt_payload(gdelt_article(seendate="20200101000000"))),
        stale_after_seconds=60,
    )

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert payload["is_stale"] is True
    assert "GDELT news is stale" in payload["warnings"]


def test_gdelt_provider_preserves_fresh_metadata():
    provider = GDELTNewsProvider(
        client=FakeGDELTClient(
            gdelt_payload(gdelt_article(seendate=datetime.now(UTC).isoformat()))
        ),
        stale_after_seconds=3600,
    )

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_build_data_provider_accepts_gdelt_single_provider_without_network_call():
    provider = build_data_provider(DataHubConfig(provider="gdelt"))

    assert isinstance(provider, DataHubRouter)
    registration = provider.registry.registrations_for(request())[0]
    assert isinstance(registration.provider, ResilientDataProvider)
    assert registration.data_types == frozenset({"news"})


def test_build_data_provider_defaults_gdelt_multi_provider_to_news():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="gdelt",
                provider="gdelt",
                symbol_map={"FED": "Federal Reserve"},
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)
    assert provider.registry.registrations_for(request())


def test_build_data_provider_rejects_unsupported_gdelt_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="gdelt",
                provider="gdelt",
                data_types=["news", "sentiment"],
            )
        ]
    )

    with pytest.raises(ValueError, match="supports only news"):
        build_data_provider(config)


def test_router_falls_back_when_gdelt_is_unavailable_for_news():
    registry = DataHubRegistry()
    registry.register("gdelt", UnavailableProvider(), {"news"}, priority=10)
    registry.register("fixture", FixtureProvider(), {"news"}, priority=20)

    bundle = DataHubRouter(registry).get_data([request()])

    assert bundle.source == "fixture"
    assert bundle.data["FED"]["latest"]["title"] == "fallback"
