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

- DataHub is an internal Maestro module. Its providers are internal adapters
  that may call external market/research systems such as Yahoo/yfinance, FRED,
  RSS feeds, or local CSV files.
- External systems are not part of Maestro. Strategy plugins never call them
  directly.
- Broker adapters, including KIS, are not DataHub providers for strategy
  research data. They belong to Maestro's account, execution, and reconciliation
  boundary.
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
- `financial_statements`: normalized balance sheet, income statement, or cash
  flow rows.
- `technical_indicators`: indicator values derived from OHLCV bars.
- `broker_quote`: broker-side reference quote used only for execution
  validation or reconciliation.

### Routing Rules

DataHub should route each request by:

- `symbol`: Maestro canonical symbol.
- `asset_type`: equity, ETF, cash, crypto, or another supported asset type.
- `data_type`: one of the supported payload types above.
- `timeframe` and `lookback`: used for historical series such as `ohlcv` and
  technical indicators.
- `indicator`: used when requesting `technical_indicators`, such as `rsi`,
  `macd`, `sma`, `ema`, or `bollinger`.
- `statement_type`: used when requesting `financial_statements`; supported
  values are `balance_sheet`, `income_statement`, `cashflow`, and `cash_flow`.
- `frequency`: optional statement frequency hint such as `annual` or
  `quarterly`.
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

`universe.instruments` is now the product/venue-aware source for tradable
instrument metadata such as currency, broker product, exchange code, broker
symbol, price tick, quantity step, minimum quantity, and minimum notional.
DataHub provider symbol maps translate Maestro canonical symbols into
provider-specific research symbols. Strategies should not carry provider-specific
ticker translation logic.

Static `allowed_symbols` lists are acceptable as an explicit safety override.
When omitted, Maestro derives the allowed set from `portfolio.currency_sleeves`
or from all configured `universe.instruments`. They are not the intended
production ceiling. The future production model should support a policy-based
dynamic universe where Virtuoso apps can propose candidate symbols, and Maestro
validates and resolves them before they can become tradable.
Cash instruments such as `CASH_KRW` and `CASH_USD` are derived from portfolio
cash symbols when omitted.

The research universe and tradable universe are separate:

- Research universe: symbols, series, and keywords used for analysis, such as
  `SPY`, `VIX`, `DXY`, FRED macro series, RSS/news keywords, or sentiment
  topics. Research inputs may be broad and may include non-tradable references.
- Tradable universe: canonical instruments eligible for allocation and
  execution. Tradable candidates must pass stricter Maestro validation,
  including instrument metadata resolution, DataHub availability/freshness,
  broker product mapping, and broker tradability checks when required.

Planned dynamic candidate flow:

1. A Virtuoso app proposes candidate symbols or data needs, marking each request
   with `intended_use: research` or `intended_use: tradable`.
2. Maestro validates candidates against `UniversePolicy`.
3. Maestro resolves canonical instrument metadata through `InstrumentResolver`.
4. Maestro checks that required DataHub providers can serve fresh data.
5. For tradable candidates, Maestro verifies broker mapping and tradability when
   policy requires it.
6. Approved candidates become temporary or persistent tradable universe entries.
7. Allocation validation accepts only approved tradable symbols.

Planned `UniversePolicy` fields include `allowed_asset_types`,
`allowed_regions`, `allowed_currencies`, `allowed_broker_products`,
`deny_symbols`, `deny_asset_tags` such as leveraged, inverse, OTC, options, and
futures, `max_new_symbols_per_run`,
`require_operator_approval_for_new_symbols`,
`require_broker_tradability_check`, and `require_data_freshness_check`.

Virtuoso apps can propose candidates but cannot approve tradability, bypass
Maestro DataHub, call broker APIs, or execute orders. Maestro should reject
unknown, unresolved, untradable, and research-only symbols in
`TargetAllocationResult.allocations`.

### Schema Compatibility

The public SDK stays centered on `DataRequest` and `DataBundle`. Strategy
plugins request data through Maestro DataHub and receive normalized payloads;
they should not call Yahoo, FRED, news APIs, crypto exchanges, KIS, or other
external APIs directly.

SDK contract 1.0 includes `CandidateInstrumentRequest`,
`intended_use: research | tradable`, richer `DataRequest` fields for
TradingAgents-style research requests, `TargetAllocationResult.metadata`, and
`StrategyManifest` capability fields such as `supports_dynamic_universe`,
`max_candidate_symbols`, `allowed_data_types`, `requires_llm`,
`supported_llm_providers`, and `estimated_runtime_seconds`.

For v0.2 compatibility, the orchestrator can still extract prices from legacy
payloads shaped as `{"price": 100.0}`. New providers should emit the explicit
`latest_price` shape.

### `broker_quote` Semantics

`broker_quote` is broker-side reference data. It may come from a broker adapter,
including KIS, to validate order prices, compare expected versus broker-visible
quotes, or support reconciliation.

`broker_quote` is not a primary strategy or research feed. Strategy research
should use DataHub market/research providers such as CSV/local, Yahoo
Finance/yfinance-style OHLCV, FRED macro, RSS/GDELT/NewsAPI news, and configured
sentiment providers. Crypto market data is deferred until the supported universe
expands beyond stocks and ETFs.

## v0.3 Provider Scope

v0.3 closes out real external research providers for the current stock/ETF
universe. Yahoo/yfinance, FRED, RSS, GDELT, and opt-in NewsAPI can make live
network calls when configured; normal tests remain fake-client and fixture
based, and live-network smoke tests are skipped by default. Sentiment is
intentionally network-free for v0.3 and analyzes configured fixture/news text
only.

DataHub is not yet a complete production market data system. It is the Maestro
boundary for research and market data requests. Before repeated real-account
operation, live approval needs stricter DataHub behavior around freshness,
session validity, provider failure, proposal snapshots, and broker quote
validation.

Implemented routing scaffold:

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
price, ohlcv, macro, news, sentiment, fundamental, financial_statements,
technical_indicators, insider_transactions, broker_quote
```

Current provider status:

- `mock`: supports `price` and `ohlcv` fixture-style payloads for development
  and tests; it is not production market data.
- `csv`: supports `price` and `ohlcv` from local CSV files.
- `yahoo` / `yfinance`: supports `price`, `ohlcv`, `fundamental`, and
  `financial_statements` through a small Yahoo/yfinance-style client wrapper and
  can call external Yahoo/yfinance data when configured. Yahoo configs also
  register a `technical_indicators` provider that derives indicators from OHLCV.
- `fred`: supports `macro` through a small stdlib HTTP client wrapper.
- `rss`: supports `news` through a small stdlib HTTP/XML client wrapper.
- `gdelt`: supports `news` through the GDELT DOC 2.0 article-list API.
- `newsapi`: supports `news` through NewsAPI `/v2/everything` with an API key.
- `sentiment`: supports `sentiment` through configured fixture/news text and a
  lightweight rule-based analyzer.

No Reddit/X/Discord/Telegram/community API, crypto, or KIS network research
provider is implemented. KIS current price data may be used as
`broker_quote` reference data by broker/execution/reconciliation logic, not as
the primary strategy research feed. NewsAPI is opt-in for strategy research/news
discovery because it requires an API key and may be plan/quota limited.
Community sentiment APIs remain future provider work. Crypto is explicitly
deferred because the current supported universe is stocks and ETFs only.

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
      data_types: [price, ohlcv]
      timeout_seconds: 5
      stale_after_seconds: 86400
    - name: fred_macro
      provider: fred
      priority: 30
      data_types: [macro]
      api_key_env: FRED_API_KEY
      timeout_seconds: 5
      stale_after_seconds: 7776000
    - name: news_events
      provider: rss
      priority: 40
      data_types: [news]
      feed_urls:
        - https://example.com/rss
      timeout_seconds: 5
      stale_after_seconds: 604800
    - name: gdelt_news
      provider: gdelt
      priority: 45
      data_types: [news]
      gdelt_timespan: 24h
      gdelt_max_records: 25
      timeout_seconds: 5
      stale_after_seconds: 604800
      symbol_map:
        FED: Federal Reserve
    - name: community_sentiment
      provider: sentiment
      priority: 50
      data_types: [sentiment]
      sentiment_texts:
        - SPY posts strong gains as confidence improves.
        - Federal Reserve commentary raises slowdown risks.
      symbol_map:
        SPY: SPY
        FED: Federal Reserve,Fed
      source_name: fixture_news
      stale_after_seconds: 86400
```

`configs/examples/live_approval_kis_multi_asset.yaml` uses this multi-provider
shape for KR+US live approval. It keeps only `yahoo_market` active for
`price`, `ohlcv`, and `technical_indicators`; no mock or CSV fallback is
configured for the live-approval real-data path. KIS remains outside DataHub and
is used only for broker account, execution, and reconciliation boundaries.

`configs/examples/paper_research_multi_provider.yaml` is a reference template for
strategy research provider composition. It registers Yahoo market/fundamental
data, FRED macro, GDELT/RSS news, rule-based sentiment, and a disabled
NewsAPI provider that operators can enable after configuring `NEWSAPI_API_KEY`
and checking their plan/quota. The template does not make FRED, news, or
sentiment calls unless a strategy issues matching `DataRequest` objects.

The Yahoo/yfinance-style provider is implemented for `price`, `ohlcv`,
`fundamental`, and `financial_statements`. A separate technical indicator
provider derives `technical_indicators` from OHLCV bars. The FRED provider is
implemented for `macro` only. The RSS provider is implemented for `news` only.
The GDELT and NewsAPI providers are implemented for `news` only and are intended
for strategy research/news discovery only, not execution, broker validation, or
direct strategy-side API access. The rule-based sentiment provider is
implemented for configured fixture/news text only. Reddit/X/Discord/Telegram
community APIs and paid sentiment APIs remain planning examples only. Crypto
provider work is deferred until the supported universe expands beyond stocks and
ETFs. The current implementation rejects unsupported provider names until real
provider adapters are added. Yahoo, FRED, RSS, GDELT, and NewsAPI can be wrapped
with bounded retry, in-process TTL cache, and request-spacing controls. Durable
cache settings remain future design work where a provider needs repeatability
beyond one process.

### Yahoo/yfinance Provider

The Yahoo/yfinance-style provider is the first real external research provider.
It remains behind DataHub and normalizes provider rows into `PricePoint`,
`OHLCVBar`, `SymbolData`, fundamental metrics, and financial statement rows.

Supported behavior:

- `price`: latest close from the selected Yahoo/yfinance history rows.
- `ohlcv`: normalized OHLCV bars from Yahoo/yfinance history rows.
- `fundamental`: selected key metrics from `Ticker.info`, including PE, PB,
  market cap, dividend yield, beta, and profitability/growth fields.
- `financial_statements`: normalized rows from `balance_sheet`, `financials`,
  or `cashflow`, routed by `DataRequest.statement_type`.
- `technical_indicators`: `rsi`, `macd`, `sma`, `ema`, and `bollinger`, derived
  from Yahoo OHLCV when the Yahoo provider is configured.
- `timeout_seconds`: passed to the Yahoo/yfinance client wrapper.
- `stale_after_seconds`: optional freshness threshold that marks payloads stale.
- `symbol_map`: canonical Maestro symbol to provider-specific Yahoo symbol
  mapping. When omitted in a full Maestro config, Yahoo/yfinance mappings are
  derived from `universe.instruments`: KIS domestic symbols use `.KS`, and KIS
  overseas symbols use the broker symbol unchanged. Explicit entries override
  derived entries.

The provider uses an optional lazy `yfinance` client wrapper at runtime. Normal
tests use fake clients and fixture rows, so `pytest -q` does not require live
network access or a real Yahoo/yfinance call.

Malformed provider rows fail loudly. Timeouts and client availability failures
are normalized as provider-unavailable errors so the router can try the next
matching provider by priority.

Example strategy data requests:

```python
DataRequest(
    symbol="AAPL",
    asset_type="stock",
    data_type="financial_statements",
    statement_type="income_statement",
    frequency="annual",
)

DataRequest(
    symbol="AAPL",
    asset_type="stock",
    data_type="technical_indicators",
    indicator="rsi",
    lookback=14,
)
```

Install the optional runtime dependency before using live Yahoo/yfinance calls:

```bash
pip install "maestro[yahoo]"
```

For local development with `uv`:

```bash
uv sync --extra yahoo
```

Single-provider example:

```yaml
datahub:
  provider: yahoo
  timeout_seconds: 5
  stale_after_seconds: 86400
```

Multi-provider example with local fallback:

```yaml
datahub:
  providers:
    - name: yahoo_primary
      provider: yahoo
      priority: 10
      data_types: [price, ohlcv]
      timeout_seconds: 5
      stale_after_seconds: 86400
    - name: csv_fallback
      provider: csv
      priority: 100
      data_types: [price, ohlcv]
      csv_path: data/sample_prices.csv
```

Config fields:

- `timeout_seconds`: maximum time passed to the yfinance history call. The
  provider maps timeout failures to provider-unavailable so the router can try a
  lower-priority fallback.
- `stale_after_seconds`: optional threshold for marking the latest Yahoo bar as
  stale. Omit it when staleness should be evaluated elsewhere.
- `symbol_map`: optional override for Maestro canonical symbols to
  Yahoo/yfinance symbols. In a full Maestro config, omit it when
  `universe.instruments[].broker_symbol` can derive the Yahoo symbol. Keep
  strategy plugins on Maestro symbols and put exceptional provider-specific
  ticker aliases here.
- `retry_max_attempts`: optional bounded retry count for provider-unavailable
  failures. The default `1` means one attempt and no retry.
- `retry_backoff_seconds`: optional linear backoff between retry attempts.
- `cache_ttl_seconds`: optional per-process TTL cache for successful provider
  responses. Cache hits do not call the upstream provider.
- `min_request_interval_seconds`: optional minimum spacing between actual
  upstream calls for this provider instance.

Live integration checks are optional and skipped by default:

```bash
MAESTRO_RUN_YFINANCE_INTEGRATION=1 uv run pytest tests/test_yahoo_integration.py
```

Normal `pytest -q` remains fake-client and fixture based, with no live network
calls.

### FRED Macro Provider

The FRED provider is the second real external research provider. It supports
`macro` requests and normalizes FRED series observations into a simple macro
payload:

```python
{
    "GDP": {
        "series_id": "GDP",
        "provider_series_id": "GDP",
        "latest": {"date": "2025-07-01", "value": 102.3, "source": "fred"},
        "observations": [
            {"date": "2025-04-01", "value": 101.2, "source": "fred"},
            {"date": "2025-07-01", "value": 102.3, "source": "fred"},
        ],
        "is_stale": False,
        "warnings": [],
        "source": "fred",
    }
}
```

The provider uses stdlib HTTP and does not add a core or optional package
dependency. It requires an API key stored in an environment variable; config
stores only the environment variable name.

Single-provider example:

```yaml
datahub:
  provider: fred
  api_key_env: FRED_API_KEY
  timeout_seconds: 5
  stale_after_seconds: 7776000
  symbol_map:
    REAL_GDP: GDPC1
```

Multi-provider example:

```yaml
datahub:
  providers:
    - name: fred_macro
      provider: fred
      priority: 20
      data_types: [macro]
      api_key_env: FRED_API_KEY
      timeout_seconds: 5
      stale_after_seconds: 7776000
      symbol_map:
        REAL_GDP: GDPC1
```

Config fields:

- `api_key_env`: environment variable name that holds the FRED API key. Secret
  values must not be placed in YAML, logs, dashboard read models, errors, or
  cached payloads.
- `timeout_seconds`: maximum time for the FRED HTTP call. Timeout failures are
  mapped to provider-unavailable so the router can try a lower-priority fallback.
- `stale_after_seconds`: optional threshold for marking the latest observation
  stale.
- `symbol_map`: maps Maestro canonical macro symbols to FRED series IDs.
- `retry_max_attempts`, `retry_backoff_seconds`, `cache_ttl_seconds`, and
  `min_request_interval_seconds`: optional shared external-provider operations
  controls. Retries apply only to provider-unavailable failures.

Malformed FRED payloads fail loudly. Missing API keys and transport failures are
normalized as provider-unavailable errors.

Live integration checks are optional and skipped by default:

```bash
MAESTRO_RUN_FRED_INTEGRATION=1 FRED_API_KEY=... uv run pytest tests/test_fred_integration.py
```

Normal `pytest -q` remains fake-client and fixture based, with no live network
calls.

### RSS News Provider

The RSS provider is the first real news provider. It supports `news` requests
and normalizes feed items into a simple news payload:

```python
{
    "MARKET": {
        "symbol": "MARKET",
        "latest": {
            "title": "Latest market story",
            "url": "https://example.com/story",
            "published_at": "2026-01-01T00:00:00+00:00",
            "summary": "Story summary",
            "feed_url": "https://example.com/rss",
            "source": "Example News",
        },
        "items": [
            {
                "title": "Latest market story",
                "url": "https://example.com/story",
                "published_at": "2026-01-01T00:00:00+00:00",
                "summary": "Story summary",
                "feed_url": "https://example.com/rss",
                "source": "Example News",
            }
        ],
        "is_stale": False,
        "warnings": [],
        "source": "rss",
    }
}
```

The provider uses stdlib HTTP and XML parsing and does not add a core or
optional package dependency. Normal tests use fake clients and fixture XML, so
`pytest -q` does not require live network access.

Single-provider example:

```yaml
datahub:
  provider: rss
  feed_urls:
    - https://example.com/rss
  timeout_seconds: 5
  stale_after_seconds: 604800
  symbol_map:
    FED: Federal Reserve
  source_map:
    https://example.com/rss: Example News
```

Multi-provider example:

```yaml
datahub:
  providers:
    - name: rss_news
      provider: rss
      priority: 40
      data_types: [news]
      feed_urls:
        - https://example.com/rss
      timeout_seconds: 5
      stale_after_seconds: 604800
      symbol_map:
        FED: Federal Reserve
      source_map:
        https://example.com/rss: Example News
```

Config fields:

- `feed_urls`: one or more RSS feed URLs. The provider fetches each feed for a
  news request and merges normalized items.
- `timeout_seconds`: maximum time for each RSS HTTP call. Timeout and transport
  failures are mapped to provider-unavailable so the router can try a fallback.
- `stale_after_seconds`: optional threshold for marking the latest dated item
  stale.
- `symbol_map`: optional request-symbol to keyword mapping. When present, the
  provider filters feed items whose title or summary contains that keyword.
- `retry_max_attempts`, `retry_backoff_seconds`, `cache_ttl_seconds`, and
  `min_request_interval_seconds`: optional shared external-provider operations
  controls. Cache is process-local and not a durable research data store.
- `source_map`: optional feed URL to display-source mapping. When omitted, the
  feed title or URL is used as the item source.

Malformed RSS XML, malformed item dates, and items missing title or URL fail
loudly. Empty feeds fail loudly instead of returning fresh empty news.

Live integration checks are optional and skipped by default:

```bash
MAESTRO_RUN_RSS_INTEGRATION=1 uv run pytest tests/test_rss_integration.py
```

Normal `pytest -q` remains fake-client and fixture based, with no live network
calls.

### GDELT News Provider

The GDELT provider supports searchable `news` requests for strategy research and
normalizes GDELT DOC 2.0 article-list results into the same RSS-compatible news
payload shape:

```python
{
    "FED": {
        "symbol": "FED",
        "latest": {
            "title": "Latest Federal Reserve story",
            "url": "https://example.com/story",
            "published_at": "2026-01-01T00:00:00+00:00",
            "summary": None,
            "source": "example.com",
            "domain": "example.com",
            "language": "English",
            "source_country": "United States",
            "social_image": "https://example.com/image.jpg",
        },
        "items": [
            {
                "title": "Latest Federal Reserve story",
                "url": "https://example.com/story",
                "published_at": "2026-01-01T00:00:00+00:00",
                "summary": None,
                "source": "example.com",
                "domain": "example.com",
                "language": "English",
                "source_country": "United States",
                "social_image": "https://example.com/image.jpg",
            }
        ],
        "is_stale": False,
        "warnings": [],
        "source": "gdelt",
    }
}
```

Single-provider example:

```yaml
datahub:
  provider: gdelt
  timeout_seconds: 5
  stale_after_seconds: 604800
  gdelt_timespan: 24h
  gdelt_max_records: 25
  symbol_map:
    FED: Federal Reserve
```

Multi-provider example:

```yaml
datahub:
  providers:
    - name: gdelt_news
      provider: gdelt
      priority: 40
      data_types: [news]
      timeout_seconds: 5
      stale_after_seconds: 604800
      gdelt_timespan: 24h
      gdelt_max_records: 25
      symbol_map:
        FED: Federal Reserve
```

Config fields:

- `gdelt_base_url`: GDELT DOC 2.0 endpoint. Defaults to
  `https://api.gdeltproject.org/api/v2/doc/doc`.
- `gdelt_timespan`: recent article window to request. Defaults to `24h`.
- `gdelt_max_records`: max articles requested from GDELT. Defaults to `25` and
  is capped at `250`.
- `symbol_map`: maps Maestro request symbols to GDELT query strings. When
  omitted, the request symbol is used as the query.
- `timeout_seconds`: maximum time for the GDELT HTTP call. Timeout and
  transport failures are mapped to provider-unavailable so the router can try a
  fallback.
- `stale_after_seconds`: optional threshold for marking the latest article
  stale.
- `retry_max_attempts`, `retry_backoff_seconds`, `cache_ttl_seconds`, and
  `min_request_interval_seconds`: optional shared external-provider operations
  controls. Cache is process-local and not a durable research data store.

GDELT is for research/news discovery only. Strategy plugins continue to request
`news` through DataHub and should not call GDELT directly. It is not an
execution, broker validation, or reconciliation source. Empty article results,
missing article lists, malformed article fields, malformed JSON, and invalid
article dates fail loudly.

Live integration checks are optional and skipped by default:

```bash
MAESTRO_RUN_GDELT_INTEGRATION=1 uv run pytest tests/test_gdelt_integration.py
```

Normal `pytest -q` remains fake-client and fixture based, with no live network
calls.

### NewsAPI News Provider

The NewsAPI provider supports searchable `news` requests for strategy research
and normalizes `/v2/everything` results into the same RSS/GDELT-compatible news
payload shape:

```python
{
    "FED": {
        "symbol": "FED",
        "latest": {
            "title": "Latest Federal Reserve story",
            "url": "https://example.com/story",
            "published_at": "2026-01-01T00:00:00+00:00",
            "summary": "Story description",
            "source": "Example News",
            "source_id": "example-news",
            "author": "Reporter",
            "url_to_image": "https://example.com/image.jpg",
            "content": "Article content excerpt",
        },
        "items": [
            {
                "title": "Latest Federal Reserve story",
                "url": "https://example.com/story",
                "published_at": "2026-01-01T00:00:00+00:00",
                "summary": "Story description",
                "source": "Example News",
                "source_id": "example-news",
                "author": "Reporter",
                "url_to_image": "https://example.com/image.jpg",
                "content": "Article content excerpt",
            }
        ],
        "is_stale": False,
        "warnings": [],
        "source": "newsapi",
    }
}
```

Single-provider example:

```yaml
datahub:
  provider: newsapi
  timeout_seconds: 5
  stale_after_seconds: 604800
  newsapi_api_key_env: NEWSAPI_API_KEY
  newsapi_page_size: 25
  newsapi_sort_by: publishedAt
  newsapi_language: en
  newsapi_search_in: title,description,content
  newsapi_domains:
    - reuters.com
  symbol_map:
    FED: Federal Reserve
```

Multi-provider example:

```yaml
datahub:
  providers:
    - name: newsapi_research
      provider: newsapi
      priority: 30
      data_types: [news]
      timeout_seconds: 5
      stale_after_seconds: 604800
      newsapi_api_key_env: NEWSAPI_API_KEY
      newsapi_page_size: 25
      newsapi_sort_by: publishedAt
      newsapi_language: en
      newsapi_search_in: title,description,content
      newsapi_sources:
        - associated-press
      symbol_map:
        FED: Federal Reserve
    - name: gdelt_news
      provider: gdelt
      priority: 40
      data_types: [news]
      symbol_map:
        FED: Federal Reserve
```

Config fields:

- `newsapi_base_url`: NewsAPI endpoint. Defaults to
  `https://newsapi.org/v2/everything`.
- `newsapi_api_key_env`: environment variable containing the API key. Defaults
  to `NEWSAPI_API_KEY`. The key is sent as the `X-Api-Key` header and is not
  placed in the query string.
- `newsapi_page_size`: max articles requested from NewsAPI. Defaults to `25`
  and is capped at `100`.
- `newsapi_sort_by`: NewsAPI sort mode. Defaults to `publishedAt`; allowed
  values are `publishedAt`, `relevancy`, and `popularity`.
- `newsapi_language`: optional NewsAPI language filter such as `en`.
- `newsapi_search_in`: optional field selector such as
  `title,description,content`.
- `newsapi_domains`, `newsapi_exclude_domains`, `newsapi_sources`: optional
  provider-side filters.
- `symbol_map`: maps Maestro request symbols to NewsAPI `q` strings. When
  omitted, the request symbol is used as the query.
- `timeout_seconds`: maximum time for the NewsAPI HTTP call. Missing API keys,
  timeout, transport, rate-limit, auth, server, and NewsAPI `status: error`
  responses are mapped to provider-unavailable so the router can try a fallback.
- `stale_after_seconds`: optional threshold for marking the latest article
  stale.
- `retry_max_attempts`, `retry_backoff_seconds`, `cache_ttl_seconds`, and
  `min_request_interval_seconds`: optional shared external-provider operations
  controls. Cache is process-local and not a durable research data store.

NewsAPI is for research/news discovery only. Strategy plugins continue to
request `news` through DataHub and should not call NewsAPI directly. It is not
an execution, broker validation, or reconciliation source. Empty article
results, missing article lists, malformed article fields, malformed JSON, and
invalid article dates fail loudly.

Live integration checks are optional and skipped by default:

```bash
MAESTRO_RUN_NEWSAPI_INTEGRATION=1 NEWSAPI_API_KEY=... uv run pytest tests/test_newsapi_integration.py
```

Normal `pytest -q` remains fake-client and fixture based, with no live network
calls.

### Rule-based Sentiment Provider

The first sentiment provider is intentionally network-free. It supports
`sentiment` requests by analyzing configured fixture/news text snippets with a
small rule-based analyzer:

```python
{
    "SPY": {
        "symbol": "SPY",
        "score": 0.75,
        "label": "positive",
        "source": "fixture_news",
        "provider": "sentiment",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "related_symbols": ["SPY"],
        "keywords": ["SPY"],
        "text_count": 2,
        "is_stale": False,
        "warnings": [],
    }
}
```

The provider does not call Reddit, X/Twitter, Discord, Telegram, paid sentiment
APIs, or any live community source. Normal tests use fixture strings and fake
analyzers.

Single-provider example:

```yaml
datahub:
  provider: sentiment
  sentiment_texts:
    - SPY posts strong gains as confidence improves.
    - SPY faces downside risk after weak guidance.
  symbol_map:
    SPY: SPY
  source_name: fixture_news
  stale_after_seconds: 86400
```

Multi-provider example:

```yaml
datahub:
  providers:
    - name: rule_sentiment
      provider: sentiment
      priority: 60
      data_types: [sentiment]
      sentiment_texts:
        - SPY posts strong gains as confidence improves.
        - Federal Reserve commentary raises slowdown risks.
      symbol_map:
        SPY: SPY
        FED: Federal Reserve,Fed
      source_name: fixture_news
      stale_after_seconds: 86400
```

Config fields:

- `sentiment_texts`: fixture/news text snippets to analyze. Empty input and
  non-string snippets fail loudly.
- `symbol_map`: optional request-symbol to comma-separated keyword mapping. The
  provider filters snippets whose text contains any configured keyword.
- `timeout_seconds`: passed through to the analyzer interface for future bounded
  implementations and fake-analyzer tests.
- `stale_after_seconds`: optional threshold for marking analyzer timestamps
  stale. The default rule-based analyzer emits generated-at timestamps, so it is
  fresh unless a test or future analyzer supplies an older timestamp.
- `source_name`: display source written into normalized payloads.

Malformed analyzer results fail loudly. Analyzer timeouts and availability
failures are normalized as provider-unavailable errors.

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
- Yahoo, FRED, RSS, GDELT, and NewsAPI are wrapped by a shared resilience layer
  that can provide bounded retries, linear retry backoff, process-local TTL
  caching, and per-provider request spacing.
- Retries are reserved for provider-unavailable failures. Provider adapters do
  not retry validation errors, unsupported symbols, or malformed responses.
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
  FRED, RSS news, GDELT, NewsAPI, rule-based sentiment, and future provider
  adapters.
- Normalization, freshness, timeout, retry, rate-limit, and error mapping should
  be tested without real network calls.
- Optional integration tests that contact real services should be explicitly
  marked, skipped by default, and isolated from normal `pytest -q` runs.
- Current skipped-by-default live-network checks cover Yahoo/yfinance, FRED,
  RSS, GDELT, and NewsAPI. Rule-based sentiment has no live integration test
  because it has no network source in v0.3.
- Fixtures should include successful payloads, unsupported symbols, stale
  responses, provider-unavailable responses, malformed provider data, and
  rate-limit cases.

Production live-approval hardening:

- Required `price` data for live approval should fail closed when stale, missing,
  outside the intended market session, or inconsistent with the proposal
  snapshot.
- The exact data snapshot used to generate a live approval proposal should be
  persisted or referenced so an operator can audit the price basis after the
  order.
- Market hours and holiday checks should be explicit for the configured venue.
  Daily OHLCV freshness alone is not enough for intraday live order decisions.
- Broker-side `broker_quote` checks may validate order prices immediately before
  execution, but they must not become a fallback strategy research feed.
- Provider retry and rate-limit behavior should be bounded and provider-specific;
  partial data must not be silently treated as fresh.

Current v0.8.2 live-approval hardening supports opt-in market-session checks via
execution config and opt-in broker quote validation from the latest KIS
read-only snapshot. DataHub provider fallback remains router-owned: tests cover
provider-unavailable fallback without letting broker quotes satisfy research
`price` requests.

Remaining real provider work:

- Implement real provider adapters for Reddit/X/Discord/Telegram community
  sentiment feeds and paid sentiment APIs.
- Defer crypto market data until stocks/ETFs are no longer the supported
  universe.
- Add provider-specific config fields only when each adapter needs them.
- Reuse the shared external-provider resilience wrapper where it fits, and add
  provider-specific retry or rate-limit handling only when an adapter needs a
  different policy.
- Add provider-specific schema normalization and fixture-backed tests.
- Keep cache behind DataHub. Current cache is per-process TTL only; do not add
  heavy database infrastructure without a concrete repeatability requirement.
