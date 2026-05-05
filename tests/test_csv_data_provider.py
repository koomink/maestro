from pathlib import Path

import pytest

from maestro.datahub.csv_provider import CSVDataProvider
from maestro.sdk import DataRequest


def test_csv_data_provider_loads_latest_price_and_bars():
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
    symbol_data = bundle.data["MOCK_ETF_A"]
    assert symbol_data["latest_price"]["price"] == 103.0
    assert len(symbol_data["bars"]) == 2
    assert symbol_data["bars"][0]["timestamp"].startswith("2026-01-02")


def test_csv_data_provider_warns_when_lookback_exceeds_available_rows():
    provider = CSVDataProvider("data/sample_prices.csv")

    bundle = provider.get_data(
        [
            DataRequest(
                symbol="MOCK_ETF_A",
                asset_type="domestic_etf",
                data_type="price",
                lookback=99,
            )
        ]
    )

    assert len(bundle.data["MOCK_ETF_A"]["bars"]) == 3
    assert bundle.data["MOCK_ETF_A"]["warnings"]


def test_csv_data_provider_rejects_unknown_symbol():
    provider = CSVDataProvider("data/sample_prices.csv")

    with pytest.raises(ValueError, match="No CSV data for symbol: MISSING"):
        provider.get_data(
            [
                DataRequest(
                    symbol="MISSING",
                    asset_type="domestic_etf",
                    data_type="price",
                )
            ]
        )


def test_csv_data_provider_rejects_missing_columns(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("timestamp,symbol,open,high,low,close\n")

    with pytest.raises(ValueError, match="missing columns"):
        CSVDataProvider(str(csv_path))


def test_csv_data_provider_rejects_invalid_ohlcv(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2026-01-01T00:00:00Z,MOCK_ETF_A,100,90,95,100,1\n"
    )

    with pytest.raises(ValueError, match="Invalid OHLCV row for MOCK_ETF_A"):
        CSVDataProvider(str(csv_path))


def test_csv_data_provider_rejects_invalid_timestamp(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "timestamp,symbol,open,high,low,close,volume\nnot-a-date,MOCK_ETF_A,100,101,99,100,1\n"
    )

    with pytest.raises(ValueError, match="Invalid timestamp"):
        CSVDataProvider(str(csv_path))


def test_sample_csv_file_exists():
    assert Path("data/sample_prices.csv").exists()
