# TRD: Maestro Technical Requirements Document

## 1. Architecture Overview

Maestro is a standalone Python application that acts as a strategy-agnostic portfolio operating system.

The architecture separates:

```text
Virtuoso strategy plugin → Maestro SDK contract → Maestro Core → Execution/State/Monitoring
```

Data access and broker execution are separate layers:

```text
Strategy plugins
    │ request DataRequest objects through Maestro SDK
    ▼
datahub/
    market and research data: price, ohlcv, macro, news, sentiment,
    fundamental, financial_statements, and technical_indicators
    providers/adapters: mock, CSV/local, Yahoo/yfinance-style OHLCV, FRED,
    RSS, GDELT, NewsAPI, rule-based sentiment, and future community providers

execution/brokers/
    broker account and execution data: auth, balances, positions, buying power,
    orders, fills, reconciliation, broker_quote reference data
```

DataHub, execution, broker adapters, approval, state, audit, and dashboard are
Maestro internal modules. Yahoo/yfinance, FRED, RSS feeds, KIS Open API, and
Telegram Bot API are external systems reached through internal adapters.
Strategy plugins must request data through Maestro DataHub and must not call
external research data APIs or broker APIs directly. KIS current price lookup may
be used as broker-side `broker_quote` reference data for execution validation or
reconciliation, but KIS is not the primary strategy or research data source.

Maestro owns the lifecycle of a portfolio management cycle:

```text
Config
→ Plugin loading
→ Optional dynamic-universe candidate evaluation
→ Data requests
→ DataHub
→ Strategy execution
→ Strategy result validation
→ Portfolio construction
→ Risk management
→ Order proposal/order generation
→ Execution
→ State update
→ Audit log
```

The current orchestration boundary keeps `MaestroOrchestrator` as the run-cycle
coordinator while moving live execution hardening checks into
`orchestration/live_gates.py`. New code should use `state/events.py` for system
event names and audited event persistence rather than repeating raw event-type
strings across services.

Live-order code remains backward compatible through
`maestro.execution.live_orders`, but new imports should prefer the narrower
`live_order_models.py`, `live_order_ports.py`, `live_order_status.py`,
`live_order_cancellation.py`, `live_order_fills.py`,
`live_order_lifecycle.py`, `live_order_safety.py`, and
`live_order_services.py` boundaries. KIS REST network transport is isolated in
`execution/brokers/kis/transport.py`; parser helpers live in
`execution/brokers/kis/parsers.py`; product-level adapter modules split
domestic/overseas read-only and live-order behavior.

Config models are physically split by domain under `config/`, with
`config/models.py` retained as the compatibility import surface. Health checks
use shared health models and a provider wrapper so future checks can be added
without expanding the `HealthService.run()` list directly.

## 2. Recommended Repository Structure

```text
maestro/
├── README.md
├── PRD.md
├── TRD.md
├── Implementation Plan.md
├── TASKS.md
├── pyproject.toml
├── .env.example
├── configs/
│   └── paper.yaml
├── src/
│   └── maestro/
│       ├── __init__.py
│       ├── cli.py
│       ├── sdk/
│       │   ├── __init__.py
│       │   ├── strategy.py
│       │   └── schemas.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── schemas.py
│       │   ├── enums.py
│       │   ├── exceptions.py
│       │   ├── ids.py
│       │   └── clock.py
│       ├── config/
│       │   ├── __init__.py
│       │   ├── loader.py
│       │   └── models.py
│       ├── plugins/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── loader.py
│       │   ├── registry.py
│       │   └── permissions.py
│       ├── datahub/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── mock_provider.py
│       │   ├── validators.py
│       │   └── normalizer.py
│       ├── orchestration/
│       │   ├── __init__.py
│       │   ├── orchestrator.py
│       │   └── cycle.py
│       ├── signals/
│       │   ├── __init__.py
│       │   ├── validator.py
│       │   └── aggregator.py
│       ├── portfolio/
│       │   ├── __init__.py
│       │   ├── manager.py
│       │   └── rebalancer.py
│       ├── risk/
│       │   ├── __init__.py
│       │   ├── manager.py
│       │   └── limits.py
│       ├── execution/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── paper.py
│       │   ├── order_builder.py
│       │   └── brokers/
│       │       ├── __init__.py
│       │       ├── base.py
│       │       └── kis/
│       │           ├── __init__.py
│       │           ├── auth.py
│       │           ├── client.py
│       │           ├── orders.py
│       │           ├── account.py
│       │           ├── market_data.py
│       │           ├── websocket.py
│       │           └── errors.py
│       ├── state/
│       │   ├── __init__.py
│       │   ├── store.py
│       │   ├── sqlite_store.py
│       │   ├── portfolio_state.py
│       │   └── reconciliation.py
│       ├── monitoring/
│       │   ├── __init__.py
│       │   ├── audit_logger.py
│       │   ├── events.py
│       │   └── health.py
│       ├── approval/
│       │   ├── __init__.py
│       │   ├── manager.py
│       │   ├── models.py
│       │   └── policies.py
│       ├── integrations/
│       │   ├── __init__.py
│       │   └── telegram/
│       │       ├── __init__.py
│       │       ├── bot.py
│       │       ├── formatter.py
│       │       └── handlers.py
│       ├── dashboard/
│       │   ├── __init__.py
│       │   └── app.py
│       └── cli.py
│           ├── __init__.py
│           ├── server.py
│           └── routes_status.py
├── examples/
│   └── sample_static_allocation/
│       ├── pyproject.toml
│       ├── README.md
│       └── src/
│           └── sample_static_allocation/
│               ├── __init__.py
│               └── strategy.py
└── tests/
    ├── test_config.py
    ├── test_plugin_loader.py
    ├── test_orchestrator.py
    ├── test_portfolio_manager.py
    ├── test_risk_manager.py
    ├── test_paper_execution.py
    └── test_integration_run_once.py
```

Some future modules may be created as stubs in v0.1 but not fully implemented.

`datahub/` owns research and market data normalization, validation, routing, freshness, and provider selection. Its supported data types include `price`, `ohlcv`, `macro`, `news`, `sentiment`, `fundamental`, `financial_statements`, `technical_indicators`, `insider_transactions`, and `broker_quote`.

`execution/brokers/` owns broker/account/execution integrations. Broker adapters, including KIS, should expose account state, buying power, positions, order submission/status, fills, reconciliation, and optional `broker_quote` data used only for execution validation or reconciliation.

Broker routing is account-aware. Top-level `accounts` declare stable logical
account IDs, and `strategies[*].account_id` maps each enabled Virtuoso app to
one account. Many strategies may share one account. Legacy single-account
`kis:` config is preserved by deriving `default_kis`. KIS real and KIS
paper-trading accounts are operationally routed through the existing KIS
clients. Toss accounts use a common domestic/overseas OpenAPI adapter for
account, holding, price, buying-power, submit, status, modification, and
cancellation. Initial armed operation permits approval-gated integer-quantity
DAY limit orders.
Operator phase configs can set `broker_accounts_path` so all phases share one
broker account inventory file. They can also set `strategy_account_map_path` so
signal and approval share one strategy-to-account mapping file. The loader
applies the account inventory before the strategy mapping and before validation;
config identity includes both shared file byte streams.

The v0.2 provider planning contract is documented in [datahub.md](datahub.md).
Future DataHub providers should route by canonical symbol, asset type, data type,
timeframe/lookback, and run mode, then return normalized `DataBundle` payloads.
Provider-specific symbols and caches belong behind DataHub, not inside strategy
plugins or broker adapters. v0.2 does not implement real external providers or
network calls.

The v0.3 scaffold adds `DataHubRegistry` and `DataHubRouter` as lightweight
in-process routing infrastructure. They are intentionally small: they record
provider capabilities, route `DataRequest` objects, normalize payloads, and
surface clear DataHub errors. They do not implement external API clients,
database caches, or broker behavior.

## 3. Technology Stack

### 3.1 Runtime

- Python 3.11+
- `uv` recommended for dependency/environment management
- CLI via Typer or argparse
- Pydantic v2 for schema validation
- PyYAML or ruamel.yaml for config parsing
- SQLite for state store
- JSONL for append-only audit logs
- pytest for tests

### 3.1.1 Current Operator Architecture

Maestro currently uses a hybrid operator architecture, not a single always-on
daemon. One-shot CLI jobs (`run-once`, `kis-sync`, `reconcile`, `health`, and
related recovery commands) coexist with long-running operator services
(`telegram-operator` and the FastAPI/React dashboard). These processes coordinate
through the configured SQLite state DB and JSONL audit log.

Every process in one deployment should use the intended operator-local config
or config set, state DB, and audit log. `mode` is the validated safety contract
(`paper`, `live_readonly`, or `live_approval`); a `profile` is the broader
operator recipe made from mode plus strategy, data, KIS, approval, execution,
reconciliation, monitoring, state, and audit settings. Single-profile
deployments carry that profile in one YAML file resolved from `MAESTRO_CONFIG`.
The Symphony workflow uses three phase-specific YAML files resolved from
`MAESTRO_READONLY_CONFIG`, `MAESTRO_SIGNAL_CONFIG`, and
`MAESTRO_APPROVAL_CONFIG`; they must share one state/audit identity.

SQLite currently uses connection timeout, `busy_timeout`, and WAL mode for
basic multi-process coexistence. StateStore also serializes writes with a
writer lock, stores the operator config path plus full, state-affecting, and
runtime config fingerprints in state metadata, rejects the same state DB being
opened with a different state-affecting config identity, and includes config
identity in heartbeat/audit payloads. Runtime-only config changes, for example
monitoring thresholds, do not force a new state DB. This is the current
daemon-before safety boundary for CLI jobs plus Telegram/dashboard services.
Operator status surfaces config identity plus state/audit paths so a running
process can be checked against the intended deployment config.

`profile_stage` is derived from the validated config and can optionally be
pinned in operator-local YAML. It separates promotion stage from `mode`:
`paper`, `paper_real_data`, `live_readonly`, `live_approval_disabled`,
`live_approval_dry_run`, `kis_paper_trading`, or `production_armed`. Use
`maestro profile-diff --left A --right B` before switching operator configs and
`maestro profile-validate --config A --target-stage production_armed` before
first real-account submission.

Daemonization is deferred until Maestro needs a central scheduler/job queue,
coordinated approval/status polling, or live-order recovery that cannot be
reliably handled by one-shot jobs plus explicit locks.

### 3.1.2 Target Signal-to-Approval Operator Workflow

The operator workflow separates Symphony operation into three profiles.
`run-signal`, `approve-signal`, the three-profile config split, broker snapshot
refs in signal packages, and stale broker-ref rejection during approval are
implemented. Approval also rejects expired signal packages, config runtime
fingerprint mismatches, and strategy-account mapping drift. Signal packages
also carry DataHub evidence, and approval re-runs the live execution gates over
the saved evidence before requesting approval. The approval path also rejects
material broker snapshot drift from the signal baseline and carries
`signal_run_id` into broker reconciliation events.

- `symphony_readonly`: refresh broker/account truth for dashboard and Telegram
  views. It runs no Virtuoso strategies and generates no orders or approvals.
- `symphony_signal`: refresh broker/account truth, run Virtuoso apps, persist an
  immutable signal package, and expose `action_required`; the scheduler invokes
  approval only when `action_required=true`. It submits no broker orders.
- `symphony_approval`: consume a required `signal_run_id`, load the saved signal
  package, and execute approval/order workflow without re-running Virtuoso
  strategies.

Broker account definitions are loaded from the shared `broker_accounts_path`
file. Strategy-level phase controls are loaded from the shared
`strategy_account_map_path` file. `enabled` is the operator-facing switch for
whether the app is in the Symphony universe. `readonly` controls dashboard/Telegram
visibility, `signal` controls whether `symphony_signal` imports and runs
the app, and `order_posture` controls whether generated orders are excluded from
approval, included as dry-run approvals, or allowed to become broker-submit
candidates. The global `execution.order_posture` remains the ceiling: global
`disabled` disables all approvals/submits, global `dry_run` downgrades strategy
`armed` orders, and only global `armed` allows strategy `armed` orders to reach
broker submit. The brokerless `dev_sandbox` account supports development
strategies that need signal and approval UX rehearsal without touching KIS or
Toss broker APIs. A single account must not mix signal-enabled strategies with
different effective order postures in one Symphony run.

This is a lifecycle split, not a loosening of `live_readonly`. `live_readonly`
continues to mean broker read-only with no strategy execution. The signal phase
is a separate workflow because strategy-generated recommendations are
part of the trading lifecycle even when no broker submit occurs.

Systemd remains the production wiring layer. `maestro-symphony-readonly.timer`
refreshes broker truth and reconciliation periodically, while
`maestro-symphony-signal.timer` runs `maestro daily-signal-approval` on the
trading-day schedule. That command obtains a file lock, refreshes read-only
broker state when configured, sends the daily Telegram signal summary, skips
approval when `action_required=false`, and stops the long-running Telegram
operator while approval polling is active.

`signal_run_id` handoff must be immutable and auditable. The saved signal
package should include the config identity, generated timestamp, account
mapping, broker snapshot references, DataHub evidence, strategy results,
account-scoped targets, risk preview, and optional order preview. Approval must
fail closed if the signal is expired, missing, mutated, generated by a different
state-affecting config, based on stale broker/data evidence, or no longer
compatible with current account mappings.

The approval phase is execution of a stored decision, not a second strategy
judgment. It may re-check broker truth, prices, reconciliation, safety state,
limits, and kill-switch state, but it must not call Virtuoso `run()` again for
the same `signal_run_id`.

### 3.2 Future Integrations

- FastAPI plus built Vite/React for the read-only dashboard
- WebSocket/API daemon surface deferred until a Maestro daemon milestone
- python-telegram-bot or aiogram for Telegram integration
- Korea Investment Securities Open API via REST and later WebSocket
- systemd for VPS deployment initially
- Docker Compose later if services grow

### 3.3 Phase 1 Additions

- `CSVDataProvider` supports simple CSV OHLCV files with columns:
  `timestamp,symbol,open,high,low,close,volume`.
- `datahub.provider` selects `mock` or `csv`.
- `datahub.csv_path` is required for CSV mode.
- SQLite state exposes read-only query methods for dashboard and status views.
- Dashboard is optional and read-only; it requires the `dashboard` extra.

### 3.4 Phase 2 Additions

- Approval requests are created after risk checks and order proposal generation.
- Paper fills execute only when approval is not required or the approval decision is `approved`.
- Approval decisions are persisted in SQLite and audit JSONL.
- `approval.default_decision` supports `approved`, `rejected`, and `expired` for the Phase 2 no-network stub.
- Telegram approval uses the Bot API polling boundary in paper and live approval
  modes when configured; normal tests inject fake clients and do not call the
  network.

### 3.5 Current KIS Read-only Foundation

- `RunMode.LIVE_READONLY` is supported for read-only broker sync commands.
- KIS integration is isolated under `execution/brokers/kis`.
- `KISReadOnlyClient` defines account snapshot, positions, buying power, broker-side quote/reference, order/fill, and unfilled order reads.
- `MockKISReadOnlyClient` provides deterministic no-network responses.
- KIS REST clients adapt the reference repo's KIS OAuth token issuance, request headers, TR_IDs, and inquiry payloads behind product-specific adapters.
- App key and secret are read from configured environment variable names only.
- Access tokens can come from an environment variable or an owner-only cache file after `/oauth2/tokenP` issuance. Tokens may be stored only in `kis.token_cache_path`, never in state, audit logs, dashboard rows, or tests.
- KIS WebSocket approval keys can come from `kis.approval_key_env` or be issued
  through `/oauth2/Approval` using the OAuth workbook's `grant_type`, `appkey`,
  and `secretkey` request body. Approval keys are treated as secrets and are not
  persisted to state, audit logs, dashboard rows, or tests.
- KIS REST support is split by broker product. `kis_domestic_stock` covers KRX
  stock/ETF read-only and approval-gated cash limit orders. `kis_overseas_stock`
  covers US-listed stock/ETF read-only and approval-gated limit orders.
  Multi-product configs use `kis.broker_products` and route each order by
  `TradableInstrument.broker_product` after checking endpoint paths, TR_IDs,
  exchange codes, and fields against Korea Investment Securities OpenAPI
  examples.
- Broker account snapshots are persisted in SQLite and audit JSONL.
- `maestro reconcile` compares Maestro portfolio state with the latest broker account snapshot and persists a `broker_reconciliation` system event plus audit event.
- v0.5 exposes no callable KIS order submission, cancel, amend, buy, or sell path.

### 3.6 Current Live Approval Order Foundation

- `RunMode.LIVE_APPROVAL` is defined for approval-gated live trading work. There is
  no `live_auto` mode.
- Live orders are disabled by default with:
  `execution.order_posture=disabled`, `require_reconciliation_pass=true`,
  `live_order_limits.max_order_notional_by_currency` set to zero values,
  `live_order_limits.max_daily_notional_by_currency` set to zero values, and
  `allowed_order_type=limit`.
- `LiveOrderSafetyService` is the only internal live order submission boundary.
  It requires an approved approval decision, the latest `broker_reconciliation`
  event to pass, limit orders only, per-order and daily notional caps,
  duplicate-order prevention, and halt-on-unknown-state behavior.
- `LiveOrderRequest`, `LiveOrderResult`, `BrokerOrderId`, and expanded
  `OrderStatus` values define the live order contract.
- `LiveOrderStatusClient` exposes `get_order_status(broker_order_id)`.
  `LiveOrderStatusService` persists snapshots as `live_order_status` system
  events and audit events; unknown broker status is converted to `halted`.
- `LiveOrderStatusSnapshot`, `FillEvent`, and `PartialFillSummary` define status
  and partial-fill normalization.
- `execution.order_posture=dry_run` keeps the live approval path through
  strategy, risk, reconciliation, and approval, then persists `live_order_dry_run`
  events without calling the broker submit adapter.
- When `execution.broker_validation.require_quote_validation=true`, order
  generation can reuse the latest broker account snapshot's validated
  `current_prices` as the live approval limit-price basis. This keeps the
  generated order aligned with the broker quote snapshot used for execution
  validation without making broker quotes a strategy research feed.
- `maestro live-preflight --config ...` exposes the live approval safety
  preflight as a scriptable CLI gate and exits nonzero on preflight failure.
- `maestro adopt-broker-snapshot --config ... --reason ...` is a state-only
  operator command for the first real-account rehearsal baseline. It copies the
  latest broker snapshot into Maestro portfolio state, records
  `broker_snapshot_adopted`, and defaults to refusing positions that are neither
  in `portfolio.allowed_symbols` nor known `universe.instruments` allowed by
  `universe.policy`. Operator configs may set
  `portfolio.unknown_broker_position_policy=include_readonly` to include
  unknown already-held broker positions in read-only display/adoption; target
  allocation validation and live order execution still reject unknown symbols.
- Live configs do not carry `portfolio.initial_cash`; live cash and positions
  are sourced from the adopted broker snapshot. KIS-backed
  `live_approval run-once` refreshes and adopts the broker snapshot before
  strategy execution. When `execution.require_reconciliation_pass=true`, it
  reconciles the adopted Maestro state against that same saved broker snapshot
  under the run id before approval and order gates, without issuing a second KIS
  snapshot fetch.
- KIS domestic and overseas live buy orders implement a pre-submit broker
  validation step with the request symbol and exact limit price before broker
  submit. Domestic orders use the domestic buying-power path; overseas orders
  call `/uapi/overseas-stock/v1/trading/inquire-psamount`. Insufficient KIS
  buying power or max buy quantity fails before any order endpoint call.
- KIS overseas order status lookup uses the broker submission timestamp to query
  the corresponding US exchange-local date range, reducing false unknown states
  around Korea/US date boundaries.
- `PartialFillReconciliationService` reads recent `live_order_status` events,
  compares cumulative filled quantity/notional against previous
  `fill_reconciliation` watermarks, applies only new fill deltas to cash and
  positions, persists a portfolio snapshot when fills were applied, and writes
  `fill_reconciliation` system and audit events.
- `FillReconciliationResult` and `AppliedFill` describe applied deltas; skipped
  statuses include duplicate/no-new-fill, terminal non-fill states, and unknown
  broker state. Rejected, canceled, halted, and unknown statuses never update the
  portfolio.
- `LiveOrderCancelRequest`, `LiveOrderCancelResult`, and `LiveOrderCancelClient`
  define the cancellation interface. There is no direct cancel CLI. KIS overseas
  cancel is implemented only behind `LiveOrderCancellationService` after endpoint
  path, TR_ID, and body fields were checked against Korea Investment Securities
  OpenAPI examples.
- Cancellation requires Telegram approval, latest broker reconciliation pass, and
  latest live order status of `open` or `partially_filled`. Partial-fill
  cancellation is only for the remaining open quantity and requires a prior fill
  reconciliation event. Filled, rejected, canceled, halted, and unknown states
  block cancellation; unknown broker state does not attempt cancel.
- Successful cancellation attempts are persisted as `live_order_cancel` system
  events and audit events. Duplicate cancellation attempts for the same broker
  order ID are rejected.
- `LiveOrderWorkflowService` orchestrates one approval-gated post-order workflow:
  submit through `LiveOrderSafetyService`, stop on halted/unknown submission,
  poll through `LiveOrderStatusService`, reconcile fills, optionally run broker
  reconciliation, and persist a `live_order_workflow` system/audit summary.
- `LiveOrderWorkflowResult` records submitted order result, latest status
  snapshot, applied fills, optional broker reconciliation result, workflow status,
  and halt/failure reason.
- `LiveOrderLifecycleService` runs a bounded multi-poll lifecycle loop. It polls
  until terminal status or `execution.order_status_max_polls`, reconciles fills
  after every poll, optionally runs broker reconciliation after fill updates, and
  persists a `live_order_lifecycle` system/audit summary. Reaching max polls is
  non-terminal and does not auto-cancel.
- `build_live_approval_dependencies()` wires the live approval service graph:
  state store, audit logger, safety service, status service, fill
  reconciliation, optional broker reconciliation, optional notifications, KIS
  live order/status clients when `kis.provider="kis"`, and injected fake clients
  for tests.
- KIS REST clients share a small base for broker-agnostic plumbing:
  credentials/auth wiring, common headers, GET/POST error handling, pagination,
  real-vs-demo TR_ID selection, and canonical symbol helpers. Product adapters
  remain separate because KIS domestic and overseas APIs use different endpoint
  families, request fields, TR_IDs, quote exchange codes, price formats, cash
  currencies, and response fields.
- `LiveOrderNotificationClient` is implemented for Telegram lifecycle
  notifications through the existing Bot API client boundary. It sends lifecycle,
  fill-status, halt, and failure messages only; it does not add buttons,
  high-risk admin controls, or write controls.
- Safe polling defaults are `order_status_poll_interval_seconds=30`,
  `order_status_max_polls=20`, and `order_status_terminal_timeout_seconds=1800`.
- `KISRestDomesticStockLiveOrderClient` adapts the domestic-stock cash order
  endpoint from the KIS open-trading-api reference:
  `POST /uapi/domestic-stock/v1/trading/order-cash`, real TR_IDs
  `TTTC0012U`/`TTTC0011U`, demo TR_IDs `VTTC0012U`/`VTTC0011U`, `ORD_DVSN=00`
  for limit orders, and uppercase KIS body keys. It also implements
  pre-submit buying-power/max-quantity validation for buy orders. It is an
  explicit domestic adapter path, not a core product assumption.
- `KISRestOverseasStockLiveOrderClient` exists as the strategic adapter boundary
  for US-listed stocks and ETFs. Its read-only, limit-order submit, status, and
  cancellation paths are implemented behind approval, reconciliation, safety, and
  product validation gates after endpoint paths, TR_IDs, exchange codes, and
  request/response fields were checked against Korea Investment Securities
  OpenAPI examples.
- KIS status tracking normalizes accepted, open, partially filled, filled,
  rejected, canceled, and unknown states into Maestro `OrderStatus` inside each
  product adapter.
- `MaestroOrchestrator.run_once()` remains unchanged for paper mode. In
  `live_approval` mode it reuses the proposal and approval path, optionally
  overlays broker snapshot prices when broker quote validation is required,
  converts approved proposed orders into limit-order `LiveOrderRequest` objects,
  and runs the bounded live order lifecycle service. This is product-level
  wiring for approval-gated live orders, not live automation.
- Maestro exposes no direct unguarded buy/sell CLI, no market orders, and no
  dashboard write controls. Real KIS and Telegram network checks are
  operator-triggered smoke/rehearsal procedures, not normal tests.

### 3.7 Production Readiness Boundary

Approval-gated broker submission is not the same as production readiness. Before
Maestro should be considered ready for repeated real-account operation, the
roadmap must close the v0.8.x hardening path:

- Real account promotion from paper to read-only, dry-run, minimum-size order,
  and limited repeated operation.
- Production DataHub hardening for stale fail-closed behavior, proposal data
  snapshots, market session checks, provider retry/rate-limit behavior, and
  broker quote validation.
- Real risk enforcement now includes an operator-enabled broker snapshot gate for
  buying power, post-order cash, pending orders, fee buffer,
  settlement/buying-power availability, unreconciled broker activity, and broker
  PnL based daily loss limits.
- Live order recovery for ambiguous submit results, process crashes, broker
  timeout cases, partial-fill mismatches, and idempotency gaps. Recovery is
  explicit: unresolved submit/lifecycle state records recovery events, blocks
  later live approval orders, and requires broker truth plus
  `recover-live-order` completion before continuing.
- Operations hardening for heartbeat monitoring, Telegram error escalation,
  audit integrity, backup/restore drills, and halt-recovery rehearsals.

Dynamic universe work should not expand the tradable surface until these
operator-readiness items are explicit and testable.

## 4. Public SDK Design

External Virtuoso apps should import only from `maestro.sdk`.

Example:

```python
from maestro.sdk import (
    BaseStrategyPlugin,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
```

The SDK is the stable public app developer API. Internal Maestro modules may change, but SDK compatibility should be preserved as much as possible.

## 5. Core Schemas

Use Pydantic models unless there is a strong reason not to.

### 5.1 Enums

Suggested enums:

```python
class RunMode(str, Enum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE_READONLY = "live_readonly"
    LIVE_APPROVAL = "live_approval"

class StrategyMode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"
    DISABLED = "disabled"

class AssetType(str, Enum):
    CASH = "cash"
    DOMESTIC_STOCK = "domestic_stock"
    DOMESTIC_ETF = "domestic_etf"
    US_STOCK = "us_stock"
    US_ETF = "us_etf"
    CRYPTO = "crypto"

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"

class OrderStatus(str, Enum):
    CREATED = "created"
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    ACCEPTED_BY_BROKER = "accepted_by_broker"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
```

### 5.2 StrategyManifest

```python
class StrategyManifest(BaseModel):
    sdk_contract_version: str = "1.0"
    strategy_id: str
    name: str
    version: str
    description: str | None = None
    author: str | None = None
    supported_modes: list[StrategyMode]
    supported_asset_types: list[AssetType]
    result_type: Literal["target_allocation", "strategy_signal"] = "target_allocation"
    requires_data: list[str] = []
    default_time_horizon: str | None = None
    can_run_live: bool = False
    can_use_leverage: bool = False
    can_short: bool = False
    supports_dynamic_universe: bool = False
    max_candidate_symbols: int | None = None
    allowed_data_types: list[str] = []
    requires_llm: bool = False
    supported_llm_providers: list[str] = []
    required_env_vars: list[str] = []
    estimated_runtime_seconds: int | None = None
    allow_direct_external_data_calls: bool = False
```

Maestro rejects plugins that require a newer SDK contract version than the
current supported contract. The loader also rejects a strategy when
the Maestro run mode is not declared in `StrategyManifest.supported_modes`, and
requires `StrategyManifest.can_run_live=True` before loading an enabled strategy
in `live_approval` mode. `strategy_signal` is public SDK structure for LLM
research apps and can be loaded when strategy config includes an explicit
Maestro-owned `signal_to_allocation` policy.

### 5.3 StrategyContext

```python
class StrategyContext(BaseModel):
    cycle_id: str
    timestamp: datetime
    run_mode: RunMode
    strategy_id: str
    portfolio_state: PortfolioState | None = None
    config: dict[str, Any] = {}
```

### 5.4 DataRequest

```python
class DataRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    data_type: str  # e.g., "price", "ohlcv", "macro", "news", "technical_indicators"
    intended_use: Literal["research", "tradable"] = "research"
    timeframe: str | None = None
    lookback: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    indicator: str | None = None
    limit: int | None = None
    query: str | None = None
    statement_type: (
        Literal["balance_sheet", "cashflow", "cash_flow", "income_statement"] | None
    ) = None
    frequency: Literal["annual", "quarterly", "trailing"] | None = None
    provider_hint: str | None = None
    source_hint: str | None = None
    fields: list[str] = []
```

`broker_quote` is reserved for broker-side reference prices used by execution validation or reconciliation. It should not replace DataHub research feeds for strategy decisions.

Dynamic-universe work adds a separate candidate request contract:

```python
class CandidateInstrumentRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    intended_use: Literal["research", "tradable"]
    data_types: list[str] = ["price"]
    region: str | None = None
    currency: str | None = None
    broker_product: str | None = None
    exchange_code: str | None = None
    broker_symbol: str | None = None
    reason: str | None = None
```

`intended_use: research` covers analysis-only symbols, series, and keywords such
as `SPY`, `VIX`, `DXY`, FRED macro series, and news topics. `intended_use:
tradable` asks Maestro to validate whether a canonical instrument can become
eligible for allocation and execution.

### 5.5 DataBundle

```python
class DataBundle(BaseModel):
    requests: list[DataRequest]
    data: dict[str, Any]
    generated_at: datetime
    source: str
```

### 5.6 TargetAllocationResult

v0.1 supports only target allocation results.

```python
class TargetAllocationResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    allocations: dict[str, float]  # symbol -> target weight
    allocation_sleeves: dict[str, dict[str, float]] | None = None
    strategy_books: list[StrategyBookAllocation] = []
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = []
    metadata: dict[str, Any] = {}
```

`TargetAllocationResult.metadata` carries structured source signals, ratings,
report summaries, tool traces, and model details when an app converts a signal
into executable target weights. Maestro rejects allocations to research-only,
unknown, unresolved, or broker-untradable symbols. Virtuoso apps may propose
candidate symbols, but they cannot directly approve tradability or execute
orders.

`strategy_books` is optional accounting metadata for apps that contain multiple
internal books or sub-strategies. Maestro records virtual book snapshots and
dashboard read models from these books, but execution remains based on the
validated aggregate portfolio target.

### 5.6.1 StrategySignalResult

SDK contract 1.0 defines a directional signal result for LLM research apps.

```python
class StrategySignalResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    symbol: str
    action: Literal["buy", "hold", "sell"]
    rating: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    price_target: float | None = None
    stop_loss: float | None = None
    time_horizon: str | None = None
    position_sizing: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = []
    metadata: dict[str, Any] = {}
```

The orchestration path normalizes `strategy_signal` results immediately after
`run()` when the strategy config supplies `signal_to_allocation`. The normalized
`TargetAllocationResult` then follows the same validator, portfolio, risk,
approval, and execution path as direct target allocation plugins.

### 5.7 PortfolioTarget

```python
class PortfolioTarget(BaseModel):
    cycle_id: str
    timestamp: datetime
    allocations: dict[str, float]
    source_results: list[str] = []
    rationale: str | None = None
    metadata: dict[str, Any] = {}
```

### 5.8 RiskDecision

```python
class RiskDecision(BaseModel):
    cycle_id: str
    approved: bool
    original_target: PortfolioTarget
    adjusted_target: PortfolioTarget | None = None
    violations: list[str] = []
    rationale: str | None = None
```

### 5.9 OrderIntent

```python
class OrderIntent(BaseModel):
    order_intent_id: str
    cycle_id: str
    symbol: str
    asset_type: AssetType
    side: OrderSide
    quantity: float | None = None
    notional: float | None = None
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    rationale: str | None = None
```

### 5.10 ExecutionResult

```python
class ExecutionResult(BaseModel):
    execution_id: str
    order_intent_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    status: OrderStatus
    filled_quantity: float = 0.0
    average_price: float | None = None
    fees: float = 0.0
    message: str | None = None
    timestamp: datetime
```

### 5.11 PortfolioState

```python
class Position(BaseModel):
    symbol: str
    asset_type: AssetType
    quantity: float
    average_price: float | None = None
    market_price: float | None = None
    market_value: float = 0.0
    unrealized_pnl: float = 0.0

class PortfolioState(BaseModel):
    timestamp: datetime
    base_currency: str
    cash: float
    positions: dict[str, Position]
    total_equity: float
    metadata: dict[str, Any] = {}
```

## 6. Plugin Contract

Every Virtuoso app must implement:

```python
class BaseStrategyPlugin(Protocol):
    def manifest(self) -> StrategyManifest:
        ...

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        ...

    def build_candidate_requests(
        self,
        context: StrategyContext,
    ) -> list[CandidateInstrumentRequest]:
        ...

    def run(self, data: DataBundle, context: StrategyContext) -> StrategyResult:
        ...
```

Future optional methods:

```python
def health_check(self) -> HealthStatus: ...
def on_start(self, context: StrategyContext) -> None: ...
def on_stop(self, context: StrategyContext) -> None: ...
def on_error(self, error: Exception, context: StrategyContext) -> None: ...
```

The optional candidate-discovery method
`build_candidate_requests(context) -> list[CandidateInstrumentRequest]` lets
Virtuoso apps propose research inputs and tradable candidates, while
Maestro remains responsible for validation, metadata resolution, DataHub checks,
broker tradability checks, operator approval when policy requires it, and
allocation eligibility.

## 7. Plugin Loading

Config example:

```yaml
strategies:
  - id: sample_static_allocation
    enabled: true
    weight: 1.0
    entrypoint: "sample_static_allocation.strategy:SampleStaticAllocationStrategy"
    config:
      allocations:
        CASH: 0.5
        MOCK_ETF_A: 0.3
        MOCK_ETF_B: 0.2
```

Loader behavior:

1. Parse entrypoint `module:class`.
2. Import module via `importlib`.
3. Instantiate class.
4. Verify instance implements required methods.
5. Read manifest.
6. Validate manifest strategy ID against config ID.
7. Validate Maestro run mode against `manifest.supported_modes`.
8. Require `manifest.can_run_live=True` for `live_approval`.
9. Register plugin in registry.

## 8. Configuration Model

`configs/paper.yaml` should support:

```yaml
mode: paper

portfolio:
  base_currency: KRW
  initial_cash: 10000000
  allowed_symbols:
    - CASH
    - MOCK_ETF_A
    - MOCK_ETF_B

strategies:
  - id: sample_static_allocation
    enabled: true
    weight: 1.0
    entrypoint: "sample_static_allocation.strategy:SampleStaticAllocationStrategy"
    config:
      allocations:
        CASH: 0.5
        MOCK_ETF_A: 0.3
        MOCK_ETF_B: 0.2

datahub:
  provider: mock

execution:
  proposal_engine: paper

state:
  sqlite_path: var/maestro_state.db

audit:
  jsonl_path: var/audit.jsonl
```

Maestro v0.1.1 uses strict Pydantic config validation. Unknown YAML fields fail
loudly instead of being ignored.

Execution config uses nested `execution.market_session` and
`execution.broker_validation` blocks and the `execution.live_order_limits` block
as the canonical schema. The older flat market-session, broker-validation, and
live-order-limit keys remain accepted for one migration release, but configs
must not mix legacy keys with the corresponding nested block.
Operator freshness thresholds live in the top-level `monitoring` block. The
older `execution.heartbeat_max_age_seconds` and
`execution.scheduled_run_max_age_seconds` keys remain accepted for one migration
release, but configs must not mix them with `monitoring`.

Static `allowed_symbols` is valid as an explicit safety override. When omitted,
Maestro derives it from `portfolio.currency_sleeves` or, for single-allocation
configs, from all configured `universe.instruments`. It should not be treated as
the final production universe model. The production path uses `UniversePolicy`
and an `InstrumentResolver` so Virtuoso apps can propose candidates and Maestro
can approve only symbols that satisfy research or tradability requirements.
Standard cash instruments are derived from portfolio cash symbols when omitted.

## 9. Orchestrator Run Cycle

`MaestroOrchestrator.run_once()` should:

1. Create `cycle_id`.
2. Load current portfolio state.
3. Load enabled, signal-enabled strategies.
4. For each strategy:
   - Build `StrategyContext`.
   - Request data requests.
   - Fetch data from DataHub.
   - Run plugin.
   - Normalize `StrategySignalResult` to `TargetAllocationResult` when needed.
   - Validate normalized result.
   - Log strategy run.
5. Aggregate valid target allocation results.
6. Build `PortfolioTarget`.
7. Run RiskManager.
8. If risk rejected: log and stop before execution.
9. Build paper orders from current state and approved target.
10. Execute paper orders.
11. Update state.
12. Write audit log and state snapshot.
13. Return cycle summary.

## 10. PortfolioManager v0.1 Logic

Inputs:

- Valid `TargetAllocationResult` objects, including policy-normalized
  `StrategySignalResult` outputs
- Strategy weights from config

Algorithm:

1. Ignore disabled strategies and, in the Symphony signal workflow,
   signal-disabled strategies.
2. Multiply each allocation by strategy weight.
3. Sum symbol weights across strategies.
4. Normalize if sum > 1.0.
5. Allocate residual to `CASH` if sum < 1.0.
6. Return `PortfolioTarget`.

## 11. RiskManager v0.1 Logic

Risk checks:

- All symbols in allowed universe.
- All weights >= 0.
- Total gross exposure <= 1.0.
- No short exposure.
- No leverage.

Risk behavior:

- If an unknown symbol is found, reject.
- If gross exposure is below 1.0, leave residual exposure in cash.
- Read-only broker display/adoption can include unknown already-held positions
  only when `portfolio.unknown_broker_position_policy=include_readonly`; this
  does not make those symbols valid target allocation or live order symbols.

## 12. PaperExecutionEngine v0.1

Inputs:

- Current `PortfolioState`
- Approved `PortfolioTarget`
- Mock prices from DataHub or internal price map

Behavior:

1. Compute current weights.
2. Compute target notional per symbol.
3. Compute difference from current notional.
4. Ignore small differences below threshold if configured.
5. Generate buy/sell order intents.
6. Fill all paper orders immediately at mock price.
7. Return execution results.

## 13. State Store

Use SQLite for queryable state and JSONL for append-only audit.

Suggested SQLite tables:

### portfolio_snapshots

- id
- timestamp
- cycle_id
- base_currency
- cash
- total_equity
- positions_json
- metadata_json

### strategy_runs

- id
- timestamp
- cycle_id
- strategy_id
- strategy_version
- status
- confidence
- result_json
- error_message

### orders

- id
- timestamp
- cycle_id
- order_intent_id
- symbol
- side
- quantity
- notional
- order_type
- limit_price
- status
- filled_quantity
- average_price
- broker_order_id
- metadata_json

### risk_decisions

- id
- timestamp
- cycle_id
- approved
- violations_json
- decision_json

Current account, currency-sleeve, and total-portfolio performance read models
are computed in `dashboard/read_models.py` from existing persisted broker
snapshots and reconciliation events. They are not persisted as dedicated
performance tables yet. Planned performance tracking should add queryable
read-model tables or views for account, strategy, currency-sleeve, and total
portfolio performance. Each record should include timestamp/as-of, source
snapshot/event IDs, currency, beginning value, ending value, cash flows,
realized PnL, unrealized PnL, fees when available, daily return, cumulative
return, drawdown, and reconciliation/freshness status. FX-enabled total
portfolio records should also include
`display_currency`, `base_currency`, `fx_source`, `fx_rate`, `fx_timestamp`,
`fx_status`, `local_return`, `fx_effect`, `converted_total_value`, and
`converted_return`. These tables are dashboard/read-model state, not a broker
order path.

### system_events

- id
- timestamp
- cycle_id
- level
- component
- event_type
- message
- details_json

## 14. Audit JSONL Format

Each line should be a JSON object:

```json
{
  "timestamp": "2026-05-05T12:00:00+09:00",
  "cycle_id": "cyc_...",
  "level": "INFO",
  "component": "orchestrator",
  "event_type": "cycle_completed",
  "message": "Cycle completed successfully",
  "details": {}
}
```

## 15. Telegram Integration Design

Telegram is the approval, urgent notification, and limited operator UI channel.

Current components:

```text
approval/
├── manager.py
├── models.py
└── policies.py

integrations/telegram/
├── bot.py
├── formatter.py
└── handlers.py
```

Requirements:

- Whitelist allowed Telegram user IDs.
- Source shared Telegram chat/user IDs from the Maestro operator environment
  (`MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS` and
  `MAESTRO_TELEGRAM_WHITELISTED_USER_IDS`) when a Telegram approval config does
  not provide explicit overrides.
- Send order proposal messages.
- Support approve/reject/detail inline buttons.
- Expire proposals after timeout.
- Log all decisions.
- Keep the first implementation polling based; webhook handling remains
  deferred.
- Put command parsing behind the Telegram adapter boundary.
- Back `/help`, `/status`, `/health`, `/signal`, `/account`, `/portfolio`,
  `/apps`, `/orders`, and `/approvals` responses with Maestro state/read models
  and the latest stored broker snapshot.
- Support `/signal_<strategy>` commands through a separate signal config. These
  commands run one selected signal-enabled strategy, persist a signal package
  visible to Dashboard freshness/read models, and do not create approvals or
  submit broker orders.
- Require confirmation callbacks for `/pause` and `/kill_switch`.
- Persist Telegram command execution to audit/system events.
- Do not allow Telegram commands to submit or cancel broker orders, call KIS
  live read endpoints directly, disable risk limits, enable live mode, disable
  dry-run mode, change risk limits, resume, clear halted state, or trigger
  broker sync/reconciliation.

## 16. KIS Adapter Future Design

KIS adapter should be isolated under:

```text
execution/brokers/kis/
```

Components:

- `auth.py`: OAuth token and approval key management
- `client.py`: read-only client protocol
- `base.py`: shared REST plumbing for auth, headers, GET/POST, pagination, and
  symbol mapping
- `rest_client.py`: broker-product factory for domestic and overseas adapters
- `domestic_readonly.py` / `overseas_readonly.py`: product-specific balance,
  buying power, positions, order/fill inquiry, unfilled orders, and broker-side
  quote/reference lookup
- `domestic_live_order.py` / `overseas_live_order.py`: product-specific limit
  order submission, status, and verified cancellation paths
- `websocket.py`: real-time price/fill notification later
- `errors.py`: error code handling

The KIS adapter is a broker adapter, not a research data provider. It should mainly handle authentication, balances, positions, buying power, fills, and reconciliation. KIS current price lookup can produce `broker_quote` data for broker-side checks, but strategy plugins should receive research and market data through DataHub providers.

KIS accounts are credential/account boundaries. Domestic versus overseas routing
is decided by the target instrument's `broker_product`: KRX instruments use
`kis_domestic_stock`, while US-listed instruments use `kis_overseas_stock`.
Brokerage accounts that can trade both markets should enable both products, but
the product-specific adapters must still construct the KIS request for the
target market.

Live trading safety:

- Start with `live_readonly`.
- Then `live_approval`.
- Do not start with `live_auto`.
- Limit orders only initially.
- Small notional limits.
- Reconciliation required before live order submission.
- Unknown order status halts new orders.

## 17. Dashboard Future Design

Dashboard should be read-only by default.

Target dashboard model:

1. **Command Center**: the persistent top band. It should show System Verdict,
   immediate reason, operating posture, access posture, and KRW/USD Capital
   Summary before drill-down evidence.
2. **Overview**: the system map. It should show the ecosystem flow from
   Virtuoso proposals through Maestro validation, risk, approval/execution,
   state recording, broker reconciliation, and operator observation. Each node
   should be backed by persisted read models and should expose a current status
   such as fresh/stale, pass/fail/missing, attention required, or latest run.
3. **Operations**: safety, health, freshness, reconciliation, daily
   live-order usage, live-order lifecycle issues, recent risk/halt/failure
   events, and attention items. This is the place to answer whether the operator
   can trust the next cycle.
4. **Portfolio**: broker truth beside Maestro state, total equity,
   cash/exposure, account return/drawdown, KRW/USD sleeve returns, total
   portfolio return, strategy book return, and strategy contribution.
5. **Virtuoso**: configured strategy apps as proposal sources. Each app
   should show app concept, data needs, latest proposals/signals, validation and
   risk verdicts, and strategy-book performance without embedding
   strategy-specific logic in Maestro.
6. **Evidence**: run-level drill-down, strategy results, orders/proposals,
   approvals, broker snapshots, fill reconciliation, live-order lifecycle
   events, system events, CSV exports, and raw payload evidence.

The implemented Dashboard is a FastAPI read-only backend plus a built
Vite/React frontend served from the same process. It should stay organized
around the target model above instead of mirroring storage tables. Full daemon
API/WebSocket updates remain a later product architecture milestone after
Maestro has a daemon event model. Until then, the Dashboard must remain read-only
and must not become an approval, kill-switch, or configuration surface.

Product interaction model:

- Dashboard operator actions should keep global refresh and strategy signal
  generation separate. Global `Refresh` may update read-only broker/account
  snapshots and signal freshness status. The `Virtuoso` tab may expose per-app
  `Generate Signal` controls that run one selected app only, persist
  proposal-only signal artifacts, and never approve or execute orders.

- The global header area should show a System Verdict and Capital Summary. It
  should not duplicate every broker/account metric that appears later in the
  Investment Console.
- Verdict cards must include reason rows derived from persisted read models:
  attention items, freshness rows, health checks, reconciliation state, live
  order lifecycle issues, and FX freshness.
- Evidence-heavy tables and JSON payloads should be collapsed by default, with
  urgent attention queues opened by default. This keeps the Command Center
  readable while preserving full read-only drill-down and CSV export coverage.
- Money rendering must be explicit: amount plus currency for native values,
  and converted-total rows must include display currency, component values,
  FX status, FX source, rate, and timestamp when applicable.
- Tables are evidence. The primary user path should start with compact status,
  reason, and capital cards, then let the operator drill into filtered tables,
  run details, payloads, and CSV exports.

Current dashboard performance read models in `dashboard/read_models.py` cover
account, currency-sleeve, and total-portfolio views from persisted broker
snapshots and reconciliation events. The dashboard renders these persisted-state
read models, freshness labels, local filters, run drill-downs, and CSV exports
without calling KIS during page rendering. Performance tracking should continue
to extend that surface with:

The dashboard snapshot also exposes
`investment_console.performance_snapshot` as a versioned read-model contract
for backend/API consumers. It groups latest performance values, typed series,
FX metadata, quality reasons, and lineage while preserving the legacy table
arrays for compatibility.

- Persisted performance tables/views for KIS account equity, realized/unrealized
  PnL, daily return, cumulative return, and drawdown.
- Strategy-level PnL/return derived from Maestro proposal, order, fill,
  strategy book, and strategy run lineage.
- Currency-sleeve PnL/return for KRW and USD sleeves.
- Total portfolio PnL/return, with base-currency conversion only when an
  explicit FX source and timestamp are available.
- A dashboard display-currency toggle for KRW and USD total portfolio views.
  KRW is the default; USD is a reporting view. The toggle affects read models,
  charts, tables, and exports only.
- Reconciliation freshness markers so stale or unreconciled broker data is not
  shown as fresh broker truth.
- FX source, rate, timestamp, and stale/missing status for every converted
  total-performance view.

Performance read models should be computed from persisted KIS snapshots,
portfolio snapshots, broker reconciliation events, live order status/lifecycle
events, fill reconciliation events, strategy book snapshots, and strategy run
payloads. The implemented account, currency-sleeve, total-portfolio, and
strategy book views are computed on read from those persisted snapshots/events;
strategy attribution rows include a `lineage` object that ties each strategy
book performance row back to same-run strategy proposals, allocation symbols,
orders, and fill-reconciliation events where those records exist. Account
bucket attribution snapshots track `account_id + symbol + bucket_id + quantity`
for strategy/manual books. Maestro-authored fills are assigned to their strategy
bucket; broker snapshot deltas outside Maestro orders are attributed to
`manual` first, then to strategy buckets with warning events if manual quantity
is insufficient. Strategy book performance still uses strategy book snapshots
as the primary value series, while account bucket attribution supplies
operator-facing actual weights and scoped order-generation inventory.

System event read models use a centralized required-field contract from
`maestro.state.events`. Dashboard event rows expose `schema_status`,
`required_fields`, and `missing_required_fields` so persisted evidence can be
audited without failing old/custom events at read time. Unknown event types are
reported as untracked rather than invalid.

Virtuoso app performance now exposes a per-app `performance_snapshot` under
`dashboard_snapshot.virtuoso_apps.strategies[*]` for strategies present in the
active operator config. Historical strategy IDs from old runs remain evidence
rows in audit/read models but are not listed as current Virtuoso apps. The
read model uses
`strategy_book_snapshots` as the app value series and explicit
`strategy_cash_flow` system events as the authoritative app-level deposit and
withdrawal ledger. TWR is the primary return metric; net PnL is computed as
ending value minus starting value minus cumulative cash flow; drawdown uses app
value peaks; and MWR/IRR is computed from irregular dated cash flows plus the
ending app value.

Telegram funding attribution records `strategy_cash_flow` for
strategy-requested deposits after the operator confirms funding. Voluntary
account cash increases detected from broker snapshots create a
`strategy_cash_flow_proposal` with target-weight allocations for active
strategies in the account; approval records one `strategy_cash_flow` event per
allocation and a `strategy_cash_flow_proposal_ack`. Broker snapshot cash-flow
fields remain account-level reference data until this explicit attribution
event exists.

Dashboard freshness rows expose their policy metadata. Configured checks with
no row are `missing`; disabled checks are `not_configured`; rows with invalid
or over-limit timestamps are `stale`; and failed reconciliation rows remain
`failed` even when old so the operator does not lose the failure cause.

dedicated persisted performance tables remain pending. The dashboard must not
call KIS or FX endpoints directly.
FX conversion is reporting-only and must not feed order generation,
buying-power checks, reconciliation cash gates, or broker risk gates. Strategy
attribution remains auditable: dashboard strategy attribution uses persisted
strategy book snapshots and lineage, while account bucket attribution uses
persisted account attribution snapshots where available.

Account performance v1 is a read model computed from persisted broker account
snapshots and broker reconciliation events. It exposes account value, cash,
positions market value, realized/unrealized PnL when present, period return,
daily return, cumulative return, drawdown, and reconciliation status. It does
not persist a new broker-order path and does not call KIS during dashboard
rendering.

Currency-sleeve performance uses the same persisted broker snapshots grouped by
snapshot currency, so KRW and USD returns remain separate. Total portfolio
performance groups broker snapshots by run/as-of time; when more than one
currency is present and no explicit FX source/timestamp exists, the row exposes
component values and `missing_fx=true` instead of computing a base-currency
return. With fresh FX, the read model may compute KRW-display or USD-display
total performance by converting only the non-display currency components. It
should preserve local sleeve return and expose the FX effect separately so users
can distinguish investment performance from currency movement. FX rates are
refreshed by the reporting-only FX service from ExchangeRate-API in v1, persisted
as `fx_rate_snapshot` system events with `source`, `as_of`, `fetched_at`,
`max_age_seconds`, and `rates` entries such as `USD/KRW`; stale or missing FX
disables converted total returns. `as_of` records the provider's rate timestamp,
while dashboard freshness is measured from `fetched_at` when available. Provider
requests are budget-throttled by `refresh_interval_seconds` separately from the
dashboard freshness threshold; the default one-hour interval caps normal
automated usage at about 744 requests in a 31-day month, while `maestro
fx-refresh --force` is reserved for explicit operator checks.

Security:

- Prefer localhost/Tailscale access.
- Do not expose public internet without auth/reverse proxy.
- No secrets displayed.
- Refresh and CSV download are local UI actions only.
- No state-changing write endpoints in early versions.

## 18. Testing Requirements

v0.1 tests:

- Config loads and validates.
- Plugin loader imports sample plugin.
- Sample plugin returns valid manifest and result.
- MockDataHub returns DataBundle.
- Strategy result validation catches invalid allocations.
- PortfolioManager combines allocations correctly.
- RiskManager rejects invalid targets.
- PaperExecutionEngine updates state.
- `run_once()` integration test completes and writes logs.

## 19. Coding Standards

- Type hints required.
- Prefer Pydantic models for external contracts.
- Avoid strategy-specific logic in Maestro core.
- Keep SDK imports stable.
- Avoid secrets in logs.
- Keep functions small and testable.
- Use clear engineering names in code; music metaphors are okay in docs but should not obscure class responsibilities.

## 20. Deployment Requirements

Early local development:

```bash
uv sync
uv pip install -e .
uv pip install -e examples/sample_static_allocation
maestro run-once --config configs/paper.yaml
```

Future VPS deployment:

- Ubuntu VPS
- `uv` environment
- systemd service
- SQLite state DB
- JSONL logs
- Tailscale for dashboard access
- Telegram bot polling or webhook
- KIS and FX provider secrets via `/etc/maestro/maestro.env` or a secret manager
