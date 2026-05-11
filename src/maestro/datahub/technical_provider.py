from datetime import UTC, datetime
from typing import Any

from maestro.core.clock import utc_now
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import ProviderUnavailableError
from maestro.datahub.yahoo_provider import YahooDataProvider
from maestro.sdk import DataBundle, DataRequest


class TechnicalIndicatorProvider(BaseDataProvider):
    source = "technical_indicators"

    def __init__(self, ohlcv_provider: BaseDataProvider | None = None) -> None:
        self.ohlcv_provider = ohlcv_provider or YahooDataProvider()

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        generated_at = utc_now()
        data: dict[str, Any] = {}
        for request in requests:
            indicator = self._indicator_for(request)
            bars = self._bars_for(request)
            values = self._calculate(indicator, bars, request)
            if not values:
                raise ValueError(
                    f"Not enough OHLCV data to calculate {indicator} for {request.symbol}"
                )
            indicator_payload = {
                "indicator": indicator,
                "values": values,
            }
            data[request.symbol] = {
                "symbol": request.symbol,
                "data_type": "technical_indicators",
                "indicator": indicator,
                "values": values,
                "technical_indicators": {indicator: indicator_payload},
                "timestamp": generated_at.isoformat(),
                "source": self.source,
                "is_stale": False,
                "warnings": [],
            }
        return DataBundle(
            requests=requests,
            data=data,
            generated_at=generated_at,
            source=self.source,
        )

    def _indicator_for(self, request: DataRequest) -> str:
        if request.indicator is None or not request.indicator.strip():
            raise ValueError("technical_indicators requests require indicator")
        indicator = request.indicator.strip().lower()
        aliases = {
            "bollinger": "bollinger",
            "boll": "bollinger",
            "bbands": "bollinger",
            "macd": "macd",
            "rsi": "rsi",
            "sma": "sma",
            "ema": "ema",
        }
        if indicator not in aliases:
            raise ValueError(f"Unsupported technical indicator: {request.indicator}")
        return aliases[indicator]

    def _bars_for(self, request: DataRequest) -> list[dict[str, Any]]:
        ohlcv_request = request.model_copy(update={"data_type": "ohlcv"})
        try:
            bundle = self.ohlcv_provider.get_data([ohlcv_request])
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(
                f"OHLCV provider unavailable for technical indicator {request.symbol}: {exc}"
            ) from exc

        payload = bundle.data.get(request.symbol)
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed OHLCV payload for {request.symbol}: expected object")
        bars = payload.get("bars")
        if not isinstance(bars, list):
            raise ValueError(f"Malformed OHLCV payload for {request.symbol}: missing bars")
        return [self._validated_bar(request.symbol, item) for item in bars]

    def _validated_bar(self, symbol: str, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError(f"Malformed OHLCV bar for {symbol}: expected object")
        try:
            close = float(item["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed OHLCV bar for {symbol}: invalid close") from exc
        timestamp = self._timestamp_for(item.get("timestamp"))
        return {"timestamp": timestamp, "close": close}

    def _timestamp_for(self, value: Any) -> str:
        if value is None:
            return datetime.now(UTC).isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _calculate(
        self,
        indicator: str,
        bars: list[dict[str, Any]],
        request: DataRequest,
    ) -> list[dict[str, Any]]:
        closes = [bar["close"] for bar in bars]
        window = request.lookback or self._default_window(indicator)
        if indicator == "sma":
            series = self._sma(closes, window)
        elif indicator == "ema":
            series = self._ema(closes, window)
        elif indicator == "rsi":
            series = self._rsi(closes, window)
        elif indicator == "macd":
            return self._macd_values(bars, closes)
        elif indicator == "bollinger":
            return self._bollinger_values(bars, closes, window)
        else:
            raise ValueError(f"Unsupported technical indicator: {request.indicator}")
        return [
            {"timestamp": bar["timestamp"], "value": value}
            for bar, value in zip(bars, series, strict=True)
            if value is not None
        ]

    def _default_window(self, indicator: str) -> int:
        if indicator == "rsi":
            return 14
        if indicator == "bollinger":
            return 20
        return 20

    def _sma(self, values: list[float], window: int) -> list[float | None]:
        if window <= 0:
            raise ValueError("indicator lookback must be positive")
        result: list[float | None] = []
        for index in range(len(values)):
            if index + 1 < window:
                result.append(None)
                continue
            sample = values[index + 1 - window : index + 1]
            result.append(sum(sample) / window)
        return result

    def _ema(self, values: list[float], window: int) -> list[float | None]:
        if window <= 0:
            raise ValueError("indicator lookback must be positive")
        if not values:
            return []
        alpha = 2 / (window + 1)
        result: list[float | None] = []
        ema_value: float | None = None
        for index, value in enumerate(values):
            if index + 1 < window:
                result.append(None)
                continue
            if ema_value is None:
                ema_value = sum(values[index + 1 - window : index + 1]) / window
            else:
                ema_value = (value - ema_value) * alpha + ema_value
            result.append(ema_value)
        return result

    def _rsi(self, values: list[float], window: int) -> list[float | None]:
        if window <= 0:
            raise ValueError("indicator lookback must be positive")
        if len(values) <= window:
            return [None for _ in values]
        result: list[float | None] = [None for _ in values]
        gains: list[float] = []
        losses: list[float] = []
        for previous, current in zip(values, values[1:], strict=False):
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        for index in range(window, len(values)):
            avg_gain = sum(gains[index - window : index]) / window
            avg_loss = sum(losses[index - window : index]) / window
            if avg_loss == 0:
                result[index] = 100.0
            else:
                rs = avg_gain / avg_loss
                result[index] = 100 - (100 / (1 + rs))
        return result

    def _macd_values(
        self,
        bars: list[dict[str, Any]],
        closes: list[float],
    ) -> list[dict[str, Any]]:
        fast = self._ema(closes, 12)
        slow = self._ema(closes, 26)
        macd_line: list[float | None] = [
            fast_value - slow_value if fast_value is not None and slow_value is not None else None
            for fast_value, slow_value in zip(fast, slow, strict=True)
        ]
        signal = self._ema([value or 0.0 for value in macd_line], 9)
        values = []
        for bar, macd_value, signal_value in zip(bars, macd_line, signal, strict=True):
            if macd_value is None or signal_value is None:
                continue
            values.append(
                {
                    "timestamp": bar["timestamp"],
                    "macd": macd_value,
                    "signal": signal_value,
                    "histogram": macd_value - signal_value,
                }
            )
        return values

    def _bollinger_values(
        self,
        bars: list[dict[str, Any]],
        closes: list[float],
        window: int,
    ) -> list[dict[str, Any]]:
        if window <= 0:
            raise ValueError("indicator lookback must be positive")
        values = []
        for index, bar in enumerate(bars):
            if index + 1 < window:
                continue
            sample = closes[index + 1 - window : index + 1]
            middle = sum(sample) / window
            variance = sum((value - middle) ** 2 for value in sample) / window
            stddev = variance**0.5
            values.append(
                {
                    "timestamp": bar["timestamp"],
                    "middle": middle,
                    "upper": middle + 2 * stddev,
                    "lower": middle - 2 * stddev,
                }
            )
        return values
