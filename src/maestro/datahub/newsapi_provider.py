import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.sdk import DataBundle, DataRequest

NEWSAPI_SORT_BY_VALUES = {"publishedAt", "relevancy", "popularity"}


class NewsAPIArticleClient(Protocol):
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
        domains: Sequence[str],
        exclude_domains: Sequence[str],
        sources: Sequence[str],
    ) -> Mapping[str, Any]:
        """Return a NewsAPI /v2/everything payload."""


class StdlibNewsAPIClient:
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
        domains: Sequence[str],
        exclude_domains: Sequence[str],
        sources: Sequence[str],
    ) -> Mapping[str, Any]:
        params: dict[str, str | int] = {
            "q": query,
            "sortBy": sort_by,
            "pageSize": page_size,
        }
        if language:
            params["language"] = language
        if search_in:
            params["searchIn"] = search_in
        if domains:
            params["domains"] = ",".join(domains)
        if exclude_domains:
            params["excludeDomains"] = ",".join(exclude_domains)
        if sources:
            params["sources"] = ",".join(sources)

        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "maestro-datahub/0.3", "X-Api-Key": api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"NewsAPI provider timed out for query: {query}"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailableError(
                f"NewsAPI provider HTTP {exc.code} for query: {query}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(
                f"NewsAPI provider is unavailable for query: {query}"
            ) from exc

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed NewsAPI payload for {query}: invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Malformed NewsAPI payload for {query}: expected object")
        return decoded


class NewsAPINewsProvider(BaseDataProvider):
    source = "newsapi"

    def __init__(
        self,
        *,
        client: NewsAPIArticleClient | None = None,
        base_url: str = "https://newsapi.org/v2/everything",
        api_key_env: str = "NEWSAPI_API_KEY",
        page_size: int = 25,
        sort_by: str = "publishedAt",
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
        language: str | None = None,
        search_in: str | None = None,
        domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
    ) -> None:
        normalized_base_url = base_url.strip()
        normalized_api_key_env = api_key_env.strip()
        normalized_sort_by = sort_by.strip()
        if not normalized_base_url:
            raise ValueError("NewsAPI provider base URL must not be blank")
        if not normalized_api_key_env:
            raise ValueError("NewsAPI provider API key environment variable must not be blank")
        if page_size < 1 or page_size > 100:
            raise ValueError("NewsAPI provider page_size must be between 1 and 100")
        if normalized_sort_by not in NEWSAPI_SORT_BY_VALUES:
            raise ValueError(
                "NewsAPI provider sort_by must be one of: "
                + ", ".join(sorted(NEWSAPI_SORT_BY_VALUES))
            )
        self.client = client or StdlibNewsAPIClient()
        self.base_url = normalized_base_url
        self.api_key_env = normalized_api_key_env
        self.page_size = page_size
        self.sort_by = normalized_sort_by
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})
        self.language = self._optional_text(language)
        self.search_in = self._optional_text(search_in)
        self.domains = self._normalize_text_list(domains or [])
        self.exclude_domains = self._normalize_text_list(exclude_domains or [])
        self.sources = self._normalize_text_list(sources or [])

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise ProviderUnavailableError(
                f"NewsAPI API key environment variable is not set: {self.api_key_env}"
            )

        generated_at = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            query = self.symbol_map.get(request.symbol, request.symbol)
            raw = self._fetch_articles(request.symbol, query, api_key)
            items = self._normalize_articles(request.symbol, raw)
            if not items:
                raise ValueError(f"No NewsAPI news items for symbol: {request.symbol}")

            lookback = request.lookback or len(items)
            selected = items[:lookback]
            warnings = []
            if lookback > len(items):
                warnings.append(
                    f"Requested lookback {lookback} exceeds available items {len(items)}"
                )

            is_stale = self._is_stale(selected, generated_at)
            if is_stale:
                warnings.append("NewsAPI news is stale")

            data[request.symbol] = {
                "symbol": request.symbol,
                "items": selected,
                "latest": selected[0],
                "is_stale": is_stale,
                "warnings": warnings,
                "source": self.source,
            }

        return DataBundle(
            requests=requests, data=data, generated_at=generated_at, source=self.source
        )

    def _fetch_articles(self, symbol: str, query: str, api_key: str) -> Mapping[str, Any]:
        try:
            return self.client.articles(
                query,
                api_key=api_key,
                base_url=self.base_url,
                page_size=self.page_size,
                sort_by=self.sort_by,
                timeout_seconds=self.timeout_seconds,
                language=self.language,
                search_in=self.search_in,
                domains=self.domains,
                exclude_domains=self.exclude_domains,
                sources=self.sources,
            )
        except ProviderUnavailableError:
            raise
        except ValueError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"NewsAPI provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"NewsAPI provider is unavailable for symbol: {symbol}"
            ) from exc

    def _normalize_articles(self, symbol: str, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        if raw.get("status") == "error":
            code = self._optional_text(raw.get("code"))
            message = self._optional_text(raw.get("message"))
            detail = f": {code}" if code else ""
            if message:
                detail = f"{detail}: {message}"
            raise ProviderUnavailableError(f"NewsAPI provider returned error{detail}")

        raw_articles = raw.get("articles")
        if not isinstance(raw_articles, Sequence) or isinstance(raw_articles, str | bytes):
            raise ValueError(f"Malformed NewsAPI payload for {symbol}: articles must be a list")

        items = []
        for article in raw_articles:
            if not isinstance(article, Mapping):
                raise ValueError(f"Malformed NewsAPI payload for {symbol}: article must be object")
            source = article.get("source")
            if not isinstance(source, Mapping):
                raise ValueError(f"Malformed NewsAPI payload for {symbol}: invalid article source")
            published_at = self._parse_datetime(article.get("publishedAt"), symbol)
            items.append(
                {
                    "title": self._required_text(article.get("title"), symbol, "title"),
                    "url": self._required_text(article.get("url"), symbol, "url"),
                    "published_at": published_at.isoformat(),
                    "summary": self._optional_text(article.get("description")),
                    "source": self._required_text(source.get("name"), symbol, "source.name"),
                    "source_id": self._optional_text(source.get("id")),
                    "author": self._optional_text(article.get("author")),
                    "url_to_image": self._optional_text(article.get("urlToImage")),
                    "content": self._optional_text(article.get("content")),
                }
            )
        return sorted(items, key=self._sort_key, reverse=True)

    def _required_text(self, value: Any, symbol: str, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Malformed NewsAPI payload for {symbol}: invalid article {field}")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Malformed NewsAPI payload for {symbol}: invalid article {field}")
        return normalized

    def _optional_text(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _normalize_text_list(self, values: Sequence[str]) -> list[str]:
        normalized = []
        for value in values:
            text = self._optional_text(value)
            if text is not None:
                normalized.append(text)
        return normalized

    def _parse_datetime(self, value: Any, symbol: str) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"Malformed NewsAPI payload for {symbol}: invalid article date")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Malformed NewsAPI payload for {symbol}: invalid article date"
            ) from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _is_stale(self, items: list[dict[str, Any]], generated_at: datetime) -> bool:
        if self.stale_after_seconds is None:
            return False
        latest = datetime.fromisoformat(items[0]["published_at"])
        return latest < generated_at - timedelta(seconds=self.stale_after_seconds)

    def _sort_key(self, item: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(item["published_at"])


__all__ = ["NewsAPIArticleClient", "NewsAPINewsProvider", "StdlibNewsAPIClient"]
