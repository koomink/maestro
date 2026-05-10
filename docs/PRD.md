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
5. Receive a `TargetAllocationResult`.
6. Validate the result.
7. Build a final `PortfolioTarget`.
8. Apply simple risk constraints.
9. Generate paper orders.
10. Update in-memory/SQLite portfolio state.
11. Write JSONL audit logs.
12. Exit successfully.

### 6.2 v0.1 Explicit Decisions

- Execution mode: **paper only**.
- Strategy result type: **TargetAllocationResult only**.
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
- Streamlit read-only dashboard
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
- Performance attribution
- Strategy lifecycle management
- Enhanced dashboard
- Optional FastAPI read-only API
- Deployment via systemd or Docker Compose

### Phase 7: Limited Live Automation

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
- Future DataHub providers include Yahoo Finance/yfinance-style OHLCV, FRED macro, CSV/local, RSS/GDELT/News API, sentiment/community data, fundamental data, and crypto exchange market data.
- Supported and planned DataHub data types include `price`, `ohlcv`, `macro`, `news`, `sentiment`, and `fundamental`.
- Production strategies must not call external data APIs directly.
- DataHub is the primary research and strategy data source. Broker adapters may expose `broker_quote` reference data for execution validation or reconciliation, but broker quotes are not the primary research feed.

### 8.3 Portfolio Management

- v0.1 supports target allocation results only.
- PortfolioManager combines strategy target allocations using configured fixed strategy weights.
- Allocations should normalize to 1.0 or allocate residual to `CASH`.
- Portfolio target must be passed to RiskManager before execution.

### 8.4 Risk Management

- v0.1 must enforce:
  - No short positions
  - No leverage
  - Max single asset weight
  - Min cash weight
  - Allowed asset universe
  - Strategy weight limits
- RiskManager may modify target allocations, but all modifications must be logged.

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

### 8.7 Telegram Approval

Future requirement:

- Telegram will be used for approval and notifications.
- Telegram must whitelist allowed user IDs.
- Trading approval requests must include proposal ID, portfolio changes, risk result, estimated order list, and approval buttons.
- Approval must expire after a configured timeout.
- Rejected, expired, and approved proposals must be logged.

### 8.8 Dashboard

Future requirement:

- Dashboard must be read-only by default.
- Dashboard should display portfolio, PnL, drawdown, strategy status, orders, proposals, system health, and KIS/Telegram status.
- Dashboard must not expose secrets.
- Dashboard should be accessible through localhost or Tailscale/VPN rather than public internet.

### 8.9 KIS Integration

Future requirement:

- KIS integration must be isolated in a broker adapter.
- KIS adapter must manage auth, balances, positions, buying power, fills, reconciliation, errors, and rate limit handling.
- KIS current price lookup may be used as `broker_quote` reference data for execution validation or reconciliation.
- KIS must not be presented as Maestro's main research or strategy data source.
- The intended first production broker product is overseas stocks/ETFs through
  `kis_overseas_stock`; real KIS overseas read-only and live submit/status paths
  must not be claimed until implemented and verified.
- `live_readonly` must expose no callable order submission, cancel, amend, buy, or sell path.
- Live trading should initially be `live_approval`, not `live_auto`.
- Only limit orders should be allowed initially.
- All broker state should be reconciled against internal state.

## 9. Non-Functional Requirements

### 9.1 Safety

- New plugins default to non-live modes.
- Live trading must be gated.
- Unknown order status should halt new orders.
- Reconciliation mismatch should halt new live orders.
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
