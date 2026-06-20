from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Protocol

from maestro.core.clock import utc_now
from maestro.fx.models import FXRateSnapshot


class ExchangeRateAPIClient(Protocol):
    def pair(
        self,
        *,
        api_key: str,
        source: str,
        target: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        raise NotImplementedError


class UrlLibExchangeRateAPIClient:
    base_url = "https://v6.exchangerate-api.com/v6"

    def pair(
        self,
        *,
        api_key: str,
        source: str,
        target: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{api_key}/pair/{source}/{target}"
        request = urllib.request.Request(url, headers={"User-Agent": "maestro-fx/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ValueError(f"ExchangeRate-API request failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", "transport error")
            raise ValueError(f"ExchangeRate-API request failed: {reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("ExchangeRate-API returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("ExchangeRate-API returned non-object JSON")
        return payload


class ExchangeRateAPIProvider:
    source = "exchangerate_api"

    def __init__(
        self,
        *,
        api_key: str,
        stale_after_seconds: int = 14400,
        timeout_seconds: float = 10.0,
        client: ExchangeRateAPIClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.stale_after_seconds = stale_after_seconds
        self.timeout_seconds = timeout_seconds
        self.client = client or UrlLibExchangeRateAPIClient()

    def fetch(self, pairs: list[str]) -> FXRateSnapshot:
        if pairs != ["USD/KRW"]:
            raise ValueError("ExchangeRate-API FX provider supports only USD/KRW in v1")
        fetched_at = utc_now()
        payload = self.client.pair(
            api_key=self.api_key,
            source="USD",
            target="KRW",
            timeout_seconds=self.timeout_seconds,
        )
        return FXRateSnapshot(
            source=self.source,
            as_of=_as_of(payload, fetched_at),
            fetched_at=fetched_at,
            max_age_seconds=self.stale_after_seconds,
            rates={"USD/KRW": _conversion_rate(payload)},
            metadata=_metadata(payload),
        )


def _conversion_rate(payload: dict[str, Any]) -> float:
    if payload.get("result") not in ("success", None):
        error = payload.get("error-type") or payload.get("error_type") or "unknown_error"
        raise ValueError(f"ExchangeRate-API returned error: {error}")
    value = payload.get("conversion_rate")
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ExchangeRate-API response missing numeric conversion_rate") from exc
    if rate <= 0:
        raise ValueError("ExchangeRate-API conversion_rate must be positive")
    return rate


def _as_of(payload: dict[str, Any], fallback: datetime) -> datetime:
    unix_time = payload.get("time_last_update_unix")
    try:
        if unix_time is not None:
            return datetime.fromtimestamp(int(unix_time), UTC)
    except (TypeError, ValueError, OSError):
        pass
    return fallback


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {}
    for key in (
        "base_code",
        "target_code",
        "time_last_update_unix",
        "time_last_update_utc",
        "time_next_update_unix",
        "time_next_update_utc",
    ):
        if key in payload:
            metadata[key] = payload[key]
    return metadata


__all__ = ["ExchangeRateAPIProvider", "UrlLibExchangeRateAPIClient"]
