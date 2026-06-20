from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.dashboard.read_models import build_fx_rate_snapshot_card
from maestro.fx.models import FXRateSnapshot, FXRefreshResult
from maestro.fx.provider import ExchangeRateAPIProvider
from maestro.fx.service import FXRefreshService
from maestro.fx.store import SystemEventFXRateStore
from maestro.state.store import StateStore


class FixtureExchangeRateClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def pair(
        self,
        *,
        api_key: str,
        source: str,
        target: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "api_key": api_key,
                "source": source,
                "target": target,
                "timeout_seconds": timeout_seconds,
            }
        )
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_fx_config_defaults_to_exchangerate_api():
    config = load_config("configs/paper.yaml")

    assert config.fx.enabled is True
    assert config.fx.provider == "exchangerate_api"
    assert config.fx.api_key_env == "EXCHANGERATE_API_KEY"
    assert config.fx.pairs == ["USD/KRW"]
    assert config.fx.stale_after_seconds == 14400
    assert config.fx.refresh_interval_seconds == 3600


def test_exchange_rate_api_provider_fetches_usd_krw_pair():
    client = FixtureExchangeRateClient(
        {
            "result": "success",
            "base_code": "USD",
            "target_code": "KRW",
            "conversion_rate": 1350.25,
            "time_last_update_unix": 1_777_777_777,
            "time_next_update_unix": 1_777_781_377,
        }
    )
    provider = ExchangeRateAPIProvider(
        api_key="secret-key",
        stale_after_seconds=14400,
        client=client,
    )

    snapshot = provider.fetch(["USD/KRW"])

    assert client.calls == [
        {
            "api_key": "secret-key",
            "source": "USD",
            "target": "KRW",
            "timeout_seconds": 10.0,
        }
    ]
    assert snapshot.source == "exchangerate_api"
    assert snapshot.rates == {"USD/KRW": 1350.25}
    assert snapshot.max_age_seconds == 14400
    assert snapshot.metadata["time_next_update_unix"] == 1_777_781_377


def test_exchange_rate_api_provider_rejects_malformed_payload():
    provider = ExchangeRateAPIProvider(
        api_key="secret-key",
        client=FixtureExchangeRateClient({"result": "success", "conversion_rate": 0}),
    )

    with pytest.raises(ValueError, match="conversion_rate"):
        provider.fetch(["USD/KRW"])


def test_fx_refresh_service_persists_success_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    now_unix = int(utc_now().timestamp())
    provider = ExchangeRateAPIProvider(
        api_key="secret-key",
        stale_after_seconds=14400,
        client=FixtureExchangeRateClient(
            {
                "result": "success",
                "base_code": "USD",
                "target_code": "KRW",
                "conversion_rate": 1350.25,
                "time_last_update_unix": now_unix,
            }
        ),
    )

    result = FXRefreshService(
        provider=provider,
        rate_store=SystemEventFXRateStore(store),
        pairs=["USD/KRW"],
    ).refresh("run_fx")

    event = store.load_latest_system_event("fx_rate_snapshot")
    fx_card = build_fx_rate_snapshot_card(store)
    assert result.status == "ok"
    assert event is not None
    assert event["payload"]["source"] == "exchangerate_api"
    assert event["payload"]["rates"] == {"USD/KRW": 1350.25}
    assert event["payload"]["max_age_seconds"] == 14400
    assert fx_card["status"] == "fresh"
    assert fx_card["rate"] == 1350.25


def test_fx_refresh_service_reuses_fresh_snapshot_without_provider_call(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    rate_store = SystemEventFXRateStore(store)
    now = utc_now()
    rate_store.save_snapshot(
        "run_existing_fx",
        FXRateSnapshot(
            source="exchangerate_api",
            as_of=now - timedelta(minutes=10),
            fetched_at=now - timedelta(minutes=10),
            max_age_seconds=14400,
            rates={"USD/KRW": 1349.5},
        ),
    )
    client = FixtureExchangeRateClient(
        {
            "result": "success",
            "base_code": "USD",
            "target_code": "KRW",
            "conversion_rate": 1350.25,
            "time_last_update_unix": int(now.timestamp()),
        }
    )
    provider = ExchangeRateAPIProvider(api_key="secret-key", client=client)

    result = FXRefreshService(
        provider=provider,
        rate_store=rate_store,
        pairs=["USD/KRW"],
        refresh_interval_seconds=3600,
    ).refresh("run_fx")

    assert result.status == "cached"
    assert result.rates == {"USD/KRW": 1349.5}
    assert client.calls == []


def test_fx_refresh_service_fetches_when_snapshot_is_older_than_interval(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    rate_store = SystemEventFXRateStore(store)
    now = utc_now()
    rate_store.save_snapshot(
        "run_existing_fx",
        FXRateSnapshot(
            source="exchangerate_api",
            as_of=now - timedelta(hours=2),
            fetched_at=now - timedelta(hours=2),
            max_age_seconds=14400,
            rates={"USD/KRW": 1340.0},
        ),
    )
    client = FixtureExchangeRateClient(
        {
            "result": "success",
            "base_code": "USD",
            "target_code": "KRW",
            "conversion_rate": 1350.25,
            "time_last_update_unix": int(now.timestamp()),
        }
    )
    provider = ExchangeRateAPIProvider(api_key="secret-key", client=client)

    result = FXRefreshService(
        provider=provider,
        rate_store=rate_store,
        pairs=["USD/KRW"],
        refresh_interval_seconds=3600,
    ).refresh("run_fx")

    assert result.status == "ok"
    assert result.rates == {"USD/KRW": 1350.25}
    assert len(client.calls) == 1


def test_fx_refresh_service_force_bypasses_fresh_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    rate_store = SystemEventFXRateStore(store)
    now = utc_now()
    rate_store.save_snapshot(
        "run_existing_fx",
        FXRateSnapshot(
            source="exchangerate_api",
            as_of=now - timedelta(minutes=10),
            fetched_at=now - timedelta(minutes=10),
            max_age_seconds=14400,
            rates={"USD/KRW": 1349.5},
        ),
    )
    client = FixtureExchangeRateClient(
        {
            "result": "success",
            "base_code": "USD",
            "target_code": "KRW",
            "conversion_rate": 1350.25,
            "time_last_update_unix": int(now.timestamp()),
        }
    )
    provider = ExchangeRateAPIProvider(api_key="secret-key", client=client)

    result = FXRefreshService(
        provider=provider,
        rate_store=rate_store,
        pairs=["USD/KRW"],
        refresh_interval_seconds=3600,
    ).refresh("run_fx", force=True)

    assert result.status == "ok"
    assert result.rates == {"USD/KRW": 1350.25}
    assert len(client.calls) == 1


def test_fx_refresh_service_persists_failure_without_secret(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    provider = ExchangeRateAPIProvider(
        api_key="super-secret-api-key",
        client=FixtureExchangeRateClient(RuntimeError("upstream down")),
    )

    with pytest.raises(RuntimeError):
        FXRefreshService(
            provider=provider,
            rate_store=SystemEventFXRateStore(store),
            pairs=["USD/KRW"],
        ).refresh("run_fx")

    event = store.load_latest_system_event("fx_rate_snapshot_failed")
    assert event is not None
    assert event["payload"]["provider"] == "exchangerate_api"
    assert event["payload"]["pairs"] == ["USD/KRW"]
    assert event["payload"]["error_type"] == "RuntimeError"
    assert "upstream down" in event["payload"]["error_message"]
    assert "super-secret-api-key" not in str(event["payload"])


def test_fx_refresh_cli_fails_when_api_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGERATE_API_KEY", raising=False)
    monkeypatch.setenv("MAESTRO_ENV_FILE", str(tmp_path / "missing_maestro.env"))
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(app, ["fx-refresh", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "EXCHANGERATE_API_KEY" in result.output
    assert "secret" not in result.output.lower()


def test_fx_refresh_cli_passes_force_to_service(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_ENV_FILE", str(tmp_path / "missing_maestro.env"))
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    calls: list[bool] = []

    def refresh_from_config(self, run_id=None, *, force=False):
        calls.append(force)
        return FXRefreshResult(
            status="ok",
            source="exchangerate_api",
            as_of=utc_now().isoformat(),
            rates={"USD/KRW": 1350.25},
        )

    monkeypatch.setattr(
        "maestro.fx.service.ConfiguredFXRefreshService.refresh_from_config",
        refresh_from_config,
    )

    result = CliRunner().invoke(
        app,
        ["fx-refresh", "--config", str(config_path), "--force"],
    )

    assert result.exit_code == 0
    assert calls == [True]


@pytest.mark.skipif(
    not os.getenv("EXCHANGERATE_API_KEY"),
    reason="EXCHANGERATE_API_KEY is not set",
)
def test_exchange_rate_api_live_usd_krw_smoke():
    provider = ExchangeRateAPIProvider(
        api_key=os.environ["EXCHANGERATE_API_KEY"],
        stale_after_seconds=14400,
    )

    snapshot = provider.fetch(["USD/KRW"])

    assert snapshot.rates["USD/KRW"] > 0
    assert snapshot.source == "exchangerate_api"
    assert snapshot.as_of <= utc_now()
