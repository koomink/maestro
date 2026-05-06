from datetime import UTC, datetime
from typing import Any

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.rss_provider import RSSNewsProvider
from maestro.sdk import DataRequest


class FakeRSSClient:
    def __init__(self, payloads: dict[str, str], error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def fetch(self, url: str, *, timeout_seconds: float) -> str:
        self.calls.append({"url": url, "timeout_seconds": timeout_seconds})
        if self.error is not None:
            raise self.error
        return self.payloads[url]


def request(symbol: str = "MARKET", lookback: int | None = 2) -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="cash", data_type="news", lookback=lookback)


def rss_feed(*items: str) -> str:
    return f"""
    <rss version="2.0">
      <channel>
        <title>Fixture News</title>
        {"".join(items)}
      </channel>
    </rss>
    """


def rss_item(
    title: str,
    link: str,
    pub_date: str,
    description: str = "Fixture summary",
) -> str:
    return f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <pubDate>{pub_date}</pubDate>
      <description>{description}</description>
    </item>
    """


def test_rss_provider_normalizes_news_items_from_fixture_feed():
    feed_url = "https://example.test/rss"
    client = FakeRSSClient(
        {
            feed_url: rss_feed(
                rss_item(
                    "Older market story",
                    "https://example.test/older",
                    "Wed, 01 Jan 2025 00:00:00 GMT",
                ),
                rss_item(
                    "Latest market story",
                    "https://example.test/latest",
                    "Wed, 01 Jan 2026 00:00:00 GMT",
                ),
            )
        }
    )
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=client,
        timeout_seconds=3.0,
        source_map={feed_url: "Fixture"},
    )

    bundle = provider.get_data([request()])

    payload = bundle.data["MARKET"]
    assert bundle.source == "rss"
    assert payload["symbol"] == "MARKET"
    assert payload["latest"]["title"] == "Latest market story"
    assert payload["latest"]["published_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["latest"]["source"] == "Fixture"
    assert len(payload["items"]) == 2
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
    assert client.calls == [{"url": feed_url, "timeout_seconds": 3.0}]


def test_rss_provider_uses_symbol_map_as_keyword_filter():
    feed_url = "https://example.test/rss"
    client = FakeRSSClient(
        {
            feed_url: rss_feed(
                rss_item(
                    "Federal Reserve decision",
                    "https://example.test/fed",
                    "Wed, 01 Jan 2026 00:00:00 GMT",
                ),
                rss_item(
                    "Unrelated story", "https://example.test/other", "Wed, 01 Jan 2026 01:00:00 GMT"
                ),
            )
        }
    )
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=client,
        symbol_map={"FED": "Federal Reserve"},
    )

    payload = provider.get_data([request("FED")]).data["FED"]

    assert [item["title"] for item in payload["items"]] == ["Federal Reserve decision"]


def test_rss_provider_rejects_empty_feed():
    feed_url = "https://example.test/rss"
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient({feed_url: rss_feed()}),
    )

    with pytest.raises(ValueError, match="No RSS news items"):
        provider.get_data([request()])


def test_rss_provider_rejects_blank_feed_url():
    with pytest.raises(ValueError, match="feed URLs must not be blank"):
        RSSNewsProvider(feed_urls=["  "], client=FakeRSSClient({}))


def test_rss_provider_rejects_malformed_feed():
    feed_url = "https://example.test/rss"
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient({feed_url: "<rss><channel><item></channel></rss>"}),
    )

    with pytest.raises(ValueError, match="Malformed RSS feed"):
        provider.get_data([request()])


def test_rss_provider_rejects_malformed_item():
    feed_url = "https://example.test/rss"
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient({feed_url: rss_feed("<item><title>Missing link</title></item>")}),
    )

    with pytest.raises(ValueError, match="item missing title or link"):
        provider.get_data([request()])


def test_rss_provider_marks_stale_payloads():
    feed_url = "https://example.test/rss"
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient(
            {
                feed_url: rss_feed(
                    rss_item(
                        "Old story", "https://example.test/old", "Wed, 01 Jan 2020 00:00:00 GMT"
                    )
                )
            }
        ),
        stale_after_seconds=60,
    )

    payload = provider.get_data([request(lookback=1)]).data["MARKET"]

    assert payload["is_stale"] is True
    assert "RSS news is stale" in payload["warnings"]


def test_rss_provider_preserves_fresh_metadata():
    feed_url = "https://example.test/rss"
    now = datetime.now(UTC)
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient(
            {
                feed_url: rss_feed(
                    rss_item("Fresh story", "https://example.test/fresh", now.isoformat())
                )
            }
        ),
        stale_after_seconds=3600,
    )

    payload = provider.get_data([request(lookback=1)]).data["MARKET"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_rss_provider_maps_timeout_to_provider_unavailable():
    provider = RSSNewsProvider(
        feed_urls=["https://example.test/rss"],
        client=FakeRSSClient({}, error=TimeoutError("slow")),
    )

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        provider.get_data([request()])


def test_rss_provider_maps_client_errors_to_provider_unavailable():
    provider = RSSNewsProvider(
        feed_urls=["https://example.test/rss"],
        client=FakeRSSClient({}, error=RuntimeError("down")),
    )

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        provider.get_data([request()])


def test_rss_provider_normalizes_atom_fixture_feed():
    feed_url = "https://example.test/atom"
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        client=FakeRSSClient(
            {
                feed_url: """
                <feed xmlns="http://www.w3.org/2005/Atom">
                  <title>Atom Fixture</title>
                  <entry>
                    <title>Atom market story</title>
                    <link href="https://example.test/atom-story" />
                    <updated>2026-01-01T00:00:00Z</updated>
                    <summary>Atom summary</summary>
                  </entry>
                </feed>
                """
            }
        ),
    )

    payload = provider.get_data([request(lookback=1)]).data["MARKET"]

    assert payload["latest"]["title"] == "Atom market story"
    assert payload["latest"]["url"] == "https://example.test/atom-story"
    assert payload["latest"]["source"] == "Atom Fixture"
