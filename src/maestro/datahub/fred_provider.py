import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from maestro.core.clock import utc_now
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER, CredentialResolver
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.sdk import DataBundle, DataRequest


class FREDSeriesClient(Protocol):
    def observations(
        self,
        series_id: str,
        *,
        api_key: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Return a FRED series observation payload."""


class StdlibFREDClient:
    base_url = "https://api.stlouisfed.org/fred/series/observations"

    def observations(
        self,
        series_id: str,
        *,
        api_key: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(
            {
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "asc",
            }
        )
        url = f"{self.base_url}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "maestro-datahub/0.3"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"FRED provider timed out for series: {series_id}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(
                f"FRED provider is unavailable for series: {series_id}"
            ) from exc

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed FRED payload for {series_id}: invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Malformed FRED payload for {series_id}: expected object")
        return decoded


class FREDDataProvider(BaseDataProvider):
    source = "fred"

    def __init__(
        self,
        *,
        client: FREDSeriesClient | None = None,
        api_key_env: str | None = "FRED_API_KEY",
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        self.client = client or StdlibFREDClient()
        self.api_key_env = api_key_env or "FRED_API_KEY"
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})
        self.credential_resolver = credential_resolver or DEFAULT_CREDENTIAL_RESOLVER

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        api_key = self._api_key()

        generated_at = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            series_id = self.symbol_map.get(request.symbol, request.symbol)
            raw = self._fetch_observations(request.symbol, series_id, api_key)
            observations = self._normalize_observations(request.symbol, raw)
            if not observations:
                raise ValueError(f"No FRED data for series: {request.symbol}")

            lookback = request.lookback or len(observations)
            selected = observations[-lookback:]
            warnings = []
            if lookback > len(observations):
                warnings.append(
                    f"Requested lookback {lookback} exceeds available observations "
                    f"{len(observations)}"
                )

            latest = selected[-1]
            is_stale = self._is_stale(latest["date"], generated_at)
            if is_stale:
                warnings.append("FRED data is stale")

            data[request.symbol] = {
                "series_id": request.symbol,
                "provider_series_id": series_id,
                "latest": latest,
                "observations": selected,
                "is_stale": is_stale,
                "warnings": warnings,
                "source": self.source,
            }

        return DataBundle(
            requests=requests, data=data, generated_at=generated_at, source=self.source
        )

    def _api_key(self) -> str:
        api_key = self.credential_resolver.get(self.api_key_env)
        if not api_key:
            raise ProviderUnavailableError(
                f"FRED API key environment variable is not set: {self.api_key_env}"
            )
        return api_key

    def _fetch_observations(self, symbol: str, series_id: str, api_key: str) -> Mapping[str, Any]:
        try:
            return self.client.observations(
                series_id,
                api_key=api_key,
                timeout_seconds=self.timeout_seconds,
            )
        except ProviderUnavailableError:
            raise
        except ValueError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(f"FRED provider timed out for series: {symbol}") from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"FRED provider is unavailable for series: {symbol}"
            ) from exc

    def _normalize_observations(self, symbol: str, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        if "error_code" in raw:
            raise ValueError(f"No FRED data for series: {symbol}")

        raw_observations = raw.get("observations")
        if not isinstance(raw_observations, Sequence) or isinstance(raw_observations, str | bytes):
            raise ValueError(f"Malformed FRED payload for {symbol}: observations must be a list")

        observations = []
        for item in raw_observations:
            if not isinstance(item, Mapping):
                raise ValueError(f"Malformed FRED payload for {symbol}: observation must be object")
            observation_date = self._parse_date(item.get("date"), symbol)
            if item.get("value") in {None, "."}:
                continue
            value = self._parse_value(item.get("value"), symbol)
            observations.append(
                {
                    "date": observation_date.isoformat(),
                    "value": value,
                    "source": self.source,
                }
            )
        return observations

    def _parse_date(self, value: Any, symbol: str) -> date:
        if not isinstance(value, str):
            raise ValueError(f"Malformed FRED payload for {symbol}: invalid date")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Malformed FRED payload for {symbol}: invalid date") from exc

    def _parse_value(self, value: Any, symbol: str) -> float:
        if value in {None, "."}:
            raise ValueError(f"Malformed FRED payload for {symbol}: invalid value")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Malformed FRED payload for {symbol}: invalid value") from exc

    def _is_stale(self, date_value: str, generated_at: datetime) -> bool:
        if self.stale_after_seconds is None:
            return False
        observation_date = date.fromisoformat(date_value)
        observation_time = datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC)
        return observation_time < generated_at - timedelta(seconds=self.stale_after_seconds)
