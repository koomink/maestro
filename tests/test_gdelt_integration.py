import os

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.gdelt_provider import GDELTNewsProvider
from maestro.sdk import DataRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MAESTRO_RUN_GDELT_INTEGRATION") != "1",
        reason="set MAESTRO_RUN_GDELT_INTEGRATION=1 to run live GDELT checks",
    ),
]


def test_gdelt_provider_live_smoke() -> None:
    provider = GDELTNewsProvider(
        timeout_seconds=5.0,
        stale_after_seconds=86400 * 14,
        symbol_map={"FED": "Federal Reserve"},
    )
    try:
        bundle = provider.get_data(
            [
                DataRequest(
                    symbol="FED",
                    asset_type="cash",
                    data_type="news",
                    lookback=1,
                )
            ]
        )
    except ProviderUnavailableError as exc:
        pytest.skip(f"GDELT unavailable during live smoke: {exc}")

    payload = bundle.data["FED"]
    assert payload["items"]
    assert payload["latest"]["title"]
    assert payload["latest"]["url"]
