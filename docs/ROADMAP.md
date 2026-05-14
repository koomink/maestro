# Maestro Roadmap

## Roadmap Principles

- Maestro is not a strategy; it is the portfolio operating system.
- Virtuoso apps propose; Maestro validates, constructs, protects, executes, and records.
- Safety-first progression: mock -> paper -> DataHub providers -> approval-gated paper -> read-only broker -> approval-gated live trading.
- DataHub is a Maestro internal research/market data layer; broker adapters are
  Maestro internal account/execution/reconciliation adapters.
- Yahoo/yfinance, FRED, RSS feeds, KIS Open API, and Telegram Bot API are
  external systems reached only through Maestro adapters.
- Virtuoso apps are external strategy plugins/apps. They request data through
  Maestro DataHub and must not call data providers or broker APIs directly.
- Dashboard should remain read-only initially.
- Telegram should be used for approval and urgent notifications, not high-risk administration.
- Live auto-trading is explicitly deferred.

## v0.1.0 — Bootable Skeleton

Implemented scope:

- Python package structure
- Strategy SDK contract
- External plugin loading by entrypoint
- Sample static allocation strategy
- MockDataHub
- `TargetAllocationResult` only for the v0.1 baseline; current execution can
  normalize policy-backed `StrategySignalResult` plugins
- SignalValidator
- Simple PortfolioManager
- Simple RiskManager
- PaperExecutionEngine
- SQLite state store
- JSONL audit logger
- CLI `run-once`
- CLI `status`
- Basic tests and lint setup

Implemented foundations beyond the strict v0.1.0 core:

- CSVDataProvider
- Read-only Streamlit dashboard foundation
- Approval request/decision models
- Approval gate stub
- Telegram formatter/no-network notifier stub
- KIS read-only mock adapter
- CLI `kis-sync` and `kis-account`

Explicitly not implemented:

- No real Telegram Bot API integration yet
- No real KIS REST API integration yet
- No live trading
- No KIS order submission
- No production hardening yet

## v0.1.1 — Stabilization and Cleanup

Scope:

- Separate ID generators for run, order, and approval IDs
- `execution.engine` validation through a small execution engine factory
- Explicit missing price errors
- Strict Pydantic config validation using `extra="forbid"`
- README, config, and docs consistency cleanup
- TASKS.md cleanup
- Focused tests for stabilization behavior
- Better failure audit metadata, including error type, message, and traceback summary

This version should not introduce real integrations or large architecture rewrites.

## v0.2 — DataHub and Dashboard Foundation

Scope:

- Improve DataHub schema
- Clarify historical versus latest price data
- Stronger CSV provider behavior
- Establish DataHub as the required path for strategy market and research data
- Keep broker account/execution data separate from DataHub research data
- Data freshness checks
- Missing data policy
- Symbol registry or simple asset metadata
- Read-only dashboard improvements
- Portfolio snapshots view
- Strategy runs view
- Orders view
- Risk/approval status view
- Broker snapshot view
- SQLite read improvements for dashboard

Dashboard remains read-only. No live execution controls.

## v0.3 — External Research Data Providers

Scope:

- DataHub provider interface and provider routing
- Provider freshness and stale-data policy
- Cache/storage policy for provider responses
- Symbol registry and provider symbol mapping
- Data schemas for `price`, `ohlcv`, `macro`, `news`, `sentiment`, `fundamental`, and `broker_quote`
- Real external research API providers for the current stock/ETF universe:
  Yahoo Finance/yfinance-style `price` and `ohlcv`, FRED `macro`, and
  RSS/GDELT/NewsAPI `news`
- A network-free rule-based `sentiment` provider over configured fixture/news
  text
- Optional live-network provider smoke tests for Yahoo/yfinance, FRED, RSS,
  GDELT, and NewsAPI, skipped by default so normal tests remain fake-client and
  fixture based
- Reddit/X/Discord/Telegram/community sentiment APIs remain future provider
  options
- Crypto exchange market data is deferred because the current supported universe
  is stocks and ETFs only

KIS is not the primary research data source. KIS broker quotes may be used later as `broker_quote` reference data for execution validation or reconciliation.

## v0.4 — Telegram Approval in Paper Mode

Scope:

- Real Telegram Bot API integration
- Telegram approval channel abstraction
- Send order proposal messages
- Polling-based approve/reject button handling for the first MVP
- `run-once` blocks while waiting for an approval, rejection, or timeout
- Inline approve/reject buttons and callback handling after the polling MVP
- Webhook handling after the polling MVP
- Whitelisted user ID enforcement
- Approval timeout
- Duplicate-decision prevention
- Paper execution only after approval
- Fill/error notifications

Still no live trading in v0.4.

## v0.5 — KIS Read-only Broker Foundation

Scope:

- Real Korea Investment Securities read-only REST client
- OAuth token management via configured environment variable names
- Optional owner-only access token cache after `/oauth2/tokenP` issuance
- Broker-side quote/reference lookup for execution validation or reconciliation
- Domestic-stock balance inquiry
- Domestic-stock buying power inquiry
- Domestic-stock order/fill inquiry
- Domestic-stock unfilled order inquiry
- Broker account snapshot normalization
- Basic internal state versus latest broker snapshot reconciliation
- `live_readonly` mode hardening

Read-only only. No order submission, cancel, amend, buy, or sell callable path.
The implemented REST behavior is an explicit domestic adapter path. Overseas
stock/ETF endpoints, pagination/continuation, and full reconciliation remain
future work.

## v0.6 — KIS Live Approval Trading

Completed scope:

- KIS live order submission behind broker adapter
- Product/venue-aware universe model for canonical symbols, broker products,
  exchange codes, currencies, price ticks, and quantity steps
- Explicit KIS adapter split between `kis_domestic_stock` and
  `kis_overseas_stock`
- Telegram approval required
- Limit orders only
- Small notional limits
- Daily notional limits
- Broker order ID mapping
- Duplicate-order prevention
- Order status polling interface
- Persisted `live_order_status` system and audit events
- KIS daily/unfilled order inquiry status normalization
- Partial/full fill portfolio reconciliation using cumulative fill deltas
- `maestro reconcile-fills`
- Cancellation policy and guarded cancel interface
- Open and partial-fill remaining-quantity cancellation rules
- Cancel forbidden for filled, rejected, canceled, halted, and unknown states
- Post-order workflow orchestration through existing safety/status/fill/reconciliation services
- Persisted `live_order_workflow` summaries
- Bounded multi-poll order lifecycle loop
- Operator notification interface with fake-client coverage
- Telegram lifecycle/fill/error notification adapter through the existing Bot API boundary
- Persisted `live_order_lifecycle` summaries
- Live approval dependency factory with KIS clients when `kis.provider="kis"` and injectable fake clients for tests
- Product-level `run_once` wiring to live order lifecycle when `mode=live_approval`
- Safe-by-default live approval example config at
  `configs/live_approval.example.yaml`
- v0.6 live approval release checklist at
  `docs/live_approval_release_checklist.md`
- Reconciliation before and after orders
- Halt on unknown broker state
- Safe defaults keep live orders disabled unless explicitly configured

Live auto-trading remains deferred. This is `live_approval`, not `live_auto`.
KIS support covers US-listed stocks/ETFs through `kis_overseas_stock` and KRX
stocks/ETFs through `kis_domestic_stock`. Multi-product operator configs can
enable both broker products with independent KRW/USD currency sleeves.
Package metadata intentionally remains `0.1.1` until Maestro adopts an explicit
package release/versioning policy; v0.6 is the roadmap capability milestone.

## v0.7 — Production Hardening

Completed scope:

- Persistent global safety state for `active`, `paused`, `killed`, and `halted`
- CLI safety controls: `safety-status`, `pause`, `resume`, and `kill-switch`
- Explicit `clear-halt --reason` recovery path for halted state
- Safety gate in `MaestroOrchestrator.run_once()` before paper or live execution
- Live approval submission blocked before approval/lifecycle execution when
  safety state is paused, killed, or halted
- Safety transition, blocked execution, and paper warning events persisted to
  system events and audit logs
- Stale required DataHub data halts live approval and warns in paper mode
- Missing, stale, or failed broker reconciliation halts live approval when
  reconciliation is required
- Daily live notional and order count gates use persisted live order events
- Instrument-aware live order validation uses `universe.instruments`
- Daily loss limit config starts fail-closed until broker PnL normalization is
  available in the v0.8.x hardening path
- Unknown order status halt behavior

This milestone prepares Maestro for more serious operation but should still prefer safety over autonomy.
The dashboard remains read-only, Telegram resume/clear-halt/live enable/risk
change/direct trading controls remain deferred, and live auto-trading remains
out of scope.

## v0.7.1 — Real-data US Stock/ETF Paper Mode

Completed scope:

- Yahoo/yfinance paper config for US-listed stocks and ETFs
- USD base-currency paper universe
- AAPL, MSFT, VOO, QQQ, and SGOV example instruments
- DataHub symbol mapping examples for external provider symbols
- Freshness policy for real market data in paper mode
- Clear docs that paper execution is simulated inside Maestro
- `CASH_USD` handled as a cash reference price without a Yahoo call

This milestone proves the overseas stock/ETF research-data path without broker
execution. It should not call KIS for strategy research data.

## v0.7.2 — KIS Overseas Read-only Adapter

Completed scope:

- KIS overseas read-only REST adapter for verified account endpoints:
  `inquire-present-balance`, `inquire-balance`, `inquire-psamount`,
  `inquire-ccnl`, `inquire-nccs`, and overseas quote reference `price`
- USD cash and foreign-currency balance normalization
- Overseas positions normalization
- Overseas buying power normalization
- Overseas fills and unfilled orders normalization
- Broker reconciliation for canonical US stock/ETF symbols
- `configs/kis_overseas_readonly.example.yaml` with env var names only
- Fake-client and fixture tests only for normal test runs

No order submission, cancel, amend, buy, or sell callable path in this
milestone.

## v0.7.3 — Operational Closeout

Completed scope:

- `maestro health` CLI
- Structured Python logging
- Deployment guide
- VPS/systemd guide
- Backup/restore guide for SQLite state, audit logs, and local config
- Operator runbooks for halt recovery and broker reconciliation

Health is local by default and does not call live KIS endpoints. No dashboard
write controls and no Telegram resume, clear-halt, live enablement, direct
trading, or risk-change controls.

## v0.7.4 — Dashboard / Observability Refresh

Completed scope:

- Read-only dashboard panels for safety state, health summary, latest broker
  snapshot, latest reconciliation, recent halt/failure events, live order
  status/lifecycle events, fill reconciliation events, and daily live order
  count/notional usage
- Portfolio analytics panels for broker exposure, Maestro state exposure, and
  snapshot history, with local refresh and CSV download actions
- Dashboard labels clarified for stock/ETF and KIS domestic/overseas workflows
- Dashboard read model tests for observability tables

The dashboard remains read-only, does not call live KIS endpoints, and does not
add state-changing dashboard write controls, Telegram resume/clear-halt/live
enable/risk-change controls, live auto-trading, market orders, or
submit/cancel/amend paths.

## v0.8 — KIS Overseas Live Approval Beta

Completed scope:

- KIS overseas US stock/ETF limit-order submit adapter after endpoint paths,
  TR_IDs, exchange codes, and request fields were checked against Korea
  Investment Securities OpenAPI examples
- KIS overseas order status adapter using existing daily fill and unfilled order
  reads
- Fill tracking and reconciliation for US-listed stocks/ETFs through the
  existing live order lifecycle services
- Fake-client live approval end-to-end tests covering approval, timeout,
  terminal status, partial fill, rejected status, and unknown-status halt paths
- Health preflight checks for live approval safety configuration
- `maestro live-preflight --config ...` for scriptable release gates
- Live approval dry-run mode that records `live_order_dry_run` events without
  broker submission
- KIS real-response fixture redaction guidance
- KIS overseas cancel adapter behind `LiveOrderCancellationService` policy gates
- Approval-gated `run_once` only
- Safety gates from v0.7 remain mandatory

No `live_auto`, no market orders, and no direct buy/sell/cancel CLI. KIS
overseas cancel is available only through the cancellation service after
approval, latest safe status, and reconciliation checks.

## v0.8.1 — Real Account Promotion Path

Completed scope:

- Make the promotion path explicit: mock paper -> real-data paper -> KIS
  read-only -> live approval dry-run -> minimum-size live order -> limited
  repeated operation.
- Add operator-local configuration requirements for real KIS and Telegram
  network rehearsals. Example configs remain safe defaults and must not contain
  secrets or real account details.
- Add skipped-by-default live smoke procedures for KIS read-only account sync,
  KIS broker reconciliation, Telegram approval, live approval dry-run, and the
  first minimum-size approval-gated order.
- Add an explicit state-only broker snapshot adoption command for the first
  verified account baseline before reconciliation rehearsals.
- Require post-order status, fill reconciliation, broker reconciliation, audit
  review, and dashboard review before repeated operation.

Normal tests remain fake-client and fixture based. Real-network checks are
operator-triggered rehearsals only.

## v0.8.2 — Production DataHub Hardening

Completed scope:

- Treat DataHub as the research and market data boundary, not as a completed
  production market data system.
- Add live-approval fail-closed freshness policy for required `price` data.
- Persist or reference the exact data snapshot used to create an order proposal.
- Add market session, holiday, and timestamp checks before live approval
  execution.
- Add provider-specific retry, timeout, rate-limit, and fallback behavior where
  a provider needs it.
- Add broker quote validation for execution checks without letting broker quotes
  become strategy research data.

## v0.8.3 — Real Risk Engine

Completed scope:

- Extend risk controls beyond target weights: cash reserve, buying power,
  position size, symbol exposure, order count, per-symbol notional, and
  portfolio-level exposure.
- Add KIS overseas pre-submit buying-power validation using the exact live order
  limit price before calling the broker order endpoint.
- Normalize broker PnL enough to enforce `execution.daily_loss_limit` instead of
  failing closed when it is configured.
- Account for fees, settlement, pending orders, and manual broker activity where
  they affect live approval safety.
- Keep all live orders approval-gated and limit-order-only.

The broker risk gate is explicit and operator-enabled through
`execution.require_broker_risk_validation=true`; normal tests remain fake-client
based and do not call KIS or Telegram network endpoints.

## v0.8.4 — Live Order Recovery

Completed scope:

- Add recovery procedures for ambiguous submit results, process crashes after
  submit, status persistence gaps, KIS timeouts, and partial-fill mismatches.
- Query KIS overseas order status using the broker submission timestamp's US
  exchange-local date range to avoid Korea/US date-boundary misses.
- Reconstruct live order state from broker truth before another live approval
  order is allowed after an ambiguous failure.
- Harden idempotency around approval IDs, broker order IDs, duplicate keys, and
  persisted lifecycle events.
- Add fake-client recovery drills for ambiguous submit, incomplete lifecycle,
  recovery completion, duplicate broker IDs, duplicate keys, and idempotent
  lifecycle persistence.

Recovery remains operator-driven: Maestro records `live_order_recovery_required`
or `live_order_recovery_halt`, blocks further live approval orders, and resumes
only after read-only broker truth, reconciliation, fill reconciliation, and
`maestro recover-live-order --reason ...` record a recovery completion.

## v0.8.5 — Ops, Audit, and Monitoring Hardening

Completed scope:

- Add heartbeat and scheduled-run monitoring for operator deployments through
  `maestro heartbeat`, `maestro_heartbeat` events, `run_once_completed` events,
  and health checks governed by `execution.heartbeat_max_age_seconds` and
  `execution.scheduled_run_max_age_seconds`.
- Add Telegram error escalation for halt, failure, stale data, reconciliation
  failure, and missed heartbeat events through `maestro ops-alerts`.
- Add audit hash-chain integrity checks for JSONL audit events.
- Exercise backup/restore and halt-recovery runbooks with explicit recovery
  commands.
- Keep dashboard read-only and keep Telegram resume/clear-halt/live enable/risk
  change/direct trading controls deferred.

## v0.9 — Dynamic Universe & Virtuoso SDK Contract

Completed scope:

- Harden Maestro SDK contracts for external Virtuoso apps with SDK contract
  version checks and optional candidate request support.
- Add `CandidateInstrumentRequest` with `intended_use: research | tradable`.
- Separate research candidates from approved tradable universe entries.
- Add conservative `UniversePolicy`: US stock/ETF, USD, KIS overseas stock,
  NASD/NYSE/AMEX, max one new tradable symbol per run, operator approval
  required, broker tradability and DataHub freshness checks required.
- Add `InstrumentResolver` to normalize canonical symbols into asset metadata,
  venue, currency, broker product, exchange code, precision, and broker mapping.
- Add dynamic universe approval models for temporary or persistent tradable
  entries.
- Validate allocations against tradable/research boundaries.
- Add Virtuoso app packaging and SDK boundary guidance.
- Add strategy app data-boundary tests.

Virtuoso apps continue to propose only; Maestro owns data access, risk,
approval, execution, state, and audit.

v0.9 should start after the v0.8.x live-operations hardening path is clear
enough that expanding the tradable universe does not expand unmanaged operational
risk.

Static `allowed_symbols` configs remain valid for examples, tests, tutorials,
and conservative paper trading. They are not intended to define the final product
limit. Dynamic universe support must keep the same safety constraints: no
`live_auto`, no market orders, no direct broker calls from strategy apps, no
direct buy/sell/cancel CLI, and no dashboard write controls.

## v1.0 — Private Approval-gated Production Beta

Completed scope:

- Private operator beta for approval-gated overseas stock/ETF workflows after
  v0.8.x promotion, DataHub, risk, recovery, and operations hardening
- Read-only dashboard
- Telegram approval and notifications only
- Health checks, backup/restore, and deployment docs complete
- Broker reconciliation and halt recovery runbooks exercised
- `maestro beta-preflight --config ...` validates private-beta readiness before
  an operator treats a live approval config as beta-ready

This is not autonomous trading. Live auto-trading remains deferred.

## v1.1 — Personal Operator MVP

Completed scope:

- `maestro init-personal --output ...` creates an operator-local, secret-free
  live approval config scaffold outside source control.
- Generated configs default to dry-run enabled, live submission disabled, limit
  orders only, small notional caps, Telegram approval, and KIS overseas
  stock/ETF broker product.
- `maestro personal-check --config ...` reports staged readiness for local
  paper/config health, KIS read-only reconciliation, Telegram approval, live
  dry-run, and minimum-size approval-gated live order readiness.
- `maestro operator-evidence --config ... --output ...` records a read-only
  readiness evidence report for operator rehearsals.
- `personal-check` and `operator-evidence` are local and do not submit broker
  orders, send Telegram messages, or run strategies.
- `docs/personal_operator_mvp.md` documents the single-user operating loop,
  first minimum-size order procedure, and recovery boundary.

This milestone packages the existing private beta pieces for one operator. It
does not add `live_auto`, market orders, direct broker trading CLI commands,
dashboard write controls, or high-risk Telegram controls such as resume,
clear-halt, live enablement, direct trading, or risk changes.

## Post-v1.1 — Telegram Operator UI

Implemented scope:

- Read-only Telegram operator commands: `/help`, `/status`, `/health`,
  `/account`, `/portfolio`, `/apps`, `/orders`, and `/approvals`.
- Back read-only responses with Maestro SQLite state and the latest stored
  broker snapshot only; Telegram commands must not call KIS live network
  endpoints directly.
- Limited safety controls: `/pause` and `/kill_switch`, each requiring a
  whitelisted user, confirmation button, and persisted audit/system event.
- Keep `/resume`, `/clear-halt`, live enablement, dry-run disablement,
  broker sync/reconciliation triggers, direct buy/sell/cancel, and risk limit
  changes out of Telegram.

This milestone treats Telegram as a constrained mobile operator console, not a
general administration surface. Recovery remains CLI/runbook driven.

## Post-v1.0 — Structural Refactor R1-R5

Completed scope:

- Centralized system event names for new code through `SystemEventType` and an
  audited system-event helper.
- Physically split live-order models, ports, status polling, cancellation,
  workflow, lifecycle, fill reconciliation, and safety services while
  preserving existing `maestro.execution.live_orders` imports.
- Extracted live approval hardening gates from `MaestroOrchestrator` into
  `LiveExecutionGateService`.
- Split KIS REST code into transport, parser helpers, domestic/overseas
  read-only adapters, and domestic/overseas live-order adapters.
- Moved private beta preflight checks into `maestro.ops`.
- Moved live approval preflight finding logic out of the health service.
- Wired dynamic-universe candidate evaluation into `run_once` for strategies
  that declare `supports_dynamic_universe=true`.
- Moved DataHub price extraction and data-quality issue collection out of the
  orchestrator.
- Split config models into domain modules while keeping
  `maestro.config.models` backward compatible.
- Added health model and provider boundaries.

The refactor is intentionally compatibility-first. Existing CLI behavior, state
schemas, strategy SDK contracts, and documented public import paths remain
unchanged.

## Deferred / Explicitly Out of Scope for Now

- Fully autonomous live trading
- Market orders
- Direct or unguarded buy/sell/cancel CLI
- Strategy-specific logic inside Maestro core
- High-risk Telegram controls: resume, clear-halt, live enablement, dry-run
  disablement, risk changes, direct trading, and broker sync/reconciliation
  triggers
- Write-capable dashboard controls
- Optional KIS WebSocket
- Performance attribution
- SDK split into a separate package
- Multi-user SaaS deployment
- Complex portfolio optimization engine
- Derivatives/futures support
