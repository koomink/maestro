import os

import pytest

from maestro.datahub.yahoo_provider import YahooDataProvider
from maestro.sdk import DataRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MAESTRO_RUN_YFINANCE_INTEGRATION") != "1",
        reason="set MAESTRO_RUN_YFINANCE_INTEGRATION=1 to run live yfinance checks",
    ),
]


def test_yahoo_provider_live_yfinance_smoke() -> None:
    pytest.importorskip("yfinance")

    provider = YahooDataProvider(timeout_seconds=5.0, stale_after_seconds=86400 * 14)
    bundle = provider.get_data(
        [
            DataRequest(
                symbol="SPY",
                asset_type="us_etf",
                data_type="price",
                timeframe="1d",
                lookback=5,
            )
        ]
    )

    payload = bundle.data["SPY"]
    assert payload["latest_price"]["price"] > 0
    assert payload["bars"]
