import os

import pytest

from maestro.config.env import load_project_dotenv
from maestro.datahub.fred_provider import FREDDataProvider
from maestro.sdk import DataRequest

load_project_dotenv()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MAESTRO_RUN_FRED_INTEGRATION") != "1",
        reason="set MAESTRO_RUN_FRED_INTEGRATION=1 to run live FRED checks",
    ),
]


def test_fred_provider_live_smoke() -> None:
    if not os.getenv("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY is required for live FRED checks")

    provider = FREDDataProvider(timeout_seconds=5.0, stale_after_seconds=86400 * 365 * 3)
    bundle = provider.get_data(
        [
            DataRequest(
                symbol="GDP",
                asset_type="cash",
                data_type="macro",
                lookback=2,
            )
        ]
    )

    payload = bundle.data["GDP"]
    assert payload["observations"]
    assert isinstance(payload["latest"]["value"], float)
