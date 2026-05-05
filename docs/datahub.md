# DataHub v0.2 Payloads

Maestro keeps the public strategy SDK `DataBundle` contract stable. In v0.2, the
shape of each symbol payload inside `DataBundle.data` is more explicit.

```python
{
    "MOCK_ETF_A": {
        "latest_price": {
            "symbol": "MOCK_ETF_A",
            "timestamp": "2026-01-03T00:00:00+00:00",
            "price": 103.0,
            "source": "csv",
        },
        "bars": [
            {
                "symbol": "MOCK_ETF_A",
                "timestamp": "2026-01-03T00:00:00+00:00",
                "open": 102.0,
                "high": 104.0,
                "low": 101.0,
                "close": 103.0,
                "volume": 1200.0,
                "source": "csv",
            }
        ],
        "is_stale": False,
        "warnings": [],
    }
}
```

## Semantics

- `latest_price` is the current reference price Maestro should use for paper
  order sizing and valuation.
- `bars` are historical OHLCV records, sorted by timestamp.
- `is_stale` is a simple freshness flag reserved for v0.2 dashboard and future
  risk checks.
- `warnings` records non-fatal data issues, such as requesting a lookback longer
  than available CSV history.

## Validation

DataHub schema models validate:

- price > 0
- open/high/low/close > 0
- volume >= 0
- high >= low
- high >= open and high >= close
- low <= open and low <= close

Unknown symbols and malformed CSV files fail loudly.

## Compatibility

The orchestrator can still read legacy payloads shaped as `{"price": 100.0}`.
Providers should prefer the v0.2 `latest_price` and `bars` shape going forward.
