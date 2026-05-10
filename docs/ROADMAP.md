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
- `TargetAllocationResult` only
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
  Yahoo Finance/yfinance-style `price` and `ohlcv`, FRED `macro`, and RSS
  `news`
- A network-free rule-based `sentiment` provider over configured fixture/news
  text
- Optional live-network provider smoke tests for Yahoo/yfinance, FRED, and RSS,
  skipped by default so normal tests remain fake-client and fixture based
- GDELT/News API and Reddit/X/Discord/Telegram/community sentiment APIs remain
  future provider options
- Crypto exchange market data is deferred because the current supported universe
  is stocks and ETFs only

KIS is not the primary research data source. KIS broker quotes may be used later as `broker_quote` reference data for execution validation or reconciliation.

## v0.4 — Telegram Approval in Paper Mode

Scope:

- Real Telegram Bot API integration
- Telegram approval channel abstraction
- Send order proposal messages
- Polling-based approve/reject command handling for the first MVP
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

Started scope:

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

Remaining scope:

- Real KIS overseas-stock submit/status/read-only adapters after endpoint paths,
  TR_IDs, exchange codes, and request/response fields are verified
- Real broker cancel adapter implementation after endpoint path, TR_IDs, and request fields are verified

Live auto-trading remains deferred. This is `live_approval`, not `live_auto`.
The intended first production target is US-listed stocks and ETFs through
`kis_overseas_stock`; domestic KIS behavior is isolated behind
`kis_domestic_stock` and is not the strategic default.
Package metadata intentionally remains `0.1.1` until Maestro adopts an explicit
package release/versioning policy; v0.6 is the roadmap capability milestone.

## v0.7 — Production Hardening

Started scope:

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
- Daily loss limit config fails closed until broker PnL normalization exists

Remaining scope:

- Unknown order status halt
- Improved Python logging
- Monitoring and health checks
- Optional KIS WebSocket
- Performance attribution

This milestone prepares Maestro for more serious operation but should still prefer safety over autonomy.
The dashboard remains read-only, Telegram admin controls remain deferred, and
live auto-trading remains out of scope.

## v0.7.1 — Real-data US Stock/ETF Paper Mode

Planned scope:

- Yahoo/yfinance paper config for US-listed stocks and ETFs
- USD base-currency paper universe
- AAPL, MSFT, VOO, QQQ, and SGOV-style example instruments
- DataHub symbol mapping examples for external provider symbols
- Freshness policy for real market data in paper mode
- Clear docs that paper execution is simulated inside Maestro

This milestone proves the overseas stock/ETF research-data path without broker
execution. It should not call KIS for strategy research data.

## v0.7.2 — KIS Overseas Read-only Adapter

Planned scope:

- KIS overseas read-only REST adapter after endpoint paths, TR_IDs, exchange
  codes, pagination, and response fields are verified
- USD cash and foreign-currency balance normalization
- Overseas positions normalization
- Overseas buying power normalization
- Overseas fills and unfilled orders normalization
- Broker reconciliation for canonical US stock/ETF symbols
- Fake-client and fixture tests only for normal test runs

No order submission, cancel, amend, buy, or sell callable path in this
milestone.

## v0.7.3 — Operational Closeout

Planned scope:

- `maestro health` CLI
- Structured Python logging
- Deployment guide
- VPS/systemd guide
- Backup/restore guide for SQLite state, audit logs, and local config
- Operator runbooks for halt recovery and broker reconciliation

No dashboard write controls and no Telegram admin controls.

## v0.8 — KIS Overseas Live Approval Beta

Planned scope:

- KIS overseas limit-order submit adapter after endpoint paths, TR_IDs, exchange
  codes, and request fields are verified
- KIS overseas order status adapter
- Fill tracking and reconciliation for US-listed stocks/ETFs
- Approval-gated `run_once` only
- Safety gates from v0.7 remain mandatory

No `live_auto`, no market orders, no direct buy/sell/cancel CLI, and no real KIS
cancel adapter unless separately verified and guarded.

## v0.9 — Virtuoso SDK/App Integration

Planned scope:

- Harden Maestro SDK contracts for external Virtuoso apps
- Versioned plugin/app compatibility checks
- Example Virtuoso app packaging guidance
- Strategy app data-boundary tests
- Documentation for external app installation and promotion from paper to live
  approval

Virtuoso apps continue to propose only; Maestro owns data access, risk,
approval, execution, state, and audit.

## v1.0 — Private Approval-gated Production Beta

Planned scope:

- Private operator beta for approval-gated overseas stock/ETF workflows
- Read-only dashboard
- Telegram approval and notifications only
- Health checks, backup/restore, and deployment docs complete
- Broker reconciliation and halt recovery runbooks exercised

This is not autonomous trading. Live auto-trading remains deferred.

## Deferred / Explicitly Out of Scope for Now

- Fully autonomous live trading
- Market orders
- Direct or unguarded buy/sell/cancel CLI
- Real KIS cancel until endpoint path, TR_IDs, request fields, and safety policy
  are verified
- Strategy-specific logic inside Maestro core
- High-risk admin controls through Telegram
- Write-capable dashboard controls
- SDK split into a separate package
- Multi-user SaaS deployment
- Complex portfolio optimization engine
- Derivatives/futures support
