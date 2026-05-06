from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.sentiment_provider import RuleBasedSentimentProvider
from maestro.sdk import DataRequest


class FakeSentimentAnalyzer:
    def __init__(self, result: dict[str, Any], error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def analyze(
        self,
        texts: list[str],
        *,
        keywords: list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "texts": texts,
                "keywords": keywords,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


def request(symbol: str = "SPY") -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="us_etf", data_type="sentiment")


def test_sentiment_provider_scores_positive_fixture_text():
    provider = RuleBasedSentimentProvider(
        texts=["SPY posts strong gains as confidence improves"],
        symbol_map={"SPY": "SPY"},
        source_name="fixture_news",
    )

    payload = provider.get_data([request()]).data["SPY"]

    assert payload["score"] > 0
    assert payload["label"] == "positive"
    assert payload["source"] == "fixture_news"
    assert payload["provider"] == "sentiment"
    assert payload["related_symbols"] == ["SPY"]
    assert payload["keywords"] == ["SPY"]
    assert payload["text_count"] == 1
    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_sentiment_provider_scores_negative_fixture_text():
    provider = RuleBasedSentimentProvider(
        texts=["SPY faces weak growth and downside risk"],
        symbol_map={"SPY": "SPY"},
    )

    payload = provider.get_data([request()]).data["SPY"]

    assert payload["score"] < 0
    assert payload["label"] == "negative"


def test_sentiment_provider_scores_neutral_fixture_text():
    provider = RuleBasedSentimentProvider(
        texts=["SPY fund reports quarterly holdings update"],
        symbol_map={"SPY": "SPY"},
    )

    payload = provider.get_data([request()]).data["SPY"]

    assert payload["score"] == 0.0
    assert payload["label"] == "neutral"


def test_sentiment_provider_uses_symbol_map_as_keyword_filter():
    analyzer = FakeSentimentAnalyzer({"score": 0.5, "label": "positive"})
    provider = RuleBasedSentimentProvider(
        texts=[
            "Federal Reserve policy looks supportive",
            "Unrelated market update",
        ],
        analyzer=analyzer,
        symbol_map={"FED": "Federal Reserve,Fed"},
        timeout_seconds=3.0,
    )

    payload = provider.get_data([request("FED")]).data["FED"]

    assert payload["keywords"] == ["Federal Reserve", "Fed"]
    assert analyzer.calls == [
        {
            "texts": ["Federal Reserve policy looks supportive"],
            "keywords": ["Federal Reserve", "Fed"],
            "timeout_seconds": 3.0,
        }
    ]


def test_sentiment_provider_rejects_empty_input():
    provider = RuleBasedSentimentProvider(texts=[])

    with pytest.raises(ValueError, match="No sentiment text configured"):
        provider.get_data([request()])


def test_sentiment_provider_rejects_malformed_input():
    provider = RuleBasedSentimentProvider(texts=["SPY valid text", 123])

    with pytest.raises(ValueError, match="Malformed sentiment input"):
        provider.get_data([request()])


def test_sentiment_provider_rejects_malformed_analyzer_result():
    provider = RuleBasedSentimentProvider(
        texts=["SPY valid text"],
        analyzer=FakeSentimentAnalyzer({"score": 2.0, "label": "positive"}),
        symbol_map={"SPY": "SPY"},
    )

    with pytest.raises(ValueError, match="score out of range"):
        provider.get_data([request()])


def test_sentiment_provider_rejects_malformed_label():
    provider = RuleBasedSentimentProvider(
        texts=["SPY valid text"],
        analyzer=FakeSentimentAnalyzer({"score": 0.0, "label": ["neutral"]}),
        symbol_map={"SPY": "SPY"},
    )

    with pytest.raises(ValueError, match="invalid label"):
        provider.get_data([request()])


def test_sentiment_provider_marks_stale_payloads():
    old_timestamp = datetime.now(UTC) - timedelta(days=2)
    provider = RuleBasedSentimentProvider(
        texts=["SPY valid text"],
        analyzer=FakeSentimentAnalyzer(
            {"score": 0.0, "label": "neutral", "timestamp": old_timestamp}
        ),
        symbol_map={"SPY": "SPY"},
        stale_after_seconds=60,
    )

    payload = provider.get_data([request()]).data["SPY"]

    assert payload["is_stale"] is True
    assert "Sentiment data is stale" in payload["warnings"]


def test_sentiment_provider_preserves_fresh_metadata():
    provider = RuleBasedSentimentProvider(
        texts=["SPY valid text"],
        analyzer=FakeSentimentAnalyzer(
            {"score": 0.0, "label": "neutral", "timestamp": datetime.now(UTC)}
        ),
        symbol_map={"SPY": "SPY"},
        stale_after_seconds=3600,
    )

    payload = provider.get_data([request()]).data["SPY"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_sentiment_provider_maps_timeout_to_provider_unavailable():
    provider = RuleBasedSentimentProvider(
        texts=["SPY valid text"],
        analyzer=FakeSentimentAnalyzer({}, error=TimeoutError("slow")),
        symbol_map={"SPY": "SPY"},
    )

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        provider.get_data([request()])
