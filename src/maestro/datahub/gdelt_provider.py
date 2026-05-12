import json
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


class GDELTArticleClient(Protocol):
    def articles(
        self,
        query: str,
        *,
        base_url: str,
        timespan: str,
        max_records: int,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Return a GDELT DOC 2.0 article-list payload."""


class StdlibGDELTClient:
    def articles(
        self,
        query: str,
        *,
        base_url: str,
        timespan: str,
        max_records: int,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        params = urllib.parse.urlencode(
            {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "timespan": timespan,
                "maxrecords": max_records,
            }
        )
        url = f"{base_url}?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "maestro-datahub/0.3"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise ProviderUnavailableError(f"GDELT provider timed out for query: {query}") from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(
                f"GDELT provider is unavailable for query: {query}"
            ) from exc

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed GDELT payload for {query}: invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Malformed GDELT payload for {query}: expected object")
        return decoded


class GDELTNewsProvider(BaseDataProvider):
    source = "gdelt"

    def __init__(
        self,
        *,
        client: GDELTArticleClient | None = None,
        base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc",
        timespan: str = "24h",
        max_records: int = 25,
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
    ) -> None:
        normalized_base_url = base_url.strip()
        normalized_timespan = timespan.strip()
        if not normalized_base_url:
            raise ValueError("GDELT provider base URL must not be blank")
        if not normalized_timespan:
            raise ValueError("GDELT provider timespan must not be blank")
        if max_records < 1 or max_records > 250:
            raise ValueError("GDELT provider max_records must be between 1 and 250")
        self.client = client or StdlibGDELTClient()
        self.base_url = normalized_base_url
        self.timespan = normalized_timespan
        self.max_records = max_records
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        generated_at = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            query = self.symbol_map.get(request.symbol, request.symbol)
            raw = self._fetch_articles(request.symbol, query)
            items = self._normalize_articles(request.symbol, raw)
            if not items:
                raise ValueError(f"No GDELT news items for symbol: {request.symbol}")

            lookback = request.lookback or len(items)
            selected = items[:lookback]
            warnings = []
            if lookback > len(items):
                warnings.append(
                    f"Requested lookback {lookback} exceeds available items {len(items)}"
                )

            is_stale = self._is_stale(selected, generated_at)
            if is_stale:
                warnings.append("GDELT news is stale")

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

    def _fetch_articles(self, symbol: str, query: str) -> Mapping[str, Any]:
        try:
            return self.client.articles(
                query,
                base_url=self.base_url,
                timespan=self.timespan,
                max_records=self.max_records,
                timeout_seconds=self.timeout_seconds,
            )
        except ProviderUnavailableError:
            raise
        except ValueError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"GDELT provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"GDELT provider is unavailable for symbol: {symbol}"
            ) from exc

    def _normalize_articles(self, symbol: str, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_articles = raw.get("articles")
        if not isinstance(raw_articles, Sequence) or isinstance(raw_articles, str | bytes):
            raise ValueError(f"Malformed GDELT payload for {symbol}: articles must be a list")

        items = []
        for article in raw_articles:
            if not isinstance(article, Mapping):
                raise ValueError(f"Malformed GDELT payload for {symbol}: article must be object")
            title = self._required_text(article.get("title"), symbol, "title")
            url = self._required_text(article.get("url"), symbol, "url")
            published_at = self._parse_datetime(article.get("seendate"), symbol)
            domain = self._required_text(article.get("domain"), symbol, "domain")
            items.append(
                {
                    "title": title,
                    "url": url,
                    "published_at": published_at.isoformat(),
                    "summary": None,
                    "source": domain,
                    "domain": domain,
                    "language": self._optional_text(article.get("language")),
                    "source_country": self._optional_text(article.get("sourcecountry")),
                    "social_image": self._optional_text(article.get("socialimage")),
                }
            )
        return sorted(items, key=self._sort_key, reverse=True)

    def _required_text(self, value: Any, symbol: str, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Malformed GDELT payload for {symbol}: invalid article {field}")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Malformed GDELT payload for {symbol}: invalid article {field}")
        return normalized

    def _optional_text(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _parse_datetime(self, value: Any, symbol: str) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"Malformed GDELT payload for {symbol}: invalid article date")
        normalized = value.strip()
        try:
            return datetime.strptime(normalized, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"Malformed GDELT payload for {symbol}: invalid article date"
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


__all__ = ["GDELTArticleClient", "GDELTNewsProvider", "StdlibGDELTClient"]
