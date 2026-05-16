import os

import pytest

from maestro.config.env import load_project_dotenv
from maestro.datahub.rss_provider import RSSNewsProvider
from maestro.sdk import DataRequest

load_project_dotenv()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MAESTRO_RUN_RSS_INTEGRATION") != "1",
        reason="set MAESTRO_RUN_RSS_INTEGRATION=1 to run live RSS checks",
    ),
]


def test_rss_provider_live_smoke() -> None:
    feed_url = os.getenv("MAESTRO_RSS_INTEGRATION_URL", "https://feeds.bbci.co.uk/news/rss.xml")
    provider = RSSNewsProvider(
        feed_urls=[feed_url],
        timeout_seconds=5.0,
        stale_after_seconds=86400 * 14,
    )
    bundle = provider.get_data(
        [
            DataRequest(
                symbol="GLOBAL_NEWS",
                asset_type="cash",
                data_type="news",
                lookback=1,
            )
        ]
    )

    payload = bundle.data["GLOBAL_NEWS"]
    assert payload["items"]
    assert payload["latest"]["title"]
    assert payload["latest"]["url"]
