import os

import pytest

from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.newsapi_provider import NewsAPINewsProvider
from maestro.sdk import DataRequest


@pytest.mark.skipif(
    os.getenv("MAESTRO_RUN_NEWSAPI_INTEGRATION") != "1",
    reason="set MAESTRO_RUN_NEWSAPI_INTEGRATION=1 to run live NewsAPI checks",
)
def test_newsapi_provider_live_smoke() -> None:
    if not os.getenv("NEWSAPI_API_KEY"):
        pytest.skip("NEWSAPI_API_KEY is required for live NewsAPI checks")

    provider = NewsAPINewsProvider(
        timeout_seconds=10.0,
        page_size=5,
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
        pytest.skip(f"NewsAPI unavailable during live smoke: {exc}")

    payload = bundle.data["FED"]
    assert payload["source"] == "newsapi"
    assert len(payload["items"]) >= 1
    assert payload["latest"]["title"]
    assert payload["latest"]["url"]
