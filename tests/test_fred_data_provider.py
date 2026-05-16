from typing import Any

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.fred_provider import FREDDataProvider
from maestro.sdk import DataRequest


class FakeFREDClient:
    def __init__(self, payloads: dict[str, dict[str, Any]], error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def observations(
        self,
        series_id: str,
        *,
        api_key: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "series_id": series_id,
                "api_key": api_key,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.payloads.get(series_id, {"observations": []})


def request(symbol: str = "GDP", lookback: int | None = 2) -> DataRequest:
    return DataRequest(symbol=symbol, asset_type="cash", data_type="macro", lookback=lookback)


def fred_payload() -> dict[str, Any]:
    return {
        "observations": [
            {"date": "2025-01-01", "value": "100.1"},
            {"date": "2025-04-01", "value": "101.2"},
            {"date": "2025-07-01", "value": "102.3"},
        ]
    }


def test_fred_provider_normalizes_macro_observations(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    client = FakeFREDClient({"GDP": fred_payload()})
    provider = FREDDataProvider(
        client=client,
        api_key_env="FRED_TEST_API_KEY",
        timeout_seconds=3.0,
    )

    bundle = provider.get_data([request()])

    payload = bundle.data["GDP"]
    assert bundle.source == "fred"
    assert payload["series_id"] == "GDP"
    assert payload["provider_series_id"] == "GDP"
    assert payload["latest"]["value"] == 102.3
    assert len(payload["observations"]) == 2
    assert payload["observations"][0]["date"] == "2025-04-01"
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
    assert client.calls == [
        {
            "series_id": "GDP",
            "api_key": "secret-value",
            "timeout_seconds": 3.0,
        }
    ]


def test_fred_provider_uses_symbol_map(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    client = FakeFREDClient({"GDPC1": fred_payload()})
    provider = FREDDataProvider(
        client=client,
        api_key_env="FRED_TEST_API_KEY",
        symbol_map={"REAL_GDP": "GDPC1"},
    )

    payload = provider.get_data([request("REAL_GDP")]).data["REAL_GDP"]

    assert payload["provider_series_id"] == "GDPC1"
    assert client.calls[0]["series_id"] == "GDPC1"


def test_fred_provider_rejects_missing_series(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    provider = FREDDataProvider(
        client=FakeFREDClient({"MISSING": {"observations": []}}),
        api_key_env="FRED_TEST_API_KEY",
    )

    with pytest.raises(ValueError, match="No FRED data for series: MISSING"):
        provider.get_data([request("MISSING")])


def test_fred_provider_rejects_malformed_payload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    provider = FREDDataProvider(
        client=FakeFREDClient({"GDP": {"observations": [{"date": "not-a-date", "value": "1"}]}}),
        api_key_env="FRED_TEST_API_KEY",
    )

    with pytest.raises(ValueError, match="Malformed FRED payload"):
        provider.get_data([request()])


def test_fred_provider_skips_missing_observation_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    client = FakeFREDClient(
        {
            "GDP": {
                "observations": [
                    {"date": "2025-01-01", "value": "."},
                    {"date": "2025-04-01", "value": "101.2"},
                ]
            }
        }
    )
    provider = FREDDataProvider(client=client, api_key_env="FRED_TEST_API_KEY")

    payload = provider.get_data([request(lookback=2)]).data["GDP"]

    assert payload["observations"] == [
        {"date": "2025-04-01", "value": 101.2, "source": "fred"}
    ]


def test_fred_provider_marks_stale_payloads(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    client = FakeFREDClient({"GDP": {"observations": [{"date": "2020-01-01", "value": "1.0"}]}})
    provider = FREDDataProvider(
        client=client,
        api_key_env="FRED_TEST_API_KEY",
        stale_after_seconds=60,
    )

    payload = provider.get_data([request(lookback=1)]).data["GDP"]

    assert payload["is_stale"] is True
    assert "FRED data is stale" in payload["warnings"]


def test_fred_provider_preserves_fresh_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    client = FakeFREDClient({"GDP": fred_payload()})
    provider = FREDDataProvider(
        client=client,
        api_key_env="FRED_TEST_API_KEY",
        stale_after_seconds=86400 * 365 * 10,
    )

    payload = provider.get_data([request(lookback=1)]).data["GDP"]

    assert payload["is_stale"] is False
    assert payload["warnings"] == []


def test_fred_provider_requires_api_key_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FRED_TEST_API_KEY", raising=False)
    provider = FREDDataProvider(
        client=FakeFREDClient({"GDP": fred_payload()}), api_key_env="FRED_TEST_API_KEY"
    )

    with pytest.raises(ProviderUnavailableError, match="FRED_TEST_API_KEY"):
        provider.get_data([request()])


def test_fred_provider_maps_timeout_to_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_TEST_API_KEY", "secret-value")
    provider = FREDDataProvider(
        client=FakeFREDClient({}, error=TimeoutError("slow")),
        api_key_env="FRED_TEST_API_KEY",
    )

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        provider.get_data([request()])
