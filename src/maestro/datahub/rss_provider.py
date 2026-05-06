import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.sdk import DataBundle, DataRequest


class RSSFeedClient(Protocol):
    def fetch(self, url: str, *, timeout_seconds: float) -> str:
        """Return RSS XML for one feed URL."""


class StdlibRSSClient:
    def fetch(self, url: str, *, timeout_seconds: float) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "maestro-datahub/0.3"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8")
        except TimeoutError as exc:
            raise ProviderUnavailableError(f"RSS provider timed out for feed: {url}") from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"RSS provider is unavailable for feed: {url}") from exc


class RSSNewsProvider(BaseDataProvider):
    source = "rss"

    def __init__(
        self,
        *,
        feed_urls: list[str],
        client: RSSFeedClient | None = None,
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
        source_map: Mapping[str, str] | None = None,
    ) -> None:
        if not feed_urls:
            raise ValueError("RSS provider requires at least one feed URL")
        normalized_feed_urls = [url.strip() for url in feed_urls]
        if any(not url for url in normalized_feed_urls):
            raise ValueError("RSS provider feed URLs must not be blank")
        self.feed_urls = normalized_feed_urls
        self.client = client or StdlibRSSClient()
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})
        self.source_map = dict(source_map or {})

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        generated_at = utc_now()
        feed_items = self._fetch_all_items()
        data: dict[str, Any] = {}
        for request in requests:
            keyword = self.symbol_map.get(request.symbol)
            items = self._filter_items(feed_items, keyword)
            if not items:
                raise ValueError(f"No RSS news items for symbol: {request.symbol}")

            lookback = request.lookback or len(items)
            selected = items[:lookback]
            warnings = []
            if lookback > len(items):
                warnings.append(
                    f"Requested lookback {lookback} exceeds available items {len(items)}"
                )

            is_stale = self._is_stale(selected, generated_at)
            if is_stale:
                warnings.append("RSS news is stale")

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

    def _fetch_all_items(self) -> list[dict[str, Any]]:
        items = []
        for url in self.feed_urls:
            xml_text = self._fetch_feed(url)
            items.extend(self._parse_feed(xml_text, url))
        if not items:
            raise ValueError("No RSS news items found")
        return sorted(items, key=self._sort_key, reverse=True)

    def _fetch_feed(self, url: str) -> str:
        try:
            return self.client.fetch(url, timeout_seconds=self.timeout_seconds)
        except ProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(f"RSS provider timed out for feed: {url}") from exc
        except Exception as exc:
            raise ProviderUnavailableError(f"RSS provider is unavailable for feed: {url}") from exc

    def _parse_feed(self, xml_text: str, url: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"Malformed RSS feed for {url}: invalid XML") from exc

        raw_items = root.findall("./channel/item")
        if not raw_items:
            raw_items = root.findall("{http://www.w3.org/2005/Atom}entry")

        source_name = self.source_map.get(url, self._feed_title(root) or url)
        return [self._parse_item(item, url, source_name) for item in raw_items]

    def _parse_item(self, item: ET.Element, feed_url: str, source_name: str) -> dict[str, Any]:
        title = self._text(item, "title") or self._text(item, "{http://www.w3.org/2005/Atom}title")
        link = self._text(item, "link")
        if link is None:
            link = self._atom_link(item)
        if not title or not link:
            raise ValueError(f"Malformed RSS feed for {feed_url}: item missing title or link")

        published_at = self._parse_datetime(
            self._text(item, "pubDate")
            or self._text(item, "published")
            or self._text(item, "updated")
            or self._text(item, "{http://www.w3.org/2005/Atom}published")
            or self._text(item, "{http://www.w3.org/2005/Atom}updated")
            or self._text(item, "{http://purl.org/dc/elements/1.1/}date"),
            feed_url,
        )
        summary = (
            self._text(item, "description")
            or self._text(item, "summary")
            or self._text(item, "{http://www.w3.org/2005/Atom}summary")
        )
        return {
            "title": title,
            "url": link,
            "published_at": published_at.isoformat() if published_at is not None else None,
            "summary": summary,
            "feed_url": feed_url,
            "source": source_name,
        }

    def _feed_title(self, root: ET.Element) -> str | None:
        channel = root.find("channel")
        if channel is not None:
            return self._text(channel, "title")
        return self._text(root, "title") or self._text(root, "{http://www.w3.org/2005/Atom}title")

    def _text(self, item: ET.Element, name: str) -> str | None:
        child = item.find(name)
        if child is None or child.text is None:
            return None
        text = child.text.strip()
        return text or None

    def _atom_link(self, item: ET.Element) -> str | None:
        link = item.find("{http://www.w3.org/2005/Atom}link")
        if link is None:
            return None
        href = link.attrib.get("href")
        return href.strip() if href else None

    def _parse_datetime(self, value: str | None, feed_url: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Malformed RSS feed for {feed_url}: invalid item date") from exc

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _filter_items(
        self, items: list[dict[str, Any]], keyword: str | None
    ) -> list[dict[str, Any]]:
        if keyword is None:
            return items
        normalized_keyword = keyword.casefold()
        return [
            item
            for item in items
            if normalized_keyword in item["title"].casefold()
            or (
                isinstance(item.get("summary"), str)
                and normalized_keyword in item["summary"].casefold()
            )
        ]

    def _is_stale(self, items: list[dict[str, Any]], generated_at: datetime) -> bool:
        if self.stale_after_seconds is None:
            return False
        dated_items = [item for item in items if item["published_at"] is not None]
        if not dated_items:
            return False
        latest = datetime.fromisoformat(dated_items[0]["published_at"])
        return latest < generated_at - timedelta(seconds=self.stale_after_seconds)

    def _sort_key(self, item: dict[str, Any]) -> datetime:
        if item["published_at"] is None:
            return datetime.min.replace(tzinfo=UTC)
        return datetime.fromisoformat(item["published_at"])
