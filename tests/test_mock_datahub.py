from maestro.datahub.mock_provider import MockDataHub
from maestro.sdk import DataRequest


def test_mock_datahub_returns_v02_compatible_payload():
    bundle = MockDataHub().get_data(
        [
            DataRequest(
                symbol="MOCK_ETF_A",
                asset_type="domestic_etf",
                data_type="price",
            )
        ]
    )

    payload = bundle.data["MOCK_ETF_A"]
    assert payload["latest_price"]["price"] == 100.0
    assert payload["bars"][0]["close"] == 100.0
    assert payload["is_stale"] is False
    assert payload["warnings"] == []
