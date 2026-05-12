import json
import time
from collections.abc import Callable

from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.sdk import DataBundle, DataRequest


class ResilientDataProvider(BaseDataProvider):
    def __init__(
        self,
        provider: BaseDataProvider,
        *,
        retry_max_attempts: int = 1,
        retry_backoff_seconds: float = 0.0,
        cache_ttl_seconds: int | None = None,
        min_request_interval_seconds: float = 0.0,
        monotonic_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.provider = provider
        self.retry_max_attempts = retry_max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self.monotonic_fn = monotonic_fn or time.monotonic
        self.sleep_fn = sleep_fn or time.sleep
        self._cache: dict[str, tuple[float, DataBundle]] = {}
        self._last_request_at: float | None = None

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        cache_key = self._cache_key(requests)
        cached = self._cache.get(cache_key)
        now = self.monotonic_fn()
        if cached is not None and self.cache_ttl_seconds is not None:
            cached_at, bundle = cached
            if now - cached_at <= self.cache_ttl_seconds:
                return bundle.model_copy(deep=True)

        self._throttle(now)
        last_error: ProviderUnavailableError | None = None
        for attempt in range(self.retry_max_attempts):
            try:
                bundle = self.provider.get_data(requests)
            except ProviderUnavailableError as exc:
                last_error = exc
                if attempt + 1 >= self.retry_max_attempts:
                    break
                if self.retry_backoff_seconds > 0:
                    self.sleep_fn(self.retry_backoff_seconds * (attempt + 1))
                continue
            if self.cache_ttl_seconds is not None:
                self._cache[cache_key] = (self.monotonic_fn(), bundle.model_copy(deep=True))
            return bundle

        if last_error is not None:
            raise last_error
        raise ProviderUnavailableError("DataHub provider is unavailable")

    def _throttle(self, now: float) -> None:
        if self.min_request_interval_seconds <= 0:
            self._last_request_at = now
            return
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            wait_seconds = self.min_request_interval_seconds - elapsed
            if wait_seconds > 0:
                self.sleep_fn(wait_seconds)
                now = self.monotonic_fn()
        self._last_request_at = now

    def _cache_key(self, requests: list[DataRequest]) -> str:
        return json.dumps(
            [request.model_dump(mode="json") for request in requests],
            sort_keys=True,
            separators=(",", ":"),
        )


__all__ = ["ResilientDataProvider"]
