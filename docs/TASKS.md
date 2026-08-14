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
- [x] `maestro run-once --config configs/paper.yaml`
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
- [x] Add shared bounded retry, process-local TTL cache, and request-spacing controls for Yahoo/yfinance, FRED, and RSS
- [x] Keep normal tests fake-client and fixture based, with no live network dependency
- [x] Plan GDELT news provider integration
- [x] Implement GDELT news provider for strategy research/news discovery
- [x] Add fixture-backed tests for GDELT success, symbol mapping, lookback limiting, malformed payloads, stale/fresh metadata, config validation, and router fallback
- [x] Add skipped-by-default live GDELT integration-test support
- [x] Plan NewsAPI provider integration as an opt-in API-key news provider
- [x] Implement NewsAPI provider for strategy research/news discovery
- [x] Add fixture-backed tests for NewsAPI success, API-key header usage, symbol mapping, lookback/page-size limiting, optional filters, malformed payloads, stale/fresh metadata, config validation, and router fallback
- [x] Add skipped-by-default live NewsAPI integration-test support
- [x] Move community sentiment APIs, paid sentiment APIs, and crypto provider work to future milestones

## Future Milestones

### Future DataHub Provider Work

- [ ] Plan Reddit/X/Discord/Telegram/community sentiment API integrations as future sentiment providers
- [ ] Plan paid sentiment API integrations if a concrete provider is selected
- [ ] Revisit crypto exchange market data provider planning only after stocks/ETFs scope expands
- [ ] Add provider-specific secrets validation without logging secret values for remaining providers that require credentials
- [ ] Add provider-specific retry and rate-limit handling where future providers need behavior beyond the shared external-provider wrapper
- [ ] Add fixture-backed tests for each future real provider adapter

### v0.4 Telegram Approval

- [x] Plan real Telegram Bot API integration boundary
- [x] Decide polling versus webhook for the first Telegram implementation
- [x] Add minimal Telegram Bot API polling client behind an interface
- [x] Add bot token environment-variable config without storing or logging token values
- [x] Add allowed Telegram user and chat ID config with shared Maestro env fallback
- [x] Send order proposal messages with approve/reject buttons
- [x] Receive approve/reject button decisions through polling
- [x] Document that `run-once` blocks while polling for approval, rejection, or timeout
- [x] Persist approval decisions and reject duplicate persisted decisions
- [x] Keep normal tests fake-client based with no Telegram network calls
- [x] Define paper-only approval behavior before live trading work begins
- [x] Add inline approve/reject button callback handling after polling MVP
- [ ] Add webhook handling after polling MVP
- [x] Add fill/error Telegram notifications

### v0.5 KIS Read-only Broker Foundation

- [x] Plan real KIS read-only REST client boundary
- [x] Plan OAuth token management and token persistence
- [x] Plan balance, position, buying power, order/fill, and unfilled order reads
- [x] Plan broker account snapshot normalization
- [x] Add basic internal state versus latest broker snapshot reconciliation
- [x] Treat KIS current price lookup as `broker_quote` reference data for execution validation or reconciliation only
- [x] Keep strategy research data routed through Maestro DataHub, not KIS
- [x] Keep normal tests fake-client based with no KIS network calls
- [x] Expose no callable KIS order submission path in v0.5
- [x] Document that domestic-stock REST behavior is an adapter path, not the
  strategic core trading universe

### v0.6 KIS Live Approval Trading

- [x] Plan KIS live order submission behind broker adapter
- [x] Require Telegram approval before live order submission
- [x] Define limit-order-only behavior and small notional limits
- [x] Add daily notional limits and duplicate-order prevention
- [x] Add broker order ID mapping and order status model expansion
- [x] Add product/venue-aware universe model for canonical symbols, broker products, exchange codes, currencies, and precision rules
- [x] Split KIS REST adapters into explicit `kis_domestic_stock` and `kis_overseas_stock` paths
- [x] Make `kis_overseas_stock` the default KIS broker product and live approval example target
- [x] Keep KIS overseas-stock real submit/status adapters fail-closed until v0.8 endpoint paths, TR_IDs, exchange codes, and fields are verified
- [x] Keep existing KIS domestic-stock limit-order behavior isolated behind the `kis_domestic_stock` adapter path
- [x] Keep normal tests fake-client based with no KIS network calls
- [x] Add live order status polling interface
- [x] Persist live order status snapshots as `live_order_status` system/audit events
- [x] Normalize KIS daily/unfilled order inquiry statuses for open, partial fill, filled, rejected, canceled, and unknown states
- [x] Implement partial/full fill portfolio reconciliation from live order status snapshots
- [x] Avoid double-counting duplicate live order status snapshots
- [x] Add `maestro reconcile-fills`
- [x] Define halt-on-unknown-state behavior
- [x] Define cancellation policy
- [x] Add cancellation request/result/client interface only
- [x] Require Telegram approval and latest safe status for cancellation
- [x] Allow open order and remaining partial-fill cancellation only
- [x] Forbid cancel for filled/rejected/canceled/halted/unknown states
- [x] Persist cancel results as system/audit events
- [x] Keep direct cancel CLI out of scope
- [x] Add post-order workflow orchestration service
- [x] Persist `live_order_workflow` summaries
- [x] Stop workflow on halted submit/status before polling/fill reconciliation
- [x] Run fill reconciliation and optional broker reconciliation after status polling
- [x] Keep workflow tests fake-client based with no KIS network calls
- [x] Add bounded multi-poll lifecycle service
- [x] Add safe order status polling config defaults
- [x] Add operator notification abstraction with fake-client tests
- [x] Add Telegram lifecycle/fill/error notification adapter behind the existing Bot API boundary
- [x] Persist `live_order_lifecycle` summaries
- [x] Do not auto-cancel when max polls are reached
- [x] Add live approval dependency factory with injectable fake clients
- [x] Wire `MaestroOrchestrator.run_once()` to the bounded live order lifecycle in `live_approval` mode
- [x] Add safe-by-default `configs/live_approval.yaml`
- [x] Add `docs/live_approval_release_checklist.md`
- [x] Document why package metadata remains `0.1.1`
- [x] Implement KIS overseas cancel adapter after endpoint path, TR_IDs, and request fields are verified
- [x] Implement real KIS overseas-stock read-only adapter after endpoint paths, TR_IDs, exchange codes, and response fields are verified
- [x] Implement real KIS overseas-stock submit/status adapters after endpoint paths, TR_IDs, exchange codes, and response fields are verified

### v0.7 Production Hardening

- [x] Add persistent safety state model for active, paused, killed, and halted states
- [x] Persist safety transitions as SQLite system events and audit events
- [x] Add `maestro safety-status --config ...`
- [x] Add `maestro pause --config ... --reason "..."`
- [x] Add `maestro resume --config ... --reason "..."`
- [x] Add `maestro kill-switch --config ... --reason "..."`
- [x] Add explicit `maestro clear-halt --config ... --reason "..."`
- [x] Gate `MaestroOrchestrator.run_once()` before live approval submission
- [x] Record live execution skips caused by paused, killed, or halted state
- [x] Keep paper mode runnable while recording a safety warning event
- [x] Prevent casual kill-switch reset through `resume`
- [x] Add explicit halted-state recovery procedure
- [x] Add stale data halt for live approval and stale-data warning for paper
- [x] Add missing, stale, and failed reconciliation halt
- [x] Auto-record broker reconciliation inside KIS-backed `live_approval run-once`
- [x] Add logical multi-account broker routing with `strategy.account_id`,
      KIS real/paper account configs, and Toss fail-closed placeholder support
      before approval/order gates when reconciliation is required
- [x] Add Toss OpenAPI account/read-only backbone using the official OpenAPI
      fixture
- [x] Add Toss submit, status, modification, and cancellation with
      approval-gated integer DAY limit orders as the initial armed policy
- [x] Add daily loss limit config skeleton that fails closed until broker PnL normalization exists
- [x] Add daily order count and notional limits
- [x] Add instrument-aware live order validation for quantity step, price tick, minimums, currency, and broker product
- [x] Add improved Python logging
- [x] Add audit log rotation or hash chain
- [x] Add monitoring and health checks
- [x] Add live approval preflight checks to health output
- [x] Add enabled strategy plugin import checks to health/preflight output
- [x] Add `maestro live-preflight --config ...` with nonzero exit on failed live approval preflight
- [x] Add deployment, VPS/systemd, and backup/restore guides
- [x] Keep dashboard write controls deferred
- [x] Keep Telegram pause, kill-switch, resume, clear-halt, live enablement,
      direct trading, and risk-change controls deferred
- [x] Keep live auto-trading out of scope

### v0.7.1 Real-data US Stock/ETF Paper Mode

- [x] Add Yahoo/yfinance paper config for US-listed stocks and ETFs
- [x] Add USD universe example with AAPL, MSFT, VOO, QQQ, and SGOV-style instruments
- [x] Document DataHub symbol mapping for external provider symbols
- [x] Validate paper-mode path with fixture Yahoo data in normal tests
- [x] Confirm `PaperExecutionEngine` remains simulated execution inside Maestro
- [x] Keep KIS out of strategy research data
- [x] Keep normal tests fake-client/fixture based

### v0.7.2 KIS Overseas Read-only Adapter

- [x] Verify KIS overseas read-only endpoint paths, TR_IDs, exchange codes, pagination, and response fields from project references
- [x] Normalize USD cash and foreign-currency balances
- [x] Normalize overseas stock/ETF positions to canonical symbols
- [x] Normalize overseas buying power
- [x] Normalize overseas fills and unfilled orders
- [x] Add broker reconciliation for US-listed stock/ETF canonical symbols
- [x] Add KR+US read-only KIS fixture coverage
  template with env var names only
- [x] Keep read-only mode free of order submission, cancel, amend, buy, and sell paths
- [x] Keep normal tests fake-client/fixture based with no KIS network calls

### v0.7.3 Operational Closeout

- [x] Add `maestro health` CLI
- [x] Add structured Python logging
- [x] Add deployment guide
- [x] Add VPS/systemd guide
- [x] Add backup/restore guide for SQLite state, audit logs, and local config
- [x] Add operator runbooks for halt recovery and broker reconciliation
- [x] Keep health local by default with no live KIS network calls
- [x] Keep dashboard write controls deferred
- [x] Keep Telegram resume, clear-halt, live enablement, direct trading, and
      risk-change controls deferred

### v0.7.4 Dashboard / Observability Refresh

- [x] Keep dashboard read-only
- [x] Add safety state dashboard read model and panel
- [x] Add health summary dashboard read model and panel
- [x] Add latest broker snapshot dashboard read model and panel
- [x] Add latest reconciliation status dashboard read model and panel
- [x] Add recent halt/failure event read model and panel
- [x] Add live order status/lifecycle event read model and panel
- [x] Add fill reconciliation event read model and panel
- [x] Add daily live order count/notional usage read model and panel
- [x] Add operator summary and attention items for live-approval monitoring
- [x] Add broker exposure, Maestro state exposure, and snapshot history panels
- [x] Add local refresh and CSV download actions without broker calls or writes
- [x] Improve labels for stock/ETF and KIS domestic/overseas workflows
- [x] Add dashboard read model tests
- [x] Keep dashboard write controls deferred
- [x] Keep live auto-trading, market orders, submit/cancel/amend work, Telegram
      admin controls, and live KIS calls from dashboard/tests deferred

### v0.8 KIS Overseas Live Approval Beta

- [x] Verify KIS overseas limit-order submit endpoint paths, TR_IDs, exchange codes, and request fields from Korea Investment Securities OpenAPI examples
- [x] Implement KIS overseas limit-order submit adapter behind live approval safety gates
- [x] Implement KIS overseas order status adapter
- [x] Track fills for US-listed stocks/ETFs through existing live status snapshots
- [x] Reconcile fills and broker state after overseas live approval orders through existing lifecycle services
- [x] Add fake-client live approval end-to-end tests for approved, rejected, expired, filled, partial-fill, rejected-status, and unknown-status paths
- [x] Add live approval dry-run mode that records broker submit payloads without calling KIS
- [x] Add fail-closed tests for KIS error responses, missing broker order IDs, and malformed order status numeric fields
- [x] Implement KIS overseas cancel adapter behind `LiveOrderCancellationService` policy gates
- [x] Document KIS real-response fixture redaction rules
- [x] Keep approval-gated `run_once` as the only live submit path
- [x] Keep live auto-trading deferred
- [x] Keep market orders deferred
- [x] Keep direct buy/sell/cancel CLI deferred

### v0.8.1 Real Account Promotion Path

- [x] Document mock paper -> real-data paper -> KIS read-only -> live dry-run ->
      minimum-size live order -> limited repeated operation as the required
      promotion path
- [x] Add an operator-local live approval config checklist with real account,
      Telegram, state, audit, and token-cache paths outside source control
- [x] Add skipped-by-default KIS read-only live smoke procedure
- [x] Add explicit `maestro adopt-broker-snapshot` operator baseline command
      before live account reconciliation rehearsals
- [x] Add skipped-by-default Telegram approval live smoke procedure
- [x] Add skipped-by-default live approval dry-run rehearsal procedure
- [x] Add first minimum-size approval-gated order checklist
- [x] Require post-order status polling, fill reconciliation, broker
      reconciliation, audit review, and dashboard review before repeated
      operation
- [x] Keep normal `pytest -q` fake-client/fixture based with no KIS or Telegram
      network calls

### v0.8.2 Production DataHub Hardening

- [x] Document DataHub as a research/market data boundary, not a completed
      production market data system
- [x] Add live-approval fail-closed behavior for stale or missing required
      `price` data
- [x] Persist or reference the data snapshot used for each live approval proposal
- [x] Add market session and holiday checks for live approval execution
- [x] Add provider-specific retry, timeout, rate-limit, and fallback tests where
      providers need them
- [x] Add broker quote validation for execution checks without using KIS as the
      strategy research data source

### v0.8.3 Real Risk Engine

- [x] Add buying-power and post-order cash checks before live approval submission
- [x] Add KIS overseas pre-submit buying-power check using the exact live order
      limit price
- [x] Add order-count, notional-cap, and pending-order risk checks
- [x] Normalize broker PnL enough to enforce
      `execution.live_order_limits.daily_loss_limit_by_currency`
- [x] Account for fees, settlement, pending orders, and manual broker activity in
      live approval safety checks
- [x] Add fake-client tests for risk fail-closed behavior

### v0.8.4 Live Order Recovery

- [x] Add recovery procedure for ambiguous KIS submit results and transport
      timeouts
- [x] Query KIS overseas order status over the broker submission
      exchange-local date range to avoid Korea/US date-boundary misses
- [x] Add recovery procedure for process crash after broker submit and before
      lifecycle persistence completes
- [x] Reconstruct live order state from broker truth before allowing another live
      approval order after ambiguous failure
- [x] Harden idempotency for approval IDs, broker order IDs, duplicate keys, and
      persisted lifecycle events
- [x] Add fake-client and redacted-fixture recovery drills

### v0.8.5 Ops, Audit, and Monitoring Hardening

- [x] Add heartbeat and scheduled-run monitoring for operator deployments
- [x] Add Telegram error escalation for halt, failure, stale data,
      reconciliation failure, and missed heartbeat events
- [x] Add audit log rotation or hash-chain integrity checks
- [x] Exercise backup/restore and halt-recovery runbooks
- [x] Keep dashboard write controls, Telegram high-risk admin controls,
      `live_auto`, market orders, and direct broker CLI deferred

### v0.9 Dynamic Universe & Virtuoso SDK Contract

- [x] Harden Maestro SDK contracts for external Virtuoso apps
- [x] Add `CandidateInstrumentRequest` planning for Virtuoso-proposed candidate
      symbols and data needs
- [x] Add `intended_use: research | tradable` planning to candidate/data
      requests
- [x] Document and implement the research universe versus tradable universe
      distinction
- [x] Add `UniversePolicy` for allowed asset types, regions, currencies, broker
      products, denied symbols, denied asset tags, max new symbols per run, and
      required approval/data/broker checks
- [x] Add `InstrumentResolver` for canonical symbol metadata, venue, currency,
      broker product, exchange code, precision, and broker mapping
- [x] Add broker tradability checks before candidate symbols can become tradable
- [x] Add dynamic symbol approval for temporary or persistent tradable universe
      entries
- [x] Reject allocations to research-only, unknown, unresolved, or untradable
      symbols
- [x] Extend `StrategyManifest` planning with `supports_dynamic_universe`,
      `max_candidate_symbols`, `allowed_data_types`, and
      `supported_asset_types`
- [x] Add versioned plugin/app compatibility checks
- [x] Document external Virtuoso app packaging
- [x] Add strategy app data-boundary tests
- [x] Verify v0.8.x promotion, DataHub, risk, recovery, and ops hardening are
      sufficient before expanding the tradable universe
- [x] Keep Virtuoso apps proposal-only; Maestro owns data, risk, approval, execution, state, and audit
- [x] Keep static `allowed_symbols` configs valid for examples, tests,
      tutorials, and conservative paper configs
- [x] Keep live auto-trading, market orders, direct broker calls from strategy
      apps, and dashboard write controls deferred

### Future Maestro Config Composition

- [x] Trigger this work once two or three external Virtuoso apps require
      operator rehearsal or production-like dry-runs at the same time
- [ ] Keep current full YAML configs supported for explicit audits, examples,
      tests, and single-app operator workflows
- [x] Add config composition for Maestro operator profiles and Virtuoso app
      fragments, starting with the Tranquillo v1 fragment
- [x] Define app fragments for strategy entrypoint, strategy config defaults,
      DataHub symbol-map hints, allowed sleeve membership, and recommendations
- [x] Keep broker, approval, execution, risk, state, audit, and promotion gates
      owned by Maestro/operator profile or local overlay, not by app fragments
- [x] Render composed configs into a normal validated `MaestroConfig`
- [x] Ensure composed configs work with `profile-diff`, `profile-validate`,
      health checks, audit review, and existing CLI commands through normal
      config loading
- [x] Add tests for merge precedence, forbidden app-fragment keys, identity
      fingerprinting, and recommendation drift checks
- [ ] Add a first-class operator-local overlay file format if repeated private
      overlays appear outside normal config copies
- [ ] Add deterministic rendered YAML output and secret redaction if a
      `render-config` CLI becomes necessary for audits

### v1.0 Private Approval-gated Production Beta

- [x] Add `maestro beta-preflight --config ...` for private beta readiness
- [x] Require live approval mode, real KIS provider, Telegram approval, broker
      snapshot, passing reconciliation, audit integrity, heartbeat monitoring,
      scheduled-run monitoring, market session checks, broker quote validation,
      broker risk validation, and daily loss limit before beta readiness
- [x] Keep beta approval-gated only; no `live_auto`, market orders, direct broker
      CLI, dashboard write controls, or high-risk Telegram resume, clear-halt,
      live enablement, direct trading, or risk-change controls
- [x] Add tests for beta preflight pass/fail behavior
- [x] Add provider-derived LLM API-key checks to live approval preflight for
      LLM-backed Virtuoso apps such as Fugue

### Post-v1.0 Structural Refactor R1-R5

- [x] Add centralized `SystemEventType` names and an audited system-event helper
      for new code paths
- [x] Physically split live-order models, ports, status polling, cancellation,
      workflow, lifecycle, fill reconciliation, and safety services while
      preserving the existing `maestro.execution.live_orders` compatibility path
- [x] Extract live approval hardening gates from `MaestroOrchestrator` into
      `LiveExecutionGateService`
- [x] Move KIS urllib transport into a dedicated KIS transport module
- [x] Physically split KIS REST parser helpers plus domestic/overseas read-only
      and live-order adapters
- [x] Move private-beta preflight logic out of CLI handlers into `maestro.ops`
- [x] Move live approval preflight findings out of the monolithic health service
- [x] Wire dynamic-universe candidate evaluation into `run_once` for strategies
      that declare `supports_dynamic_universe=true`
- [x] Move DataHub price extraction and data-quality issue collection out of the
      orchestrator
- [x] Split config models into domain modules while keeping
      `maestro.config.models` backward compatible
- [x] Add health models and health-check provider structure
- [x] Keep static universe configs and existing public imports backward
      compatible
- [x] Verify with `pytest -q`, `ruff check .`, and `ruff format --check .`

### v1.1 Personal Operator MVP

- [x] Add `maestro init-personal --output ...` for safe operator-local config generation
- [x] Keep generated config secret-free and source-control agnostic
- [x] Default generated config to live approval, `execution.order_posture=dry_run`,
      limit orders only, and small live notional caps
- [x] Add `maestro personal-check --config ...` staged readiness output for paper, KIS read-only, Telegram approval, live dry-run, and minimum-size live order gates
- [x] Add `maestro operator-evidence --config ... --output ...` read-only
      readiness evidence report for operator rehearsals
- [x] Keep `personal-check` local and free of broker submit or Telegram send side effects
- [x] Document the personal operator workflow in `docs/personal_operator_mvp.md`
- [x] Keep live auto-trading, market orders, direct broker trading CLI, dashboard
      write controls, and Telegram resume, clear-halt, live enablement, direct
      trading, and risk-change controls deferred
- [x] Add KIS domestic+overseas multi-product operator config with KRW/USD
      currency sleeves and symbol-level broker-product routing
- [x] Scope the Tranquillo domestic ETF KIS promotion to mock-investment
      `paper_trading=true` broker submit and document the four scheduled-cycle
      pilot

### Hybrid Operator Hardening

- [x] Define `mode`, `profile`, and `operator config` terminology in docs
- [x] Document that Telegram and dashboard are long-running operator services
      inside the current hybrid operator architecture
- [x] Add StateStore writer lock for shared SQLite state before overlapping
      scheduled writer jobs are allowed
- [x] Add config fingerprint and canonical config path recording to
      heartbeat, audit, and state metadata
- [x] Add drift check for opening the same state DB with a different
      config identity
- [x] Add `MAESTRO_CONFIG` fallback so operator services can share one config
      path without repeating `--config` in every command
- [x] Surface config path, fingerprint, state path, and audit path in
      Telegram/dashboard operator status
- [x] Update systemd docs to use one operator config across services and timers
- [x] Document broker snapshot adoption as the live baseline transition instead
      of promoting paper SQLite state

### Telegram Operator UI
- [x] Add explicit opt-in Telegram funding requests for buy-only contribution sleeves when cash is below `min_monthly_budget`, with separate order approval after funding confirmation

- [x] Add Telegram command router with whitelist enforcement
- [x] Add `/help`
- [x] Add `/status`
- [x] Add `/health`
- [x] Add `/account` from latest broker snapshots across enabled read-only accounts
- [x] Add `/portfolio` with broker refresh/adoption across enabled read-only accounts
- [x] Split read-only unknown broker position adoption from execution safety so
      Telegram/CLI portfolio display can include already-held unknown symbols
      while target allocation and live orders still fail closed
- [x] Add `/apps` from configured strategies and latest strategy runs
- [x] Add `/orders` and `/approvals`
- [x] Add `/signal` latest signal package view
- [x] Add proposal-only `/signal_<strategy>` Telegram commands backed by the
      signal config and Dashboard-visible signal packages
- [x] Add `/pause` with confirmation button
- [x] Add `/kill_switch` with confirmation button
- [x] Persist Telegram command audit/system events
- [x] Keep KIS sync, reconcile, resume, clear-halt, direct trading, live enable,
      dry-run disablement, and risk changes out of Telegram

### Planned Milestone: KIS Performance Tracking & Analytics Dashboard

- [x] Add account performance v1 read model from persisted broker snapshots
- [x] Add read-only dashboard Performance tab for account value, return,
      drawdown, reconciliation labels, charts, and CSV export
- [x] Add dashboard read model tests for account performance calculations
- [x] Add currency-sleeve performance read model and dashboard table/chart that
      keeps KRW/USD returns separate
- [x] Add total portfolio performance read model that marks mixed-currency rows
      as missing FX instead of force-converting values
- [x] Define persisted performance snapshot/read-model schema for account,
      strategy, currency sleeve, and total portfolio returns
- [x] Use persisted broker snapshots as the broker-truth source for account equity,
      cash, positions, realized/unrealized PnL, fees, and settlement fields when
      available
- [x] Preserve KRW and USD return series separately before adding any
      base-currency converted portfolio return
- [x] Add explicit missing-FX behavior and reserved FX source/timestamp fields
      for cross-currency portfolio views
- [x] Define the FX source snapshot/config shape for reporting, including
      source name, rate, as-of timestamp, stale threshold, and supported pairs
- [x] Add ExchangeRate-API backed FX refresh service, CLI command, dashboard
      refresh wiring, and `fx_rate_snapshot` persistence for USD/KRW reporting
- [x] Add a one-hour FX provider request throttle and `maestro fx-refresh
      --force` override for ExchangeRate-API free-plan budget control
- [x] Add KRW as the default dashboard performance display currency
- [x] Add a read-only KRW/USD display-currency toggle for total portfolio
      performance charts and tables
- [x] Split local sleeve return, FX effect, and converted total return in
      performance read models
- [x] Ensure FX conversion is used only for dashboard/reporting and never for
      order generation, buying power, reconciliation cash gates, or risk cash
      checks
- [x] Add missing/stale FX dashboard labels and disable converted total return
      calculations when FX is unavailable or stale
- [x] Attribute strategy returns from persisted proposal, order, fill, and
      strategy-run lineage without adding strategy-specific code paths
- [x] Define the documented fallback attribution rule for shared holdings until
      lot-level strategy accounting exists
- [x] Add account-level daily/cumulative return and drawdown calculations
- [x] Preserve mixed-currency cash with `cash_by_currency` and logical account IDs
- [x] Add audited account performance baselines and account cash-flow events
- [x] Chain account/portfolio TWR from the adopted baseline with TWR-index drawdown
- [x] Preserve account tracking start/end membership without rewriting old NAV
- [x] Add time-period dashboard performance queries and actual coverage metadata
- [ ] Add strategy-level daily/cumulative return and drawdown calculations
- [x] Add currency-sleeve daily/cumulative return and drawdown calculations
- [x] Add total portfolio daily/cumulative return and drawdown calculations
- [x] Label performance rows with persisted reconciliation pass/fail/missing
      state
- [x] Add a versioned dashboard `performance_snapshot` contract with latest
      values, series, FX quality, and persisted lineage metadata
- [x] Define system-event required-field contracts and expose missing-field
      status in dashboard system event read models
- [x] Add freshness-age/stale labels so old broker snapshots or old
      reconciliations are not presented as fresh truth
- [x] Add freshness policy tests and expose policy metadata for stale, missing,
      not-configured, and failed rows
- [x] Add read-only dashboard charts for account value/return/drawdown,
      currency-sleeve returns, total portfolio return/drawdown, and latest
      reconciliation status
- [ ] Add read-only dashboard charts for strategy-level returns after strategy
      attribution exists
- [x] Redesign dashboard information architecture around Symphony Map, Operator
      Cockpit, Investment Console, Virtuoso Apps, and Audit Trail
- [x] Make Symphony Map the dashboard landing view with persisted status nodes
      for proposal, validation/risk, execution/state, reconciliation, and
      operator attention
- [x] Replace top-level duplicate metrics with a System Verdict and Capital
      Summary that explain why the system is OK, warning, or blocked
- [x] Add reason rows for dashboard status cards so DANGER/FAIL/STALE states
      identify the underlying attention item, freshness row, health check,
      reconciliation issue, lifecycle event, or FX state
- [x] Standardize money rendering so every visible amount has a native or
      display currency and converted totals show FX source/rate/timestamp
- [x] Replace Streamlit with a FastAPI/React read-only dashboard while keeping
      approval controls, kill switch, config writes, and WebSocket/daemon APIs
      deferred until a daemon/API event model exists
- [x] Keep dashboard graphs backed by persisted state/read models only; do not
      call KIS live endpoints from dashboard rendering
- [x] Add CSV export for performance tables without adding dashboard write
      controls
- [x] Rework the FastAPI/React dashboard into a compact Command Center with
      user-question tabs, collapsed evidence tables/JSON, and DESIGN.md-aligned
      surfaces/typography
- [x] Add fixture/fake-client tests for account, currency-sleeve,
      total-portfolio, multi-currency, multi-strategy, and dashboard read model
      calculations
- [x] Add fixture/fake-client tests for strategy attribution and stale data
      labeling once implemented
- [x] Add Virtuoso app-level performance snapshots with TWR, net PnL, cumulative
      cash flow, current value, drawdown, MWR/IRR, and cash-flow markers
- [x] Add Telegram-approved `strategy_cash_flow` attribution for
      strategy-requested and voluntary account funding flows
- [x] Add focused Virtuoso app dashboard subtabs for Overview, Performance,
      Backtest, Orders, and Evidence
- [x] Document current operator usage and limitations in README/TRD

## Symphony Signal-to-Approval Workflow

- [x] Rename/replace operator profiles with `symphony_readonly`,
      `symphony_signal`, and `symphony_approval`
- [x] Keep `symphony_readonly` broker/account read-only with no strategy
      execution, no order generation, and no approval creation
- [x] Add an initial signal CLI/workflow that runs enabled Virtuoso apps and
      persists a signal package
- [x] Extend the signal CLI/workflow so it refreshes broker truth, runs enabled
      Virtuoso apps, and persists an immutable signal package
- [x] Add initial `signal_run_id` generation, storage, strategy results,
      target/risk preview, order preview, and no-op/action-required status
- [x] Add stricter DataHub evidence checks
- [x] Add signal expiry, config identity checks, and account mapping validation
- [x] Add shared `broker_accounts_path` support so Symphony phases use one broker account inventory.
- [x] Add shared `strategy_account_map_path` support so signal and approval use
      one account routing file
- [x] Extend the shared strategy mapping with `readonly`, `signal`, and
      strategy-level `order_posture` controls
- [x] Extend the shared strategy mapping with execution sleeves, per-sleeve
      `order_generation_mode`, target weights, and cash rebalance boundaries
- [x] Add account strategy/manual bucket targets and attribution snapshots so
      `toss_brokerage` can reserve manual capacity beside `crescendo_us`
- [x] Reconcile attribution after broker sync, require baseline adoption,
      attribute Maestro fills, and enforce account bucket capacity
- [x] Add a generic multi-account contribution allocator and apply Tranquillo v1
      across `kis_ps / tranquillo_ps` and `kis_isa / tranquillo_isa`
- [x] Apply the contribution fee buffer once, split oversized contribution
      orders at the per-order cap, and populate the 2026 KRX holiday schedule
      used by Tranquillo operator profiles
- [x] Add brokerless `dev_sandbox` routing for development strategies that need
      signal/approval UX rehearsal without KIS or Toss broker access
- [x] Reject mixed strategy order posture within one account in a Symphony run
- [x] Add broker snapshot references to signal packages
- [x] Add an initial approval CLI/workflow that requires `signal_run_id` and does
      not re-run Virtuoso strategies
- [x] Re-check data freshness, reconciliation, quote drift,
      safety state, live order recovery state, and order limits before approval
      or broker submit
- [x] Re-check broker snapshot ref freshness before approval
- [x] Include `signal_run_id` in approval payloads, order payloads, live order
      requests/results, lifecycle events, and audit logs
- [x] Include `signal_run_id` in reconciliation events
- [x] Include `signal_run_id` in dashboard read models
- [x] Add dashboard and Telegram views that show latest readonly state, latest
      signal package, no-op reason, and actionable `signal_run_id`
- [x] Add Dashboard global refresh for read-only account snapshot sync plus
      signal freshness classification, without running Virtuoso apps
- [x] Add Virtuoso sub-tabs with Overview and one tab per app
- [x] Add per-app Dashboard `Generate Signal` controls that run one selected
      strategy only and never approve or execute orders
- [x] Add tests for no-signal no-op, stale/expired signal rejection, config
      mismatch rejection, account mapping mismatch rejection, and approval from
      a saved signal package
- [x] Add systemd wiring and a locked signal-to-approval wrapper for
      `symphony_readonly`, `symphony_signal`, and conditional
      `symphony_approval`
- [x] Add an account-routed pre-approval buying-capacity gate that isolates only
      over-capacity orders and preserves the final broker pre-submit check
- [x] Align KIS domestic capacity lookup with limit-order `ORD_DVSN=00` so
      broker maximum quantity is not understated by a market-order ceiling
- [x] Add same-day `/retry_order` proposals with fresh capacity/live gates, new
      Telegram approval, linked ack events, and duplicate prevention
- [x] Submit approved multi-order batches before round-robin status polling,
      suppress unchanged open notifications, and persist batch summaries
- [x] Dispatch live Telegram approvals asynchronously through the always-on
      operator, with 10-minute expiry and 2/5/8-minute reminders
- [x] Generalize recovery to capacity, pre-broker failure, and expired approval
      candidates while keeping open/partial orders on `/modify`
- [x] Add Telegram recovery-review buttons for failure alerts and `/orders`,
      with original/current-maximum choices and an audited direct-quantity
      ForceReply flow that still requires a new approval
- [x] Add batch status counters and phase-duration telemetry without changing
      polling frequency
- [x] Add KIS domestic native revision support plus account recovery for legacy
      order IDs and refreshed `/orders` modification examples
- [x] Classify KIS application-level order errors as definitive rejections,
      preserve ambiguous transport failures as halted, and supersede recovered
      source contribution orders in monthly idempotency checks
- [x] Record audited run provenance for signal, approval, and `run_once` with
      deployment commit/source hash and prepared/runtime config fingerprints
- [x] Add an audited Telegram `/recovery` center with deduplicated halt alerts,
      guarded safety-halt clearing, Toss OPEN/CLOSED history resolution,
      broker-attestation fallback, and shared CLI recovery preflight
- [x] Separate Toss buying power from the account cash ledger, add an audited
      opening baseline and drift report, make cash-flow/fill updates idempotent,
      backfill Toss order history, and keep ledger cash separate from TWR
- [x] Make signal refresh ledger-read-only, couple Toss snapshots to successful
      OPEN/CLOSED history backfill, protect pre-baseline replay, and use ledger
      cash for account, currency-sleeve, and total confirmed performance
- [x] Detect stable unexplained Toss buying-power steps, request guarded Telegram
      cash-flow confirmation, and record operator-verified flows idempotently
- [x] Validate linked cash-flow cardinality and scope, record internal transfers
      atomically, and remove the fixed event-history limit from performance reads
- [x] Make Toss adoption/backfill migration-safe: recognize version-1 cash
      baselines, seed full cumulative order watermarks, apply later fill deltas,
      atomically persist adoption provenance, and add audited bookkeeping repair
- [x] Preserve live-order recovery until fill reconciliation succeeds, resolve
      cash-suspense incidents explicitly, and alert on fills that remain above
      the ledger watermark for 15 minutes

- [x] Run rebalances as sell-then-buy cohort phases per account and currency:
      hold buys until every sell fills, re-query broker buying power and shrink
      the approved buys to it, and on any incomplete sell cancel the remainder,
      skip the buys, and alert the operator
- [x] Check pre-approval and retry capacity against the order's own currency:
      have Toss answer from `buying_power_by_currency`, fail closed when the
      currency is missing or the adapter answers in another one, share the
      currency fallback with the live gate, and quote blocked maximums in whole
      tradable steps
- [x] Replace the stream of Telegram approval/order notifications with one card
      per approval that the poll sweep edits in place: state keyed by
      `(card_key, chat_id)` in a projection table, intent recorded before every
      send so a copy of unknown delivery is never resent, stage decided from
      event payloads on separate progress/attention axes, a read-only daily
      parent card for multi-group signal runs, a one-line no-action notice, and
      a plain-text fallback plus `telegram_ui` health degrade after three
      consecutive failures. Recipients are fixed at first delivery so widening
      the allowed-chat list resends nothing, and settled signal runs leave the
      sweep's scan through a terminal index. See
      [specs/2026-08-09-telegram-ux-redesign-design.md](superpowers/specs/2026-08-09-telegram-ux-redesign-design.md)
      stage 2; the legacy notification paths stay in parallel until stage 5
- [ ] Price rotation orders off the Toss order book (`/api/v1/orderbook`), walking
      to the level where cumulative volume covers the order and capping at
      `max_quote_deviation_pct`, to raise the sell fill rate. See
      [specs/2026-08-05-rotation-two-phase-execution-design.md](superpowers/specs/2026-08-05-rotation-two-phase-execution-design.md)

## Completed / Historical Notes

- v0.1.0 delivered the bootable paper-mode skeleton described in [ROADMAP.md](ROADMAP.md).
- v0.1.1 stabilized IDs, config validation, missing price behavior, execution engine validation, failure audit metadata, and docs consistency.
- v0.2 delivered DataHub schema clarity, stronger CSV/mock providers, risk decision persistence, dashboard read models, and read-only dashboard improvements.

For version-level planning and future milestones, see [ROADMAP.md](ROADMAP.md).
