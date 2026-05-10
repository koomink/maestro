from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.schemas import OHLCVBar, PricePoint, SymbolData
from maestro.sdk import DataBundle, DataRequest


class YahooHistoryClient(Protocol):
    def history(
        self,
        symbol: str,
        *,
        period: str,
        interval: str,
        timeout_seconds: float,
    ) -> Any:
        """Return Yahoo/yfinance-style OHLCV history for one provider symbol."""


class YFinanceClient:
    def history(
        self,
        symbol: str,
        *,
        period: str,
        interval: str,
        timeout_seconds: float,
    ) -> Any:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderUnavailableError("yfinance package is not installed") from exc

        try:
            return yf.Ticker(symbol).history(
                period=period,
                interval=interval,
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {symbol}"
            ) from exc


class YahooDataProvider(BaseDataProvider):
    source = "yahoo"

    def __init__(
        self,
        *,
        client: YahooHistoryClient | None = None,
        timeout_seconds: float = 10.0,
        stale_after_seconds: int | None = None,
        symbol_map: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client or YFinanceClient()
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.symbol_map = dict(symbol_map or {})

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        generated_at = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            if request.symbol.startswith("CASH"):
                data[request.symbol] = SymbolData(
                    symbol=request.symbol,
                    latest_price=PricePoint(
                        symbol=request.symbol,
                        timestamp=generated_at,
                        price=1.0,
                        source="cash_reference",
                    ),
                    bars=[],
                    is_stale=False,
                    warnings=[],
                ).model_dump(mode="json")
                continue
            provider_symbol = self.symbol_map.get(request.symbol, request.symbol)
            rows = self._fetch_rows(request, provider_symbol)
            if not rows:
                raise ValueError(f"No Yahoo data for symbol: {request.symbol}")

            lookback = request.lookback or len(rows)
            selected = rows[-lookback:]
            if not selected:
                raise ValueError(f"No Yahoo data for symbol: {request.symbol}")

            bars = [self._build_bar(request.symbol, row) for row in selected]
            latest = bars[-1]
            warnings = []
            if lookback > len(rows):
                warnings.append(f"Requested lookback {lookback} exceeds available rows {len(rows)}")

            is_stale = self._is_stale(latest.timestamp, generated_at)
            if is_stale:
                warnings.append("Yahoo data is stale")

            data[request.symbol] = SymbolData(
                symbol=request.symbol,
                latest_price=PricePoint(
                    symbol=request.symbol,
                    timestamp=latest.timestamp,
                    price=latest.close,
                    source=self.source,
                ),
                bars=bars,
                is_stale=is_stale,
                warnings=warnings,
            ).model_dump(mode="json")

        return DataBundle(
            requests=requests, data=data, generated_at=generated_at, source=self.source
        )

    def _fetch_rows(self, request: DataRequest, provider_symbol: str) -> list[Mapping[str, Any]]:
        try:
            raw = self.client.history(
                provider_symbol,
                period=self._period_for(request),
                interval=request.timeframe or "1d",
                timeout_seconds=self.timeout_seconds,
            )
        except ProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {request.symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {request.symbol}"
            ) from exc
        return self._normalize_history_rows(raw)

    def _period_for(self, request: DataRequest) -> str:
        if request.lookback is not None:
            return f"{request.lookback}d"
        return "1mo"

    def _normalize_history_rows(self, raw: Any) -> list[Mapping[str, Any]]:
        if raw is None:
            return []
        if hasattr(raw, "empty") and raw.empty:
            return []
        if hasattr(raw, "iterrows"):
            return [
                {"timestamp": timestamp, **self._row_to_mapping(row)}
                for timestamp, row in raw.iterrows()
            ]
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
            return [self._row_to_mapping(row) for row in raw]
        if isinstance(raw, Iterable) and not isinstance(raw, str | bytes | Mapping):
            return [self._row_to_mapping(row) for row in raw]
        raise ValueError("Malformed Yahoo payload: history must be tabular rows")

    def _row_to_mapping(self, row: Any) -> Mapping[str, Any]:
        if isinstance(row, Mapping):
            return row
        if hasattr(row, "to_dict"):
            return row.to_dict()
        raise ValueError("Malformed Yahoo payload: row must be mapping-like")

    def _build_bar(self, symbol: str, row: Mapping[str, Any]) -> OHLCVBar:
        try:
            return OHLCVBar(
                symbol=symbol,
                timestamp=self._parse_timestamp(row.get("timestamp")),
                open=float(self._read_field(row, "open")),
                high=float(self._read_field(row, "high")),
                low=float(self._read_field(row, "low")),
                close=float(self._read_field(row, "close")),
                volume=float(self._read_field(row, "volume")),
                source=self.source,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ValueError(f"Malformed Yahoo payload for {symbol}: {exc}") from exc

    def _read_field(self, row: Mapping[str, Any], name: str) -> Any:
        for key in (name, name.capitalize(), name.upper()):
            if key in row:
                return row[key]
        raise ValueError(f"missing {name}")

    def _parse_timestamp(self, value: Any) -> datetime:
        if value is None:
            raise ValueError("missing timestamp")
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            timestamp = value
        elif isinstance(value, str):
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("invalid timestamp")

        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    def _is_stale(self, timestamp: datetime, generated_at: datetime) -> bool:
        if self.stale_after_seconds is None:
            return False
        return timestamp < generated_at - timedelta(seconds=self.stale_after_seconds)
