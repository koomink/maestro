import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.schemas import OHLCVBar, PricePoint, SymbolData
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
                generated_at = utc_now()
                data["CASH"] = SymbolData(
                    symbol="CASH",
                    latest_price=PricePoint(
                        symbol="CASH",
                        timestamp=generated_at,
                        price=1.0,
                        source="csv",
                    ),
                    bars=[],
                ).model_dump(mode="json")
                continue
            rows = self.rows_by_symbol.get(request.symbol)
            if not rows:
                raise ValueError(f"No CSV data for symbol: {request.symbol}")
            lookback = request.lookback or len(rows)
            selected = rows[-lookback:]
            warnings = []
            if lookback > len(rows):
                warnings.append(f"Requested lookback {lookback} exceeds available rows {len(rows)}")
            latest = selected[-1]
            symbol_data = SymbolData(
                symbol=request.symbol,
                latest_price=PricePoint(
                    symbol=request.symbol,
                    timestamp=latest.timestamp,
                    price=latest.close,
                    source="csv",
                ),
                bars=selected,
                warnings=warnings,
            )
            data[request.symbol] = symbol_data.model_dump(mode="json")
        return DataBundle(requests=requests, data=data, generated_at=utc_now(), source="csv")

    def _load_rows(self) -> dict[str, list[OHLCVBar]]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV data file not found: {self.csv_path}")

        rows_by_symbol: dict[str, list[OHLCVBar]] = defaultdict(list)
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = self.required_columns - columns
            if missing:
                raise ValueError(f"CSV data file is missing columns: {sorted(missing)}")
            for row in reader:
                symbol = row["symbol"]
                try:
                    bar = OHLCVBar(
                        symbol=symbol,
                        timestamp=self._parse_timestamp(row["timestamp"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        source="csv",
                    )
                except ValueError as exc:
                    raise ValueError(f"Invalid OHLCV row for {symbol}: {exc}") from exc
                rows_by_symbol[symbol].append(bar)

        for rows in rows_by_symbol.values():
            rows.sort(key=lambda row: row.timestamp)
        return dict(rows_by_symbol)

    def _parse_timestamp(self, value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp: {value}") from exc
