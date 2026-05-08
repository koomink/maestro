# TASKS

## Completed Milestone: v0.2 DataHub and Dashboard Foundation

### DataHub

- [x] Add v0.2 DataHub schema models:
  - `PricePoint`
  - `OHLCVBar`
  - `SymbolData`
- [x] Clarify latest price versus historical bars
- [x] Update MockDataHub to emit v0.2-compatible payloads
- [x] Update CSVDataProvider to emit v0.2-compatible payloads
- [x] Preserve backward compatibility where practical for current orchestrator price extraction
- [x] Parse CSV timestamps into datetime objects
- [x] Validate OHLCV rows:
  - open/high/low/close > 0
  - volume >= 0
  - high >= low
  - high >= open and high >= close
  - low <= open and low <= close
- [x] Define missing data behavior clearly
- [x] Add basic freshness or stale data fields to `SymbolData`
- [x] Add tests for CSV schema validation
- [x] Add tests for missing symbols and invalid OHLCV rows

### DataHub Planning

- [x] Define a provider interface that can support future `price`, `ohlcv`, `macro`, `news`, `sentiment`, `fundamental`, and `broker_quote` payloads
- [x] Define provider routing rules for symbol, asset type, data type, timeframe, and run mode
- [x] Define freshness and stale-data policies per data type
- [x] Define cache/storage requirements for provider responses without overbuilding a database in v0.2
- [x] Define a symbol registry and provider symbol mapping plan
- [x] Define schema compatibility rules so strategy plugins request data through Maestro DataHub only
- [x] Document that `broker_quote` is broker-side reference data for execution validation or reconciliation, not the primary strategy research feed

### Symbol Metadata

- [x] Add a simple `SymbolMetadata` model
- [x] Include fields such as:
  - symbol
  - asset_type
  - currency
  - tradable
  - quantity_step
  - min_order_quantity
  - min_order_notional
- [x] Keep metadata lightweight and in-memory for v0.2
- [x] Do not build a full asset registry database yet

### State / Persistence

- [x] Add a `risk_decisions` SQLite table
- [x] Add `save_risk_decision` method
- [x] Add `list_risk_decisions` method
- [x] Persist RiskDecision after every risk check
- [x] Add SQLite connection timeout
- [x] Add PRAGMA `busy_timeout`
- [x] Add PRAGMA `journal_mode=WAL` where appropriate
- [x] Preserve existing tables and behavior
- [x] Add tests for risk decision persistence
- [x] Add tests confirming existing state methods still work

### Dashboard Read Models

- [x] Add dashboard read model helpers
- [x] Keep StateStore focused on persistence
- [x] Put display transformations in `dashboard/read_models.py`
- [x] Add overview read model
- [x] Add portfolio table read model
- [x] Add strategy runs table read model
- [x] Add orders table read model
- [x] Add approvals table read model
- [x] Add risk decisions table read model
- [x] Add broker snapshots table read model
- [x] Add system events table read model

### Dashboard UI

- [x] Improve the Streamlit dashboard while keeping it read-only
- [x] Add overview metrics:
  - cash
  - positions count
  - strategy runs count
  - orders count
  - approvals count
  - broker snapshots count
  - latest run time if available
- [x] Add portfolio section with readable table
- [x] Add strategy runs section
- [x] Add orders section
- [x] Add approvals section
- [x] Add risk decisions section
- [x] Add broker snapshots section
- [x] Add system events or recent errors section
- [x] Do not add buttons that execute orders
- [x] Do not add controls to change risk limits
- [x] Do not add live mode activation
- [x] Do not add strategy enable/disable controls

### Orchestration

- [x] Persist RiskDecision after risk check
- [x] Keep current run-once behavior intact
- [x] Optionally split run_once into small private methods only where it improves readability
- [x] Do not create a large workflow engine
- [x] Do not change strategy plugin contracts unless needed for DataHub compatibility

### Documentation

- [x] Update README if needed
- [x] Update ROADMAP.md only if v0.2 scope needs clarification
- [x] Document the v0.2 DataHub payload shape
- [x] Document that dashboard is read-only
- [x] Document that real integrations remain deferred

### Verification

- [x] `ruff check .`
- [x] `ruff format --check .`
- [x] `pytest -q`
- [x] `maestro run-once --config configs/paper.yaml`
- [x] `maestro run-once --config configs/csv_paper.yaml`
- [x] `maestro status --config configs/paper.yaml`

## Completed Milestone: v0.3 External Research Data Providers

### Provider Scaffold

- [x] Add lightweight DataHub provider registry
- [x] Add lightweight DataHub request router
- [x] Add DataHub errors for unsupported data type, no provider, provider unavailable, and stale data
- [x] Keep existing `mock` and `csv` providers working through the router
- [x] Add tests for routing, no-provider errors, normalization, stale/fresh metadata, and backward compatibility
- [x] Add lightweight multi-provider config support for current local providers
- [x] Add deterministic provider priority and source preference rules
- [x] Add tests for provider priority and config backward compatibility
- [x] Document v0.3 provider config shape for Yahoo/yfinance, FRED, RSS news, and rule-based sentiment
- [x] Defer crypto provider work because the current universe is stocks and ETFs only

### Real Provider Planning and Implementation

- [x] Plan Yahoo Finance/yfinance-style OHLCV provider integration
- [x] Plan FRED macro provider integration
- [x] Plan RSS news provider integration
- [x] Plan lightweight rule-based sentiment provider integration
- [x] Define provider configuration shape
- [x] Define provider secrets handling
- [x] Define provider unavailable behavior
- [x] Define provider error, timeout, retry, and rate-limit behavior
- [x] Define fake-client and fixture testing policy for future external providers
- [x] Define tests for provider routing, freshness, and schema normalization
- [x] Implement Yahoo Finance/yfinance-style price and OHLCV provider
- [x] Implement FRED macro provider
- [x] Implement RSS news provider
- [x] Implement lightweight rule-based sentiment provider
- [x] Add Yahoo/yfinance-style timeout and provider-unavailable handling
- [x] Add Yahoo/yfinance-style malformed-response handling
- [x] Add fixture-backed tests for Yahoo/yfinance-style provider success, missing symbol, malformed payload, stale/fresh metadata, and router integration
- [x] Add `yfinance` as an optional dependency extra
- [x] Document Yahoo/yfinance install and config usage
- [x] Add skipped-by-default live yfinance integration-test support
- [x] Add FRED API key environment-variable config without logging secret values
- [x] Add FRED timeout and provider-unavailable handling
- [x] Add FRED malformed-response handling
- [x] Add fixture-backed tests for FRED success, missing series, malformed payload, stale/fresh metadata, missing API key, and router integration
- [x] Add skipped-by-default live FRED integration-test support
- [x] Add RSS timeout and provider-unavailable handling
- [x] Add RSS malformed-feed and empty-feed handling
- [x] Add fixture-backed tests for RSS success, empty feed, malformed feed, stale/fresh metadata, config validation, and router integration
- [x] Add skipped-by-default live RSS integration-test support
- [x] Add rule-based sentiment provider unavailable and malformed-result handling
- [x] Add fixture-backed tests for sentiment positive, negative, neutral, empty input, malformed input, stale/fresh metadata, config validation, and router integration
- [x] Confirm Yahoo/yfinance, FRED, and RSS can make live network calls when configured, with skipped-by-default integration tests
- [x] Keep normal tests fake-client and fixture based, with no live network dependency
- [x] Move GDELT/News API, community sentiment APIs, paid sentiment APIs, and crypto provider work to future milestones

## Future Milestones

### Future DataHub Provider Work

- [ ] Plan GDELT/News API provider integration as future news providers
- [ ] Plan Reddit/X/Discord/Telegram/community sentiment API integrations as future sentiment providers
- [ ] Plan paid sentiment API integrations if a concrete provider is selected
- [ ] Revisit crypto exchange market data provider planning only after stocks/ETFs scope expands
- [ ] Add provider-specific secrets validation without logging secret values for remaining providers that require credentials
- [ ] Add provider-specific retry and rate-limit handling where future providers need it
- [ ] Add fixture-backed tests for each future real provider adapter

### v0.4 Telegram Approval

- [x] Plan real Telegram Bot API integration boundary
- [x] Decide polling versus webhook for the first Telegram implementation
- [x] Add minimal Telegram Bot API polling client behind an interface
- [x] Add bot token environment-variable config without storing or logging token values
- [x] Add allowed Telegram user and chat ID config
- [x] Send order proposal messages with approve/reject commands
- [x] Receive approve/reject decisions through polling
- [x] Document that `run-once` blocks while polling for approval, rejection, or timeout
- [x] Persist approval decisions and reject duplicate persisted decisions
- [x] Keep normal tests fake-client based with no Telegram network calls
- [x] Define paper-only approval behavior before live trading work begins
- [ ] Add inline approve/reject button callback handling after polling MVP
- [ ] Add webhook handling after polling MVP
- [ ] Add fill/error Telegram notifications

### v0.5 KIS Read-only Broker Integration

- [x] Plan real KIS read-only REST client boundary
- [x] Plan OAuth token management and token persistence
- [x] Plan balance, position, buying power, order/fill, and unfilled order reads
- [x] Plan broker account snapshot normalization
- [x] Add basic internal state versus latest broker snapshot reconciliation
- [x] Treat KIS current price lookup as `broker_quote` reference data for execution validation or reconciliation only
- [x] Keep strategy research data routed through Maestro DataHub, not KIS
- [x] Keep normal tests fake-client based with no KIS network calls
- [x] Expose no callable KIS order submission path in v0.5
- [x] Document domestic-stock read-only-first scope and future overseas/pagination/symbol-map/reconciliation work

### v0.6 KIS Live Approval Trading

- [x] Plan KIS live order submission behind broker adapter
- [x] Require Telegram approval before live order submission
- [x] Define limit-order-only behavior and small notional limits
- [x] Add daily notional limits and duplicate-order prevention
- [x] Add broker order ID mapping and order status model expansion
- [x] Add KIS domestic-stock limit-order client skeleton behind the safety interface
- [x] Keep normal tests fake-client based with no KIS network calls
- [x] Add live order status polling interface
- [x] Persist live order status snapshots as `live_order_status` system/audit events
- [x] Normalize KIS daily/unfilled order inquiry statuses for open, partial fill, filled, rejected, canceled, and unknown states
- [x] Implement partial/full fill portfolio reconciliation from live order status snapshots
- [x] Avoid double-counting duplicate live order status snapshots
- [x] Add `maestro reconcile-fills`
- [x] Define halt-on-unknown-state behavior
- [ ] Define cancellation policy

## Completed / Historical Notes

- v0.1.0 delivered the bootable paper-mode skeleton described in [ROADMAP.md](ROADMAP.md).
- v0.1.1 stabilized IDs, config validation, missing price behavior, execution engine validation, failure audit metadata, and docs consistency.
- v0.2 delivered DataHub schema clarity, stronger CSV/mock providers, risk decision persistence, dashboard read models, and read-only dashboard improvements.

For version-level planning and future milestones, see [ROADMAP.md](ROADMAP.md).
