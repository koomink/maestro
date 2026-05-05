import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.sdk import DataBundle, DataRequest


class CSVDataProvider(BaseDataProvider):
    required_columns = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}

    def __init__(self, csv_path: str) -> None:
        self.csv_path = Path(csv_path)
        self.rows_by_symbol = self._load_rows()

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        data: dict[str, Any] = {}
        for request in requests:
            if request.symbol == "CASH":
                data["CASH"] = {"price": 1.0, "ohlcv": []}
                continue
            rows = self.rows_by_symbol.get(request.symbol)
            if not rows:
                raise ValueError(f"No CSV data for symbol: {request.symbol}")
            lookback = request.lookback or len(rows)
            selected = rows[-lookback:]
            data[request.symbol] = {
                "price": selected[-1]["close"],
                "ohlcv": selected,
            }
        return DataBundle(requests=requests, data=data, generated_at=utc_now(), source="csv")

    def _load_rows(self) -> dict[str, list[dict[str, Any]]]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV data file not found: {self.csv_path}")

        rows_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = self.required_columns - columns
            if missing:
                raise ValueError(f"CSV data file is missing columns: {sorted(missing)}")
            for row in reader:
                rows_by_symbol[row["symbol"]].append(
                    {
                        "timestamp": row["timestamp"],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                )

        for rows in rows_by_symbol.values():
            rows.sort(key=lambda row: row["timestamp"])
        return dict(rows_by_symbol)
