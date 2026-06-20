from __future__ import annotations

from datetime import datetime
from typing import Protocol

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER
from maestro.fx.models import FXRateSnapshot, FXRefreshResult
from maestro.fx.provider import ExchangeRateAPIProvider
from maestro.fx.store import FXRateStore, SystemEventFXRateStore
from maestro.state.store import StateStore


class FXProvider(Protocol):
    source: str

    def fetch(self, pairs: list[str]) -> FXRateSnapshot:
        raise NotImplementedError


class FXRefreshService:
    def __init__(
        self,
        *,
        provider: FXProvider,
        rate_store: FXRateStore,
        pairs: list[str],
        refresh_interval_seconds: int = 3600,
    ) -> None:
        self.provider = provider
        self.rate_store = rate_store
        self.pairs = pairs
        self.refresh_interval_seconds = refresh_interval_seconds

    def refresh(
        self,
        run_id: str | None = None,
        *,
        force: bool = False,
    ) -> FXRefreshResult:
        refresh_run_id = run_id or new_run_id()
        if not force:
            cached = _fresh_cached_result(
                self.rate_store.load_latest_snapshot(),
                source=self.provider.source,
                pairs=self.pairs,
                refresh_interval_seconds=self.refresh_interval_seconds,
            )
            if cached is not None:
                return cached
        try:
            snapshot = self.provider.fetch(self.pairs)
        except Exception as exc:
            self.rate_store.save_failure(
                refresh_run_id,
                provider=self.provider.source,
                pairs=self.pairs,
                error=exc,
            )
            raise
        self.rate_store.save_snapshot(refresh_run_id, snapshot)
        return FXRefreshResult(
            status="ok",
            source=snapshot.source,
            as_of=snapshot.as_of.isoformat(),
            rates=dict(snapshot.rates),
        )


class ConfiguredFXRefreshService:
    def __init__(
        self,
        config: MaestroConfig,
        store: StateStore,
    ) -> None:
        self.config = config
        self.rate_store = SystemEventFXRateStore(store)

    def refresh_from_config(
        self,
        run_id: str | None = None,
        *,
        force: bool = False,
    ) -> FXRefreshResult:
        refresh_run_id = run_id or new_run_id()
        fx_config = self.config.fx
        if not fx_config.enabled:
            return FXRefreshResult(status="skipped")
        if not force:
            cached = _fresh_cached_result(
                self.rate_store.load_latest_snapshot(),
                source=fx_config.provider,
                pairs=fx_config.pairs,
                refresh_interval_seconds=fx_config.refresh_interval_seconds,
            )
            if cached is not None:
                return cached
        api_key = DEFAULT_CREDENTIAL_RESOLVER.get(fx_config.api_key_env)
        if api_key is None:
            error = ValueError(f"Credential is not set: {fx_config.api_key_env}")
            self.rate_store.save_failure(
                refresh_run_id,
                provider=fx_config.provider,
                pairs=fx_config.pairs,
                error=error,
            )
            raise error
        provider = ExchangeRateAPIProvider(
            api_key=api_key,
            stale_after_seconds=fx_config.stale_after_seconds,
            timeout_seconds=fx_config.timeout_seconds,
        )
        return FXRefreshService(
            provider=provider,
            rate_store=self.rate_store,
            pairs=fx_config.pairs,
            refresh_interval_seconds=fx_config.refresh_interval_seconds,
        ).refresh(refresh_run_id, force=force)


def _fresh_cached_result(
    snapshot: FXRateSnapshot | None,
    *,
    source: str,
    pairs: list[str],
    refresh_interval_seconds: int,
) -> FXRefreshResult | None:
    if snapshot is None or snapshot.source != source:
        return None
    if any(pair not in snapshot.rates for pair in pairs):
        return None
    if not _within_interval(
        snapshot.fetched_at or snapshot.as_of,
        refresh_interval_seconds=refresh_interval_seconds,
    ):
        return None
    return FXRefreshResult(
        status="cached",
        source=snapshot.source,
        as_of=snapshot.as_of.isoformat(),
        rates={pair: snapshot.rates[pair] for pair in pairs},
    )


def _within_interval(timestamp: datetime, *, refresh_interval_seconds: int) -> bool:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=utc_now().tzinfo)
    return (utc_now() - timestamp).total_seconds() <= refresh_interval_seconds


__all__ = ["ConfiguredFXRefreshService", "FXRefreshService"]
