from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.sdk import DataBundle, DataRequest


class SentimentAnalyzer(Protocol):
    def analyze(
        self,
        texts: list[str],
        *,
        keywords: list[str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Return a sentiment result for fixture/news text snippets."""


class RuleBasedSentimentAnalyzer:
    positive_terms = frozenset(
        {
            "beat",
            "beats",
            "benefit",
            "bullish",
            "confidence",
            "gain",
            "gains",
            "growth",
            "improve",
            "improves",
            "optimistic",
            "positive",
            "rally",
            "record",
            "strong",
            "surge",
            "upside",
        }
    )
    negative_terms = frozenset(
        {
            "bearish",
            "concern",
            "decline",
            "declines",
            "downgrade",
            "fall",
            "falls",
            "loss",
            "losses",
            "miss",
            "misses",
            "negative",
            "risk",
            "risks",
            "selloff",
            "slowdown",
            "weak",
            "worry",
        }
    )

    def analyze(
        self,
        texts: list[str],
        *,
        keywords: list[str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del timeout_seconds
        positive = 0
        negative = 0
        for text in texts:
            tokens = self._tokens(text)
            positive += sum(1 for token in tokens if token in self.positive_terms)
            negative += sum(1 for token in tokens if token in self.negative_terms)

        total = positive + negative
        score = 0.0 if total == 0 else (positive - negative) / total
        return {
            "score": score,
            "label": self._label(score),
            "keywords": keywords,
        }

    def _tokens(self, text: str) -> list[str]:
        normalized = "".join(
            character.lower() if character.isalnum() else " " for character in text
        )
        return normalized.split()

    def _label(self, score: float) -> str:
        if score > 0.2:
            return "positive"
        if score < -0.2:
            return "negative"
        return "neutral"


class RuleBasedSentimentProvider(BaseDataProvider):
    source = "sentiment"

    def __init__(
        self,
        *,
        texts: Sequence[Any],
        analyzer: SentimentAnalyzer | None = None,
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
        source_name: str | None = None,
    ) -> None:
        self.texts = list(texts)
        self.analyzer = analyzer or RuleBasedSentimentAnalyzer()
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})
        self.source_name = source_name or self.source

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        generated_at = utc_now()
        texts = self._validated_texts()
        data: dict[str, Any] = {}
        for request in requests:
            keywords = self._keywords_for(request.symbol)
            matched_texts = self._filter_texts(texts, keywords)
            if not matched_texts:
                raise ValueError(f"No sentiment text for symbol: {request.symbol}")

            result = self._analyze(request.symbol, matched_texts, keywords)
            score = self._score(result, request.symbol)
            label = self._label(result, score, request.symbol)
            timestamp = self._timestamp(result, generated_at, request.symbol)
            is_stale = self._is_stale(timestamp, generated_at)
            warnings = []
            if is_stale:
                warnings.append("Sentiment data is stale")

            data[request.symbol] = {
                "symbol": request.symbol,
                "score": score,
                "label": label,
                "source": self.source_name,
                "provider": self.source,
                "timestamp": timestamp.isoformat(),
                "related_symbols": [request.symbol],
                "keywords": keywords,
                "text_count": len(matched_texts),
                "is_stale": is_stale,
                "warnings": warnings,
            }

        return DataBundle(
            requests=requests, data=data, generated_at=generated_at, source=self.source
        )

    def _validated_texts(self) -> list[str]:
        if not self.texts:
            raise ValueError("No sentiment text configured")
        invalid = [item for item in self.texts if not isinstance(item, str) or not item.strip()]
        if invalid:
            raise ValueError("Malformed sentiment input: texts must be non-empty strings")
        return [item.strip() for item in self.texts]

    def _keywords_for(self, symbol: str) -> list[str]:
        mapped = self.symbol_map.get(symbol, symbol)
        keywords = [item.strip() for item in mapped.split(",") if item.strip()]
        if not keywords:
            raise ValueError(f"No sentiment keywords configured for symbol: {symbol}")
        return keywords

    def _filter_texts(self, texts: list[str], keywords: list[str]) -> list[str]:
        lowered_keywords = [keyword.casefold() for keyword in keywords]
        return [
            text
            for text in texts
            if any(keyword in text.casefold() for keyword in lowered_keywords)
        ]

    def _analyze(self, symbol: str, texts: list[str], keywords: list[str]) -> Mapping[str, Any]:
        try:
            return self.analyzer.analyze(
                texts,
                keywords=keywords,
                timeout_seconds=self.timeout_seconds,
            )
        except ProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Sentiment provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Sentiment provider is unavailable for symbol: {symbol}"
            ) from exc

    def _score(self, result: Mapping[str, Any], symbol: str) -> float:
        try:
            score = float(result["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed sentiment result for {symbol}: invalid score") from exc
        if score < -1.0 or score > 1.0:
            raise ValueError(f"Malformed sentiment result for {symbol}: score out of range")
        return score

    def _label(self, result: Mapping[str, Any], score: float, symbol: str) -> str:
        raw_label = result.get("label")
        if raw_label is None:
            if score > 0.2:
                return "positive"
            if score < -0.2:
                return "negative"
            return "neutral"
        if not isinstance(raw_label, str) or raw_label not in {
            "positive",
            "negative",
            "neutral",
        }:
            raise ValueError(f"Malformed sentiment result for {symbol}: invalid label")
        return raw_label

    def _timestamp(
        self, result: Mapping[str, Any], generated_at: datetime, symbol: str
    ) -> datetime:
        raw_timestamp = result.get("timestamp")
        if raw_timestamp is None:
            return generated_at
        if isinstance(raw_timestamp, datetime):
            timestamp = raw_timestamp
        elif isinstance(raw_timestamp, str):
            try:
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"Malformed sentiment result for {symbol}: invalid timestamp"
                ) from exc
        else:
            raise ValueError(f"Malformed sentiment result for {symbol}: invalid timestamp")

        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    def _is_stale(self, timestamp: datetime, generated_at: datetime) -> bool:
        if self.stale_after_seconds is None:
            return False
        return timestamp < generated_at - timedelta(seconds=self.stale_after_seconds)
