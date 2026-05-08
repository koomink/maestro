# Maestro Roadmap

## Roadmap Principles

- Maestro is not a strategy; it is the portfolio operating system.
- Virtuoso apps propose; Maestro validates, constructs, protects, executes, and records.
- Safety-first progression: mock -> paper -> DataHub providers -> approval-gated paper -> read-only broker -> approval-gated live trading.
- DataHub is the research/market data layer; broker adapters are the account/execution layer.
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

## v0.5 — KIS Read-only Broker Integration

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
Overseas stock/ETF endpoints, pagination/continuation, canonical symbol mapping,
and full reconciliation remain future work.

## v0.6 — KIS Live Approval Trading

Started scope:

- KIS live order submission behind broker adapter
- Telegram approval required
- Limit orders only
- Small notional limits
- Daily notional limits
- Broker order ID mapping
- Duplicate-order prevention
- Order status polling interface
- Persisted `live_order_status` system and audit events
- KIS daily/unfilled order inquiry status normalization
- Reconciliation before and after orders
- Halt on unknown broker state
- Safe defaults keep live orders disabled unless explicitly configured

Remaining scope:

- Full partial fill portfolio reconciliation
- Cancellation policy
- Post-order reconciliation workflow

Live auto-trading remains deferred. This is `live_approval`, not `live_auto`.

## v0.7 — Production Hardening

Scope:

- Kill switch
- Pause/resume trading
- Stale data halt
- Reconciliation mismatch halt
- Unknown order status halt
- Daily loss limit
- Daily order count/notional limits
- Improved Python logging
- Audit log rotation or hash chain
- Monitoring and health checks
- Deployment guide
- VPS/systemd guide
- Backup/restore guide
- Optional KIS WebSocket
- Performance attribution

This milestone prepares Maestro for more serious operation but should still prefer safety over autonomy.

## Deferred / Explicitly Out of Scope for Now

- Fully autonomous live trading
- Strategy-specific logic inside Maestro core
- High-risk admin controls through Telegram
- Write-capable dashboard controls
- SDK split into a separate package
- Multi-user SaaS deployment
- Complex portfolio optimization engine
- Derivatives/futures support
