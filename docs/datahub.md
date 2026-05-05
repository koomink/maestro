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

## v0.3 Provider Scaffold

v0.3 starts with a lightweight provider scaffold, not real integrations.

Implemented scaffold:

- `DataHubRegistry` records provider capabilities: data types, optional symbols,
  optional asset types, optional run modes, priority, and availability.
- `DataHubRouter` selects a provider for each `DataRequest`, groups requests by
  provider, normalizes provider payloads, and combines them into one
  `DataBundle`.
- DataHub errors distinguish unsupported data types, no matching provider,
  unavailable providers, and stale data when stale payloads are disallowed.
- Existing `mock` and `csv` configs are still supported through the router.

The scaffold recognizes these data types:

```text
price, ohlcv, macro, news, sentiment, fundamental, broker_quote
```

Current built-in providers remain local-only:

- `mock`: supports `price` and `ohlcv` fixture-style payloads.
- `csv`: supports `price` and `ohlcv` from local CSV files.

No Yahoo, FRED, news, GDELT, sentiment/community, crypto, or KIS network provider
is implemented in this scaffold. Those remain future v0.3 provider work.

### Multi-Provider Config

Existing single-provider configs still work:

```yaml
datahub:
  provider: mock
```

```yaml
datahub:
  provider: csv
  csv_path: data/sample_prices.csv
```

v0.3 also supports an optional `datahub.providers` list for multiple local
providers. The router chooses the first matching provider by lowest `priority`;
when priorities match, config order is the source preference. Disabled providers
are ignored. When `providers` is present, it takes precedence over the legacy
single `provider` field.

```yaml
datahub:
  providers:
    - name: local_csv_prices
      provider: csv
      priority: 10
      data_types: [price, ohlcv]
      asset_types: [domestic_etf]
      run_modes: [paper]
      csv_path: data/sample_prices.csv
    - name: mock_fallback
      provider: mock
      priority: 100
      data_types: [price, ohlcv]
```

Future external providers should use the same shape, without strategy plugins
calling those APIs directly:

```yaml
datahub:
  providers:
    - name: yahoo_ohlcv
      provider: yahoo
      priority: 20
      data_types: [price, ohlcv, fundamental]
    - name: fred_macro
      provider: fred
      priority: 30
      data_types: [macro]
    - name: news_events
      provider: news
      priority: 40
      data_types: [news]
    - name: community_sentiment
      provider: sentiment
      priority: 50
      data_types: [sentiment]
    - name: crypto_market_data
      provider: crypto_exchange
      priority: 20
      data_types: [price, ohlcv]
      symbols: [BTC-USD]
```

Those provider names are planning examples only. The current implementation
rejects them until real provider adapters are added. Crypto asset-type support,
secrets, API keys, timeouts, rate limits, and durable cache settings remain
future v0.3 design work.

### Provider Operations Planning

Future external research providers must keep operational concerns inside
DataHub. Strategy plugins continue to request data through `DataRequest` and must
not read secrets, construct API clients, or call provider APIs directly.

Secrets handling:

- Provider credentials should be referenced by environment variable name in
  config, not stored as raw secrets in YAML.
- Providers that do not require credentials, such as some Yahoo/yfinance-style
  market data paths, should make that explicit in their adapter docs.
- Required secrets should be validated when the provider is built. Missing
  credentials should make that provider unavailable rather than falling back to a
  broker data source for strategy research.
- Secret values must not be written to audit logs, dashboard read models, errors,
  or cached payloads.

Timeout, retry, and rate-limit behavior:

- Every network-backed provider should have a small explicit timeout.
- Retries should be bounded and reserved for transient transport or rate-limit
  failures. Provider adapters should not retry validation errors, unsupported
  symbols, or malformed responses.
- Rate-limit responses should become provider-level unavailable or stale-data
  outcomes with clear warnings; they should not silently return partial research
  data as fresh.
- Provider adapters should normalize failures into DataHub errors or stale
  payload metadata before returning to the router.

Provider-unavailable behavior:

- If a matching provider is configured but disabled, missing credentials,
  rate-limited, or otherwise unavailable, DataHub should try the next matching
  provider by priority.
- If all matching providers are unavailable, DataHub should raise a
  provider-unavailable error instead of returning empty data.
- If no provider matches the request at all, DataHub should raise a no-provider
  error. That is different from an unavailable configured provider.
- `broker_quote` providers remain broker-side reference data for execution
  validation or reconciliation and must not become the fallback for research
  `price` or `ohlcv` requests.

Testing policy for future external providers:

- Unit tests should use fake clients or fixture payloads for Yahoo/yfinance,
  FRED, news, sentiment, and crypto providers.
- Normalization, freshness, timeout, retry, rate-limit, and error mapping should
  be tested without real network calls.
- Optional integration tests that contact real services should be explicitly
  marked, skipped by default, and isolated from normal `pytest -q` runs.
- Fixtures should include successful payloads, unsupported symbols, stale
  responses, provider-unavailable responses, malformed provider data, and
  rate-limit cases.

Remaining real provider work:

- Implement real provider adapters for Yahoo/yfinance-style price and OHLCV,
  FRED macro data, RSS/GDELT/news, sentiment/community feeds, and crypto market
  data.
- Add provider-specific config fields only when each adapter needs them.
- Add bounded timeout/retry/rate-limit code inside each adapter.
- Add provider-specific schema normalization and fixture-backed tests.
- Add any cache only behind DataHub and only if needed for repeatability or
  rate-limit protection; v0.3 should not add heavy database infrastructure.
