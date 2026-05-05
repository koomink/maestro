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

## v0.2 Provider Planning

This section defines the v0.2-to-v0.3 DataHub architecture boundary. It is a
planning contract only: v0.2 still uses mock and CSV providers, with no real
external API calls.

### Provider Interface Concept

Future DataHub providers should implement one small capability: accept validated
`DataRequest` objects and return a `DataBundle` with normalized per-symbol
payloads. Providers should not know about portfolio construction, risk checks,
broker orders, or strategy-specific logic.

Planned payload types:

- `price`: latest reference price for sizing, valuation, and paper execution.
- `ohlcv`: historical open/high/low/close/volume bars.
- `macro`: macroeconomic time series or point-in-time observations.
- `news`: normalized article or event metadata.
- `sentiment`: normalized sentiment/community signals.
- `fundamental`: normalized issuer, asset, or financial statement data.
- `broker_quote`: broker-side reference quote used only for execution
  validation or reconciliation.

### Routing Rules

DataHub should route each request by:

- `symbol`: Maestro canonical symbol.
- `asset_type`: equity, ETF, cash, crypto, or another supported asset type.
- `data_type`: one of the supported payload types above.
- `timeframe` and `lookback`: used for historical series such as `ohlcv`.
- run mode: research, paper, live-read-only, or future live-approval modes.

Routing must remain deterministic. If no provider can satisfy a request, DataHub
should fail loudly rather than silently returning partial research data. Broker
adapters should not be selected for ordinary strategy research data.

### Freshness Policy

Each provider response should carry enough metadata for DataHub to mark payloads
as fresh or stale. v0.2 keeps the policy simple with `is_stale` and `warnings`;
v0.3 can make thresholds configurable per data type.

Default planning assumptions:

- `price` and `broker_quote`: strict freshness, because stale values can affect
  sizing or reconciliation.
- `ohlcv`: freshness depends on timeframe; daily bars can tolerate a longer
  delay than intraday bars.
- `macro` and `fundamental`: slower-moving data; freshness is usually measured
  in days or release cycles.
- `news` and `sentiment`: freshness depends on strategy horizon and should be
  explicit in the request or provider config.

Stale data should be visible to strategies through the payload and available to
future risk checks. It should not be hidden by provider-specific behavior.

### Cache and Storage Policy

v0.2 should not add a full research data database. The near-term plan is:

- Keep mock data in memory.
- Keep CSV/local data in files.
- Allow future providers to use small in-process or file-backed caches for
  rate-limit protection and repeatability.
- Persist only operational state, audit logs, broker snapshots, and dashboard
  read models in the existing SQLite state store.

If v0.3 needs durable provider caches, it should add them behind DataHub rather
than mixing research data storage into strategy plugins or broker adapters.

### Symbol and Provider Mapping

Maestro should use canonical symbols inside strategies, portfolio targets, risk,
and execution. Provider-specific symbols should be mapped at the DataHub
boundary.

The lightweight v0.2 `SymbolMetadata` model is the starting point. A future
symbol registry can add provider aliases, exchange identifiers, currency, asset
type, tradability, quantity constraints, and minimum notional rules. Strategies
should not carry provider-specific ticker translation logic.

### Schema Compatibility

The public SDK stays centered on `DataRequest` and `DataBundle`. Strategy
plugins request data through Maestro DataHub and receive normalized payloads;
they should not call Yahoo, FRED, news APIs, crypto exchanges, KIS, or other
external APIs directly.

For v0.2 compatibility, the orchestrator can still extract prices from legacy
payloads shaped as `{"price": 100.0}`. New providers should emit the explicit
`latest_price` shape.

### `broker_quote` Semantics

`broker_quote` is broker-side reference data. It may come from a broker adapter,
including KIS, to validate order prices, compare expected versus broker-visible
quotes, or support reconciliation.

`broker_quote` is not a primary strategy or research feed. Strategy research
should use DataHub market/research providers such as CSV/local today and future
Yahoo Finance/yfinance-style OHLCV, FRED, news, sentiment, fundamental, or crypto
market data providers later.
