# PRD: Maestro Portfolio Operating System

## 1. Product Summary

**Maestro** is a standalone, strategy-agnostic portfolio operating system for automated investment management. It is the core operating program within the broader **Symphony ecosystem**.

The conceptual model is:

```text
Symphony = ecosystem
Maestro = portfolio operating system
Virtuoso apps = external strategy plugins/apps
```

Maestro does not contain investment alpha logic. Instead, external strategy packages, called **Virtuoso apps**, connect to Maestro through a standardized plugin contract. Virtuoso apps generate investment proposals, while Maestro manages data access, strategy execution, portfolio construction, risk control, approval workflows, execution, state, monitoring, and audit logs.

## 2. Product Vision

Maestro should become a robust personal portfolio operating system that allows the user to develop, test, connect, monitor, and eventually deploy multiple independent investment strategy apps safely.

The long-term goal is not just to build an automated trading script, but to build a reusable investment infrastructure layer where strategies can be added, removed, tested, and promoted across research, paper trading, live approval, and eventually limited live automation modes.

## 3. Core Product Philosophy

```text
Virtuoso apps propose.
Maestro decides.
Maestro protects.
Maestro executes.
Maestro records.
```

Key principles:

1. **Strategy-agnostic core**: Maestro must not depend on the internal logic of any specific strategy.
2. **Contract-first plugin architecture**: Virtuoso apps communicate with Maestro only through stable SDK schemas and interfaces.
3. **Safe by default**: New strategies default to research or paper mode and cannot directly access capital.
4. **Data through Maestro DataHub**: Production strategy apps request market, macro, news, sentiment, fundamental, and other research data through Maestro DataHub rather than calling external APIs directly.
5. **Execution only by Maestro**: Strategy apps never place orders directly.
6. **Human-in-the-loop before automation**: Early live trading must require Telegram approval.
7. **Read-only dashboard**: Dashboard initially observes the ecosystem but cannot execute trades or change configuration.
8. **Audit everything**: All decisions, proposals, risk checks, orders, fills, errors, and state changes must be logged.

## 4. Target Users

Initial target user:

- A technically capable individual investor/developer building a personal automated portfolio management system.
- The user wants to experiment with multiple strategy packages, especially dynamic asset allocation, technical analysis, fundamental analysis, macro strategies, and later LLM/news-based strategies.
- The user plans to use Korea Investment Securities Open API for eventual live trading.
- The user wants Telegram-based approval and notifications, and a read-only web dashboard for monitoring.

Future target users may include:

- Quant hobbyists
- Portfolio automation researchers
- Developers building plugin-based strategy systems
- Personal trading infrastructure users

## 5. Product Scope

### 5.1 In Scope for Maestro

Maestro is responsible for:

- Configuration management
- Strategy plugin loading
- Strategy plugin permission/mode management
- Strategy execution orchestration
- Market and research data request handling through DataHub
- Strategy result validation
- Portfolio target construction
- Risk constraint enforcement
- Order proposal generation
- Telegram notification and approval workflow
- Paper execution
- Broker adapters for read-only account state and later live execution, including Korea Investment Securities
- State management
- Reconciliation between internal state and broker account state
- Audit logging
- Read-only dashboard/API
- System health monitoring
- CLI commands

### 5.2 Out of Scope for Maestro

Maestro should not implement:

- Specific alpha logic
- Strategy-specific indicators or valuation models
- Strategy-specific branching by `strategy_id`
- Direct strategy access to broker execution
- Direct strategy access to capital
- Direct strategy calls to external research data APIs
- Direct strategy calls to broker APIs, including KIS
- Unrestricted web/dashboard trading controls in early versions
- Live auto-trading in the MVP

## 6. MVP Definition: Maestro v0.1

Maestro v0.1 is a bootable skeleton of the portfolio OS.

### 6.1 v0.1 Success Criteria

A user can run:

```bash
maestro run-once --config configs/paper.yaml
```

And Maestro will:

1. Load YAML configuration.
2. Load one external sample strategy plugin from `examples/sample_static_allocation`.
3. Use `MockDataHub` to return mock data.
4. Execute the strategy plugin.
5. Receive a `TargetAllocationResult`, or normalize a policy-backed
   `StrategySignalResult` into one.
6. Validate the result.
7. Build a final `PortfolioTarget`.
8. Apply simple risk constraints.
9. Generate paper orders.
10. Update in-memory/SQLite portfolio state.
11. Write JSONL audit logs.
12. Exit successfully.

### 6.2 v0.1 Explicit Decisions

- Execution mode: **paper only**.
- Strategy result type: **TargetAllocationResult only** for the v0.1 baseline.
  Current execution also accepts `StrategySignalResult` when strategy config
  supplies an explicit `signal_to_allocation` policy.
- Data provider: **MockDataHub only**.
- State storage: **SQLite + JSONL audit log**.
- Sample strategy: **inside `examples/` but structured as an independent installable plugin package**.
- Dashboard: not required in v0.1, but state/log structure should be dashboard-ready.
- Telegram: not required in v0.1.
- Korea Investment Securities Open API: not required in v0.1.

## 7. Product Roadmap

### Phase 0: Maestro v0.1 Core Skeleton

- Independent Python project
- Pydantic schemas
- YAML config loader
- Plugin SDK
- Plugin loader
- MockDataHub
- One example external plugin
- Orchestrator `run_once()`
- Target allocation result validation
- SimplePortfolioManager
- SimpleRiskManager
- PaperExecutionEngine
- SQLite state store
- JSONL audit logger
- CLI command
- Basic unit/integration tests

### Phase 1: Data and Dashboard Foundation

- CSVDataProvider
- Simple historical data loading
- FastAPI/React read-only dashboard
- Portfolio overview
- Recent strategy runs
- Recent paper orders
- System status page

### Phase 2: External Research Data Providers

- DataHub provider interface and routing
- Yahoo Finance/yfinance-style OHLCV provider
- FRED macro provider
- CSV/local data provider hardening
- RSS/GDELT/News API provider options
- Sentiment/community data provider options
- Crypto exchange market data provider options
- Data freshness, cache/storage, and symbol registry improvements

### Phase 3: Telegram Notifications and Approval Flow

- Telegram bot integration
- Cycle summary notifications
- Order proposal messages
- Inline approval/rejection buttons
- Whitelisted Telegram user IDs
- Approval timeout
- Approved paper execution
- Approval audit logs

### Phase 4: Korea Investment Securities Read-only Broker Adapter

- KIS OAuth access token management
- App key and secret from environment variables only
- Optional owner-only access token cache
- Broker-side quote/reference lookup for execution validation and reconciliation
- Balance inquiry
- Position normalization
- Buying power inquiry
- Daily order/fill inquiry
- Unfilled order inquiry
- `live_readonly` mode
- Basic account reconciliation against latest broker snapshot
- No KIS order submission path in this phase

### Phase 5: Live Approval Trading

- Limit-order-only live trading
- Telegram approval required
- Small order limits
- Daily notional limits
- KIS live order submission
- Order status polling
- Partial fill handling
- Execution result Telegram notification
- Internal state vs broker reconciliation

### Phase 6: Production Hardening

- KIS WebSocket support
- Real-time fill notification
- Advanced RiskManager
- Kill switch
- Drawdown limits
- Broker PnL normalization for risk checks
- Strategy lifecycle management
- Enhanced dashboard
- Full daemon/API/WebSocket dashboard surface
- Deployment via systemd or Docker Compose

### Phase 7: Practical Live Operations Hardening

- Real account promotion path from mock paper to real-data paper, KIS read-only,
  live dry-run, first minimum-size live order, and limited repeated operation
- Production DataHub hardening for freshness, proposal snapshots, market
  sessions, provider failures, and broker quote validation
- Operator-enabled broker risk controls for buying power, post-order cash,
  PnL-based daily loss limit, pending orders, fees, settlement, and unreconciled
  broker activity
- Live order recovery for ambiguous submit results, process crashes, status
  persistence gaps, partial fills, broker truth replay, and explicit recovery
  completion before another live approval order
- Heartbeat monitoring, Telegram error escalation, audit integrity, and
  backup/restore drills

### Phase 8: Dynamic Universe and Virtuoso SDK Hardening

- Strategy-proposed candidate instruments through `CandidateInstrumentRequest`
- Research versus tradable universe separation
- Conservative universe policies may allow US stock/ETF through
  `kis_overseas_stock` and KR stock/ETF through `kis_domestic_stock`.
  Multi-product operator configs rebalance KRW and USD currency sleeves
  independently; Maestro does not auto-convert FX or move value across sleeves.
- Instrument resolver for canonical metadata and broker mappings
- Versioned strategy/app compatibility checks

### Phase 9: KIS Performance Tracking and Analytics Dashboard

- Broker-backed account equity, PnL, return, and drawdown tracking from KIS
  account snapshots and Maestro reconciliation state
- Strategy-level return tracking using Maestro-owned proposal, order, fill, and
  strategy-run lineage rather than strategy-specific code branches
- Currency-sleeve return tracking for KRW and USD portfolios, with explicit FX
  conversion policy before showing a base-currency total return
- KRW as the default reporting base currency, with a read-only dashboard toggle
  that can display total portfolio performance in KRW or USD when FX data is
  fresh
- Read-only dashboard graphs for account returns, strategy returns,
  currency-sleeve returns, total portfolio returns, drawdown, and reconciliation
  freshness
- FX-adjusted views must show FX source, rate, timestamp, and missing/stale
  status; FX conversion must not affect execution, buying power, or risk gates
- CSV export for performance views without dashboard write controls

Current implementation covers account, currency-sleeve, total-portfolio, and
Virtuoso app performance read models from persisted broker snapshots/events,
strategy book snapshots, and explicit Telegram-attributed strategy cash-flow
events, plus dashboard charts and CSV export. Dedicated persisted performance
tables, richer lot-level strategy attribution, and the full Virtuoso backtest
runner remain future work.

### Phase 10: Limited Live Automation

- Very small auto-approval rules
- Larger orders remain Telegram-approved
- Automated daily/weekly rebalancing with risk gates
- Full kill switch and monitoring requirements

## 8. Functional Requirements

### 8.1 Plugin System

- Maestro must load strategy plugins using an entrypoint string from config.
- Strategy plugins must implement the Maestro SDK interface.
- Plugins must provide a manifest.
- Plugins must build data requests.
- Plugins must return validated strategy results.
- Plugin imports should be strategy-agnostic.

### 8.2 DataHub

- DataHub must process `DataRequest` objects.
- v0.1 DataHub returns deterministic mock data.
- Current implementation supports mock and CSV-style DataHub foundations.
- DataHub providers include Yahoo Finance/yfinance-style OHLCV, FRED macro, CSV/local, RSS/GDELT/NewsAPI, rule-based sentiment, and fundamental data. Future provider options include community sentiment data and crypto exchange market data.
- Supported and planned DataHub data types include `price`, `ohlcv`, `macro`, `news`, `sentiment`, and `fundamental`.
- Production strategies must not call external data APIs directly.
- DataHub is the primary research and strategy data source. Broker adapters may expose `broker_quote` reference data for execution validation or reconciliation, but broker quotes are not the primary research feed.

### 8.3 Portfolio Management

- v0.1 supports target allocation results only.
- PortfolioManager combines strategy target allocations using configured fixed strategy weights.
- Allocations should normalize to 1.0 or allocate residual to `CASH`.
- Portfolio target must be passed to RiskManager before execution.
- Static allowed-symbol configs remain valid for examples, tests, tutorials, and
  conservative paper trading, but they are not the intended product ceiling.
- The production design separates a broad research universe from a stricter
  tradable universe. Research inputs may include symbols, macro series, or
  keywords used only for analysis. Tradable symbols must pass Maestro validation
  before allocation or execution.
- Virtuoso apps may propose candidate symbols through the SDK, but Maestro must
  validate them against `UniversePolicy`, resolve metadata through an
  `InstrumentResolver`, check DataHub availability/freshness, and verify broker
  tradability when required.

### 8.4 Risk Management

- v0.1 must enforce:
  - No short positions
  - No leverage
  - Allowed asset universe
  - Strategy weight limits
- RiskManager rejects invalid targets before execution.
- Dynamic-universe checks reject unknown, unresolved, untradable, and
  research-only symbols in `TargetAllocationResult`.
- Virtuoso apps must not directly approve tradability, call broker APIs, submit
  orders, or bypass Maestro safety gates.

### 8.5 Execution

- v0.1 supports paper execution only.
- ExecutionEngine converts current state and approved target into paper orders.
- Paper orders are assumed filled at mock/reference prices.
- Live execution is not allowed in v0.1.

### 8.6 State and Audit

- State must be stored in SQLite.
- Audit events must be written to JSONL.
- Events must include cycle ID, timestamps, component, event type, details, and severity.
- Dashboard-readiness should be considered from v0.1.

### 8.7 Telegram Approval and Operator UI

Requirements:

- Telegram will be used for approval and notifications.
- Telegram must whitelist allowed user IDs.
- Trading approval requests must include proposal ID, portfolio changes, risk result, estimated order list, and approval buttons.
- Approval must expire after a configured timeout.
- Rejected, expired, and approved proposals must be logged.
- Telegram operator commands may expose read-only state and the limited
  `/pause` and `/kill_switch` safety controls.
- Telegram must not expose live enablement, dry-run disablement, broker sync,
  direct trading, recovery, or risk-limit changes.

### 8.8 Dashboard

Current and future requirement:

- Dashboard must be read-only by default.
- Dashboard should display portfolio, account-level PnL/return, strategy-level
  PnL/return, currency-sleeve PnL/return, total portfolio return, drawdown,
  strategy status, orders, proposals, system health, and KIS/Telegram status.
- Dashboard should default total performance views to KRW and allow a read-only
  USD display toggle for converted reporting.
- Performance charts must label stale or unreconciled broker data instead of
  presenting it as fresh broker truth.
- Performance charts must label missing or stale FX data and must not compute a
  converted total return from stale FX.
- Dashboard rendering must use persisted snapshots/read models and must not call
  KIS or FX endpoints directly.
- Virtuoso app performance should use TWR as the primary strategy return, show
  net PnL, cumulative cash flow, current value, drawdown, and MWR/IRR, and use
  explicit Telegram-approved `strategy_cash_flow` events as the app-level
  funding source of truth.
- Dashboard must not expose secrets.
- Dashboard should be accessible through localhost or Tailscale/VPN rather than public internet.

### 8.9 KIS Integration

Future requirement:

- KIS integration must be isolated in a broker adapter.
- KIS adapter must manage auth, balances, positions, buying power, fills, reconciliation, errors, and rate limit handling.
- KIS current price lookup may be used as `broker_quote` reference data for execution validation or reconciliation.
- KIS must not be presented as Maestro's main research or strategy data source.
- The intended first production broker product is overseas stocks/ETFs through
  `kis_overseas_stock`; real KIS overseas read-only and approval-gated US
  stock/ETF limit-order submit/status paths must not be claimed until
  implemented and verified.
- `live_readonly` must expose no callable order submission, cancel, amend, buy, or sell path.
- Live trading should initially be `live_approval`, not `live_auto`.
- Only limit orders should be allowed initially.
- All broker state should be reconciled against internal state.
- The target multi-account operator workflow should separate broker observation,
  strategy signal generation, and approval-gated execution. `live_readonly`
  remains pure broker read-only. Strategy signal generation should persist an
  immutable `signal_run_id`, and approval should execute that saved signal
  without re-running strategy apps.
- Strategy-level operator controls should decide whether a Virtuoso app appears
  in read-only operator views, participates in signal generation, and produces
  disabled, dry-run, or armed approval candidates. Global execution posture must
  remain the ceiling: a strategy cannot submit live orders unless the global
  config is also armed.
- Development-stage strategies should have a brokerless `dev_sandbox` account
  option for signal and approval UX rehearsal without touching KIS mock, KIS
  real, or Toss broker APIs.

## 9. Non-Functional Requirements

### 9.1 Safety

- New plugins default to non-live modes.
- Live trading must be gated.
- Unknown order status should halt new orders.
- Reconciliation mismatch should halt new live orders.
- Mixed strategy order posture within the same account should be rejected so one
  account is not asked to handle dry-run and armed orders in the same Symphony
  run.
- Secrets must not be logged.

### 9.2 Maintainability

- Code should be modular.
- Plugin contract should be stable.
- Strategy-specific logic must not leak into Maestro core.
- Public plugin API should live under `maestro.sdk`.

### 9.3 Observability

- All cycles must be auditable.
- System events must be visible through logs and later dashboard.
- Errors should include component and context.

### 9.4 Extensibility

- Data providers should be swappable.
- Execution engines should be swappable.
- Broker adapters should be swappable.
- Plugins should be installable as separate packages.

### 9.5 Security

- API keys and tokens must be loaded from environment variables or secret stores.
- Dashboard is read-only by default.
- Dashboard access should be restricted.
- Telegram approval must whitelist users.
- KIS withdrawal or unnecessary permissions should not be used.

## 10. Key Product Risks

1. Maestro becoming a large unstructured monolith.
2. Strategy-specific logic leaking into Maestro.
3. Broker execution errors causing duplicate orders.
4. Internal state diverging from broker state.
5. Overbuilding before the core pipeline works.
6. Dashboard or Telegram creating a security surface.
7. Live trading before sufficient paper and read-only validation.

## 11. Success Metrics

### v0.1

- `maestro run-once` completes end-to-end.
- At least one external example plugin loads through entrypoint config.
- Strategy output is validated.
- Paper portfolio state updates correctly.
- SQLite state and JSONL audit logs are written.
- Basic tests pass.

### Pre-Live

- 30+ successful paper cycles.
- Telegram approval flow tested with paper execution.
- KIS overseas read-only reconciliation works after the adapter is implemented.
- Dashboard displays portfolio and system status.
- Kill switch behavior tested.

### Live Approval

- User receives order proposal via Telegram.
- User approval triggers KIS overseas live order only after the overseas submit
  and status adapters are implemented and verified.
- Fill results are reconciled and logged.
- No duplicate orders under retry/error conditions.
- Live order size remains within configured risk limits.

### Repeated Real-account Operation

- Operator-local config has passed mock paper, real-data paper, KIS read-only,
  Telegram approval, live dry-run, and first minimum-size live order gates.
- Required DataHub prices are fresh, auditable, and valid for the market session.
- Broker reconciliation, fill reconciliation, and dashboard state match broker
  truth after each live approval order.
- Account, strategy, currency-sleeve, and total portfolio performance views are
  computed from reconciled broker snapshots and persisted Maestro events, with
  stale/unreconciled periods clearly labeled.
- Buying power, post-order cash, pending orders, and daily loss limits are
  enforced before submission.
- Ambiguous broker submit, process crash, timeout, and manual broker
  intervention procedures are documented and rehearsed.
- Heartbeat, alerting, audit integrity, backup, and halt recovery are exercised.
