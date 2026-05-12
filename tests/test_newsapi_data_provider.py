import urllib.error
from datetime import UTC, datetime
from typing import Any

import pytest

from maestro.config.models import DataHubConfig, DataHubProviderConfig
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.newsapi_provider import NewsAPINewsProvider, StdlibNewsAPIClient
from maestro.datahub.registry import DataHubRegistry
from maestro.datahub.resilience import ResilientDataProvider
from maestro.datahub.router import DataHubRouter
from maestro.sdk import DataBundle, DataRequest


class FakeNewsAPIClient:
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
        api_key: str,
        base_url: str,
        page_size: int,
        sort_by: str,
        timeout_seconds: float,
        language: str | None,
        search_in: str | None,
        domains: list[str],
        exclude_domains: list[str],
        sources: list[str],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "api_key": api_key,
                "base_url": base_url,
                "page_size": page_size,
                "sort_by": sort_by,
                "timeout_seconds": timeout_seconds,
                "language": language,
                "search_in": search_in,
                "domains": domains,
                "exclude_domains": exclude_domains,
                "sources": sources,
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
        raise ProviderUnavailableError("newsapi unavailable")


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


def newsapi_payload(*articles: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "totalResults": len(articles), "articles": list(articles)}


def newsapi_article(
    title: str = "Federal Reserve decision",
    url: str = "https://example.test/fed",
    published_at: str = "2026-01-01T01:00:00Z",
    source_name: str = "Example News",
) -> dict[str, Any]:
    return {
        "source": {"id": "example", "name": source_name},
        "author": "Reporter",
        "title": title,
        "description": "Fed policy summary",
        "url": url,
        "urlToImage": "https://example.test/image.jpg",
        "publishedAt": published_at,
        "content": "Fuller content",
    }


def test_newsapi_provider_normalizes_article_list_from_fixture_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    client = FakeNewsAPIClient(
        newsapi_payload(
            newsapi_article(
                title="Older Fed story",
                url="https://example.test/older",
                published_at="2026-01-01T00:00:00Z",
            ),
            newsapi_article(
                title="Latest Fed story",
                url="https://example.test/latest",
                published_at="2026-01-01T01:00:00Z",
            ),
        )
    )
    provider = NewsAPINewsProvider(
        client=client,
        base_url="https://example.test/newsapi",
        api_key_env="NEWSAPI_TEST_KEY",
        page_size=10,
        sort_by="publishedAt",
        timeout_seconds=3.0,
    )

    bundle = provider.get_data([request()])

    payload = bundle.data["FED"]
    assert bundle.source == "newsapi"
    assert payload["symbol"] == "FED"
    assert payload["latest"]["title"] == "Latest Fed story"
    assert payload["latest"]["published_at"] == "2026-01-01T01:00:00+00:00"
    assert payload["latest"]["summary"] == "Fed policy summary"
    assert payload["latest"]["source"] == "Example News"
    assert payload["latest"]["source_id"] == "example"
    assert payload["latest"]["author"] == "Reporter"
    assert payload["latest"]["url_to_image"] == "https://example.test/image.jpg"
    assert payload["latest"]["content"] == "Fuller content"
    assert len(payload["items"]) == 2
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
    assert client.calls == [
        {
            "query": "FED",
            "api_key": "secret-value",
            "base_url": "https://example.test/newsapi",
            "page_size": 10,
            "sort_by": "publishedAt",
            "timeout_seconds": 3.0,
            "language": None,
            "search_in": None,
            "domains": [],
            "exclude_domains": [],
            "sources": [],
        }
    ]


def test_stdlib_newsapi_client_sends_api_key_header_and_query_params(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict[str, Any] = {}

    def fake_urlopen(request_obj: object, *, timeout: float) -> FakeHTTPResponse:
        seen["headers"] = request_obj.headers
        seen["url"] = request_obj.full_url
        seen["timeout"] = timeout
        return FakeHTTPResponse(b'{"status":"ok","articles":[]}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    StdlibNewsAPIClient().articles(
        "Federal Reserve",
        api_key="secret-value",
        base_url="https://example.test/newsapi",
        page_size=25,
        sort_by="publishedAt",
        timeout_seconds=1.5,
        language="en",
        search_in="title,description",
        domains=["example.test"],
        exclude_domains=["exclude.test"],
        sources=["associated-press"],
    )

    assert seen["headers"]["X-api-key"] == "secret-value"
    assert seen["timeout"] == 1.5
    assert "q=Federal+Reserve" in seen["url"]
    assert "language=en" in seen["url"]
    assert "searchIn=title%2Cdescription" in seen["url"]
    assert "domains=example.test" in seen["url"]
    assert "excludeDomains=exclude.test" in seen["url"]
    assert "sources=associated-press" in seen["url"]


def test_newsapi_provider_uses_symbol_map_as_query(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    client = FakeNewsAPIClient(newsapi_payload(newsapi_article()))
    provider = NewsAPINewsProvider(
        client=client,
        api_key_env="NEWSAPI_TEST_KEY",
        symbol_map={"FED": "Federal Reserve"},
    )

    provider.get_data([request("FED", lookback=1)])

    assert client.calls[0]["query"] == "Federal Reserve"


def test_newsapi_provider_applies_lookback_after_page_size_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    client = FakeNewsAPIClient(
        newsapi_payload(
            newsapi_article("Newest", "https://example.test/newest", "2026-01-01T02:00:00Z"),
            newsapi_article("Middle", "https://example.test/middle", "2026-01-01T01:00:00Z"),
            newsapi_article("Oldest", "https://example.test/oldest", "2026-01-01T00:00:00Z"),
        )
    )
    provider = NewsAPINewsProvider(client=client, api_key_env="NEWSAPI_TEST_KEY", page_size=3)

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert [item["title"] for item in payload["items"]] == ["Newest"]
    assert client.calls[0]["page_size"] == 3


def test_newsapi_provider_passes_optional_request_params(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    client = FakeNewsAPIClient(newsapi_payload(newsapi_article()))
    provider = NewsAPINewsProvider(
        client=client,
        api_key_env="NEWSAPI_TEST_KEY",
        language="en",
        search_in="title,description,content",
        domains=["example.test"],
        exclude_domains=["excluded.test"],
        sources=["associated-press"],
    )

    provider.get_data([request(lookback=1)])

    assert client.calls[0]["language"] == "en"
    assert client.calls[0]["search_in"] == "title,description,content"
    assert client.calls[0]["domains"] == ["example.test"]
    assert client.calls[0]["exclude_domains"] == ["excluded.test"]
    assert client.calls[0]["sources"] == ["associated-press"]


def test_newsapi_provider_requires_api_key_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NEWSAPI_TEST_KEY", raising=False)
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(newsapi_payload(newsapi_article())),
        api_key_env="NEWSAPI_TEST_KEY",
    )

    with pytest.raises(ProviderUnavailableError, match="NEWSAPI_TEST_KEY"):
        provider.get_data([request()])


@pytest.mark.parametrize(
    "error",
    [TimeoutError("slow"), urllib.error.URLError("down"), RuntimeError("down")],
)
def test_newsapi_provider_maps_timeout_and_transport_errors_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(error=error),
        api_key_env="NEWSAPI_TEST_KEY",
    )

    with pytest.raises(ProviderUnavailableError, match="NewsAPI provider"):
        provider.get_data([request()])


@pytest.mark.parametrize("status_code", [401, 429, 500, 503])
def test_stdlib_newsapi_client_maps_http_errors_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
):
    def fake_urlopen(request_obj: object, *, timeout: float) -> FakeHTTPResponse:
        raise urllib.error.HTTPError(
            url="https://example.test/newsapi",
            code=status_code,
            msg="error",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ProviderUnavailableError, match=f"HTTP {status_code}"):
        StdlibNewsAPIClient().articles(
            "Federal Reserve",
            api_key="secret-value",
            base_url="https://example.test/newsapi",
            page_size=25,
            sort_by="publishedAt",
            timeout_seconds=1.0,
            language=None,
            search_in=None,
            domains=[],
            exclude_domains=[],
            sources=[],
        )


def test_newsapi_provider_maps_newsapi_status_error_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(
            {"status": "error", "code": "rateLimited", "message": "quota exceeded"}
        ),
        api_key_env="NEWSAPI_TEST_KEY",
    )

    with pytest.raises(ProviderUnavailableError, match="rateLimited"):
        provider.get_data([request()])


def test_stdlib_newsapi_client_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch):
    def fake_urlopen(request_obj: object, *, timeout: float) -> FakeHTTPResponse:
        return FakeHTTPResponse(b"{not-json")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="invalid JSON"):
        StdlibNewsAPIClient().articles(
            "Federal Reserve",
            api_key="secret-value",
            base_url="https://example.test/newsapi",
            page_size=25,
            sort_by="publishedAt",
            timeout_seconds=1.0,
            language=None,
            search_in=None,
            domains=[],
            exclude_domains=[],
            sources=[],
        )


def test_newsapi_provider_rejects_empty_article_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient({"status": "ok", "articles": []}),
        api_key_env="NEWSAPI_TEST_KEY",
    )

    with pytest.raises(ValueError, match="No NewsAPI news items"):
        provider.get_data([request()])


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "articles must be a list"),
        ({"articles": {"title": "bad"}}, "articles must be a list"),
        ({"articles": ["bad"]}, "article must be object"),
        (newsapi_payload({"url": "https://example.test/missing-title"}), "invalid article source"),
        (
            newsapi_payload({**newsapi_article(), "title": ""}),
            "invalid article title",
        ),
        (
            newsapi_payload({**newsapi_article(), "publishedAt": "not-a-date"}),
            "invalid article date",
        ),
    ],
)
def test_newsapi_provider_rejects_malformed_schema_or_dates(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(payload),
        api_key_env="NEWSAPI_TEST_KEY",
    )

    with pytest.raises(ValueError, match=message):
        provider.get_data([request()])


def test_newsapi_provider_marks_stale_payloads(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(
            newsapi_payload(newsapi_article(published_at="2020-01-01T00:00:00Z"))
        ),
        api_key_env="NEWSAPI_TEST_KEY",
        stale_after_seconds=60,
    )

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert payload["is_stale"] is True
    assert "NewsAPI news is stale" in payload["warnings"]


def test_newsapi_provider_preserves_fresh_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEWSAPI_TEST_KEY", "secret-value")
    provider = NewsAPINewsProvider(
        client=FakeNewsAPIClient(
            newsapi_payload(newsapi_article(published_at=datetime.now(UTC).isoformat()))
        ),
        api_key_env="NEWSAPI_TEST_KEY",
        stale_after_seconds=3600,
    )

    payload = provider.get_data([request(lookback=1)]).data["FED"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_build_data_provider_accepts_newsapi_single_provider_without_network_call():
    provider = build_data_provider(DataHubConfig(provider="newsapi"))

    assert isinstance(provider, DataHubRouter)
    registration = provider.registry.registrations_for(request())[0]
    assert isinstance(registration.provider, ResilientDataProvider)
    assert registration.data_types == frozenset({"news"})


def test_build_data_provider_defaults_newsapi_multi_provider_to_news():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="newsapi",
                provider="newsapi",
                symbol_map={"FED": "Federal Reserve"},
                newsapi_language="en",
            )
        ]
    )

    provider = build_data_provider(config)

    assert isinstance(provider, DataHubRouter)
    assert provider.registry.registrations_for(request())


def test_build_data_provider_rejects_unsupported_newsapi_data_types():
    config = DataHubConfig(
        providers=[
            DataHubProviderConfig(
                name="newsapi",
                provider="newsapi",
                data_types=["news", "sentiment"],
            )
        ]
    )

    with pytest.raises(ValueError, match="supports only news"):
        build_data_provider(config)


def test_router_falls_back_when_newsapi_is_unavailable_for_news():
    registry = DataHubRegistry()
    registry.register("newsapi", UnavailableProvider(), {"news"}, priority=10)
    registry.register("fixture", FixtureProvider(), {"news"}, priority=20)

    bundle = DataHubRouter(registry).get_data([request()])

    assert bundle.source == "fixture"
    assert bundle.data["FED"]["latest"]["title"] == "fallback"
