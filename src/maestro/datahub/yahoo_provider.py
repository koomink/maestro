from calendar import monthrange
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
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

    def info(self, symbol: str, *, timeout_seconds: float) -> Mapping[str, Any]:
        """Return Yahoo/yfinance-style fundamental info for one provider symbol."""

    def dividends(self, symbol: str, *, timeout_seconds: float) -> Any:
        """Return Yahoo/yfinance-style dividend events for one provider symbol."""

    def financial_statement(
        self,
        symbol: str,
        *,
        statement_type: str,
        timeout_seconds: float,
    ) -> Any:
        """Return one Yahoo/yfinance-style financial statement table."""


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

    def info(self, symbol: str, *, timeout_seconds: float) -> Mapping[str, Any]:
        del timeout_seconds
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderUnavailableError("yfinance package is not installed") from exc

        try:
            info = yf.Ticker(symbol).info
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {symbol}"
            ) from exc
        if not isinstance(info, Mapping):
            raise ValueError(f"Malformed Yahoo info payload for {symbol}: expected mapping")
        return info

    def dividends(self, symbol: str, *, timeout_seconds: float) -> Any:
        del timeout_seconds
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderUnavailableError("yfinance package is not installed") from exc

        try:
            return yf.Ticker(symbol).dividends
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {symbol}"
            ) from exc

    def financial_statement(
        self,
        symbol: str,
        *,
        statement_type: str,
        timeout_seconds: float,
    ) -> Any:
        del timeout_seconds
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderUnavailableError("yfinance package is not installed") from exc

        try:
            ticker = yf.Ticker(symbol)
            if statement_type == "balance_sheet":
                return ticker.balance_sheet
            if statement_type == "income_statement":
                return ticker.financials
            if statement_type == "cashflow":
                return ticker.cashflow
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {symbol}"
            ) from exc
        raise ValueError(f"Unsupported Yahoo financial statement type: {statement_type}")


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
            if request.data_type == "fundamental":
                provider_symbol = self.symbol_map.get(request.symbol, request.symbol)
                data[request.symbol] = self._fundamental_payload(
                    request,
                    provider_symbol,
                    generated_at,
                )
                continue
            if request.data_type == "financial_statements":
                provider_symbol = self.symbol_map.get(request.symbol, request.symbol)
                data[request.symbol] = self._financial_statement_payload(
                    request,
                    provider_symbol,
                    generated_at,
                )
                continue
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

            bars, warnings = self._build_bars(request.symbol, selected, generated_at)
            if not bars:
                raise ValueError(f"No Yahoo data for symbol: {request.symbol}")
            latest = bars[-1]
            if lookback > len(rows):
                warnings.append(f"Requested lookback {lookback} exceeds available rows {len(rows)}")

            is_stale = self._is_stale(
                latest.timestamp,
                generated_at,
                timeframe=request.timeframe,
                data_type=request.data_type,
            )
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

    def _fundamental_payload(
        self,
        request: DataRequest,
        provider_symbol: str,
        generated_at: datetime,
    ) -> dict[str, Any]:
        metric_keys = [
            "trailing_pe",
            "forward_pe",
            "price_to_book",
            "market_cap",
            "dividend_yield",
            "beta",
            "enterprise_value",
            "profit_margins",
            "return_on_equity",
            "revenue_growth",
        ]
        selected_fields = request.fields or metric_keys
        historical_dividend_yield = None
        if request.as_of is not None and "dividend_yield" in selected_fields:
            historical_dividend_yield = self._historical_dividend_yield(
                request,
                provider_symbol,
            )

        raw: Mapping[str, Any] = {}
        needs_info = historical_dividend_yield is None or any(
            field != "dividend_yield" for field in selected_fields
        )
        if needs_info:
            raw = self._info_payload(request, provider_symbol)

        metrics = {
            "trailing_pe": self._clean_value(raw.get("trailingPE")),
            "forward_pe": self._clean_value(raw.get("forwardPE")),
            "price_to_book": self._clean_value(raw.get("priceToBook")),
            "market_cap": self._clean_value(raw.get("marketCap")),
            "dividend_yield": (
                historical_dividend_yield
                if historical_dividend_yield is not None
                else self._clean_value(raw.get("dividendYield"))
            ),
            "beta": self._clean_value(raw.get("beta")),
            "enterprise_value": self._clean_value(raw.get("enterpriseValue")),
            "profit_margins": self._clean_value(raw.get("profitMargins")),
            "return_on_equity": self._clean_value(raw.get("returnOnEquity")),
            "revenue_growth": self._clean_value(raw.get("revenueGrowth")),
        }
        filtered_metrics = {field: metrics[field] for field in selected_fields if field in metrics}
        raw_info = (
            {key: self._clean_value(value) for key, value in raw.items() if key in request.fields}
            if request.fields
            else {}
        )
        fundamental = {"metrics": filtered_metrics, "raw_info": raw_info}

        return {
            "symbol": request.symbol,
            "provider_symbol": provider_symbol,
            "data_type": "fundamental",
            "fundamental": fundamental,
            "metrics": filtered_metrics,
            "raw_info": raw_info,
            "timestamp": generated_at.isoformat(),
            "source": self.source,
            "is_stale": False,
            "warnings": [],
        }

    def _info_payload(self, request: DataRequest, provider_symbol: str) -> Mapping[str, Any]:
        try:
            raw = self.client.info(provider_symbol, timeout_seconds=self.timeout_seconds)
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
        if not raw:
            raise ValueError(f"No Yahoo fundamental data for symbol: {request.symbol}")
        return raw

    def _historical_dividend_yield(
        self,
        request: DataRequest,
        provider_symbol: str,
    ) -> float:
        if request.as_of is None:
            raise ValueError("as_of is required for historical dividend_yield")
        cutoff = request.as_of.date()
        start = self._shift_months(cutoff, 12)
        events = self._dividend_events(request.symbol, provider_symbol)
        trailing_dividend = sum(
            amount for event_date, amount in events if start <= event_date < cutoff
        )
        if trailing_dividend <= 0:
            raise ValueError(
                f"No Yahoo dividend history for symbol: {request.symbol} "
                f"between {start.isoformat()} and {cutoff.isoformat()}"
            )
        price = self._close_before(request, provider_symbol, cutoff)
        return trailing_dividend / price

    def _dividend_events(
        self,
        symbol: str,
        provider_symbol: str,
    ) -> list[tuple[date, float]]:
        try:
            raw = self.client.dividends(
                provider_symbol,
                timeout_seconds=self.timeout_seconds,
            )
        except ProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider timed out for symbol: {symbol}"
            ) from exc
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Yahoo provider is unavailable for symbol: {symbol}"
            ) from exc
        events = self._normalize_dividend_events(raw, symbol)
        if not events:
            raise ValueError(f"No Yahoo dividend history for symbol: {symbol}")
        return events

    def _normalize_dividend_events(self, raw: Any, symbol: str) -> list[tuple[date, float]]:
        if raw is None:
            return []
        if hasattr(raw, "empty") and raw.empty:
            return []
        if isinstance(raw, Mapping):
            return [
                self._dividend_event(date_value, amount, symbol)
                for date_value, amount in raw.items()
            ]
        if hasattr(raw, "items"):
            return [
                self._dividend_event(date_value, amount, symbol)
                for date_value, amount in raw.items()
            ]
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
            events = []
            for item in raw:
                if isinstance(item, Mapping):
                    date_value = item.get("timestamp") or item.get("date") or item.get("Date")
                    amount = self._read_dividend_amount(item)
                    events.append(self._dividend_event(date_value, amount, symbol))
                    continue
                if (
                    isinstance(item, Sequence)
                    and not isinstance(item, str | bytes)
                    and len(item) == 2
                ):
                    events.append(self._dividend_event(item[0], item[1], symbol))
                    continue
                raise ValueError(f"Malformed Yahoo dividend history for {symbol}")
            return events
        if isinstance(raw, Iterable) and not isinstance(raw, str | bytes | Mapping):
            return self._normalize_dividend_events(list(raw), symbol)
        raise ValueError(f"Malformed Yahoo dividend history for {symbol}")

    def _read_dividend_amount(self, row: Mapping[str, Any]) -> Any:
        for key in ("dividend", "Dividends", "value", "amount"):
            if key in row:
                return row[key]
        raise ValueError("Malformed Yahoo dividend history: missing dividend amount")

    def _dividend_event(self, date_value: Any, amount: Any, symbol: str) -> tuple[date, float]:
        try:
            event_date = self._parse_timestamp(date_value).date()
            dividend = float(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Malformed Yahoo dividend history for {symbol}") from exc
        if dividend != dividend:
            raise ValueError(f"Malformed Yahoo dividend history for {symbol}")
        return event_date, dividend

    def _close_before(
        self,
        request: DataRequest,
        provider_symbol: str,
        cutoff: date,
    ) -> float:
        try:
            raw = self.client.history(
                provider_symbol,
                period="max",
                interval="1d",
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
        rows = self._normalize_history_rows(raw)
        closes = []
        for row in rows:
            try:
                timestamp = self._parse_timestamp(row.get("timestamp"))
                close = float(self._read_field(row, "close"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Malformed Yahoo payload for {request.symbol}: {exc}") from exc
            if timestamp.date() < cutoff:
                closes.append((timestamp, close))
        if not closes:
            raise ValueError(
                f"No Yahoo price history before {cutoff.isoformat()} for symbol: {request.symbol}"
            )
        return max(closes, key=lambda item: item[0])[1]

    def _shift_months(self, value: date, months: int) -> date:
        month_index = value.month - months - 1
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, monthrange(year, month)[1])
        return date(year, month, day)

    def _financial_statement_payload(
        self,
        request: DataRequest,
        provider_symbol: str,
        generated_at: datetime,
    ) -> dict[str, Any]:
        statement_type = self._statement_type_for(request)
        try:
            raw = self.client.financial_statement(
                provider_symbol,
                statement_type=statement_type,
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
        statement = self._normalize_statement(raw, request.symbol)
        if not statement:
            raise ValueError(f"No Yahoo financial statement data for symbol: {request.symbol}")
        statement_payload = {
            "statement_type": statement_type,
            "frequency": request.frequency or "annual",
            "statement": statement,
        }

        return {
            "symbol": request.symbol,
            "provider_symbol": provider_symbol,
            "data_type": "financial_statements",
            "statement_type": statement_type,
            "frequency": request.frequency or "annual",
            "statement": statement,
            "financial_statements": {statement_type: statement_payload},
            "timestamp": generated_at.isoformat(),
            "source": self.source,
            "is_stale": False,
            "warnings": [],
        }

    def _statement_type_for(self, request: DataRequest) -> str:
        if request.statement_type is None:
            raise ValueError(
                "financial_statements requests require statement_type "
                "(balance_sheet, income_statement, or cashflow)"
            )
        statement_type = (
            "cashflow" if request.statement_type == "cash_flow" else request.statement_type
        )
        if statement_type not in {"balance_sheet", "income_statement", "cashflow"}:
            raise ValueError(f"Unsupported financial statement type: {request.statement_type}")
        return statement_type

    def _normalize_statement(self, raw: Any, symbol: str) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if hasattr(raw, "empty") and raw.empty:
            return []
        if hasattr(raw, "to_dict"):
            try:
                return self._statement_from_dict(raw.to_dict(orient="index"))
            except TypeError:
                return self._statement_from_dict(raw.to_dict())
        if isinstance(raw, Mapping):
            return self._statement_from_dict(raw)
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
            normalized = []
            for item in raw:
                if not isinstance(item, Mapping):
                    raise ValueError(
                        f"Malformed Yahoo financial statement payload for {symbol}: "
                        "rows must be mappings"
                    )
                normalized.append(
                    {str(key): self._clean_value(value) for key, value in item.items()}
                )
            return normalized
        raise ValueError(
            f"Malformed Yahoo financial statement payload for {symbol}: expected table"
        )

    def _statement_from_dict(self, raw: Mapping[Any, Any]) -> list[dict[str, Any]]:
        rows_by_period: dict[str, dict[str, Any]] = {}
        for metric, values in raw.items():
            metric_name = str(metric)
            if isinstance(values, Mapping):
                for period, value in values.items():
                    period_key = self._period_key(period)
                    row = rows_by_period.setdefault(period_key, {"period": period_key})
                    row[metric_name] = self._clean_value(value)
            else:
                row = rows_by_period.setdefault("current", {"period": "current"})
                row[metric_name] = self._clean_value(values)
        return list(rows_by_period.values())

    def _period_key(self, value: Any) -> str:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _clean_value(self, value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "item"):
            value = value.item()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, float) and value != value:
            return None
        if isinstance(value, (int, float, str, bool)):
            return value
        return str(value)

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

    FORMING_BAR_MAX_AGE_SECONDS = 24 * 60 * 60

    def _build_bars(
        self,
        symbol: str,
        rows: Sequence[Mapping[str, Any]],
        generated_at: datetime,
    ) -> tuple[list[OHLCVBar], list[str]]:
        bars: list[OHLCVBar] = []
        warnings: list[str] = []
        last_index = len(rows) - 1
        for index, row in enumerate(rows):
            try:
                bars.append(self._build_bar(symbol, row))
            except ValueError as exc:
                # Yahoo can serve the still-forming current-session bar with
                # transiently inconsistent OHLC fields (e.g. low > close while
                # the quote assembles). Drop only that bar; malformed completed
                # bars stay fail-closed.
                if index == last_index and self._is_forming_bar_row(row, generated_at):
                    warnings.append(f"Dropped malformed forming bar for {symbol}: {exc}")
                    continue
                raise
        return bars, warnings

    def _is_forming_bar_row(self, row: Mapping[str, Any], generated_at: datetime) -> bool:
        try:
            timestamp = self._parse_timestamp(row.get("timestamp"))
        except (TypeError, ValueError):
            return False
        age_seconds = (generated_at - timestamp).total_seconds()
        return 0 <= age_seconds < self.FORMING_BAR_MAX_AGE_SECONDS

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

    def _is_stale(
        self,
        timestamp: datetime,
        generated_at: datetime,
        *,
        timeframe: str | None = None,
        data_type: str | None = None,
    ) -> bool:
        if self.stale_after_seconds is None:
            return False
        stale_after_seconds = self._effective_stale_after_seconds(timeframe, data_type)
        return timestamp < generated_at - timedelta(seconds=stale_after_seconds)

    def _effective_stale_after_seconds(
        self,
        timeframe: str | None,
        data_type: str | None,
    ) -> int:
        configured = self.stale_after_seconds
        if configured is None:
            raise ValueError("stale_after_seconds is required")
        if data_type == "ohlcv" and timeframe in {"1mo", "1M", "month", "monthly"}:
            return max(configured, 45 * 24 * 60 * 60)
        if data_type == "ohlcv" and timeframe in {"1wk", "1w", "week", "weekly"}:
            return max(configured, 14 * 24 * 60 * 60)
        return configured
