from maestro.datahub.csv_provider import CSVDataProvider
from maestro.sdk import DataRequest


def test_csv_data_provider_loads_latest_price():
    provider = CSVDataProvider("data/sample_prices.csv")

    bundle = provider.get_data(
        [
            DataRequest(
                symbol="MOCK_ETF_A",
                asset_type="domestic_etf",
                data_type="price",
                lookback=2,
            )
        ]
    )

    assert bundle.source == "csv"
    assert bundle.data["MOCK_ETF_A"]["price"] == 103.0
    assert len(bundle.data["MOCK_ETF_A"]["ohlcv"]) == 2
