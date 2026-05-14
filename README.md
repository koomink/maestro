# Maestro

Maestro is a standalone, strategy-agnostic portfolio operating system for automated investment management.

It is part of the broader **Symphony** ecosystem.

```text
Symphony = ecosystem
Maestro = portfolio operating system
Virtuoso apps = strategy plugins/apps
```

## Concept

Maestro is not a trading strategy. It is the operating system that manages strategy apps.

```text
Virtuoso apps propose.
Maestro decides.
Maestro protects.
Maestro executes.
Maestro records.
```

Virtuoso apps generate investment proposals such as target allocations. Maestro validates those proposals, combines them into a portfolio target, applies risk controls, generates orders, executes through paper or broker engines, updates state, and records everything.

## Why Maestro Exists

A serious automated portfolio system needs more than strategy logic. It needs:

- Data governance
- Strategy plugin management
- Portfolio construction
- Risk control
- Approval workflows
- Broker execution
- State management
- Reconciliation
- Audit logs
- Monitoring
- Dashboard visibility

Maestro aims to provide this infrastructure while keeping strategy logic external and modular.

## System Model

```text
Symphony Ecosystem
│
├── Maestro OS
│   ├── DataHub
│   │   └── Provider adapters
│   ├── Plugin Manager
│   ├── Strategy Orchestrator
│   ├── Signal Validator
│   ├── Portfolio Manager
│   ├── Risk Manager
│   ├── Approval Manager
│   ├── Execution Engine
│   │   ├── PaperExecutionEngine
│   │   └── Live approval lifecycle
│   ├── Broker Adapters
│   │   └── KIS adapter
│   ├── State Manager
│   ├── Audit Logger
│   ├── Dashboard / API
│   └── Notification Adapters
│       └── Telegram adapter
│
├── Virtuoso Apps
│   ├── Ataraxia
│   ├── TechAgora
│   ├── Macro
│   ├── Fundamental
│   └── TradingAgents
│
└── External Systems
    ├── Yahoo/yfinance
    ├── FRED
    ├── RSS feeds
    ├── Korea Investment Securities Open API
    └── Telegram Bot API
```

Maestro internal modules include DataHub, provider adapters, the execution
engine, broker adapters, approval manager, risk manager, state store, audit
logger, and dashboard. Virtuoso apps are external strategy plugins/apps loaded
through the Maestro SDK contract. External systems are services Maestro may call
through internal adapters; they are not Maestro modules.

## Data and Execution Boundaries

Maestro separates research/market data from broker account and execution data.

```text
External market/research data
Yahoo/yfinance, FRED, RSS feeds, CSV/local files,
GDELT/NewsAPI, and future community APIs
        │
        ▼
Maestro DataHub provider adapters
        │
        ▼
Maestro DataHub
        │
        ▼
Virtuoso strategy plugins
        │
        ▼
Portfolio / risk / approval
        │
        ▼
Maestro execution engine
        │
        ▼
Maestro broker adapters, including KIS
auth, balances, positions, buying power, orders, fills, reconciliation,
and broker-side quote/reference data for execution checks
        │
        ▼
External broker APIs, including KIS Open API
```

DataHub is the Maestro market and research data layer. Its providers may call
external systems such as Yahoo/yfinance, FRED, RSS feeds, or local CSV files.
Broker adapters are the Maestro account, execution, and reconciliation layer.
Execution is internal to Maestro: paper execution is simulated inside Maestro,
while live approval execution goes through broker adapters.

Strategy plugins must request data through Maestro DataHub and must not call
external market, macro, news, sentiment, Telegram, or broker APIs directly.

Korea Investment Securities current price lookup may be used as broker-side quote/reference data for execution validation or reconciliation, but KIS is not the primary strategy or research data source.

## Project Status

Maestro package metadata is still `0.1.1` until an explicit release/versioning
policy is adopted. Capability work has advanced through the documented v0.8.x
DataHub, risk, recovery, and operations hardening path, plus the v1.0 private
approval-gated beta readiness checks in [docs/ROADMAP.md](docs/ROADMAP.md).
It is not an autonomous production trading system.

Current runnable modes are:

- Mock paper mode with `configs/paper.yaml`
- CSV-backed paper mode with `configs/csv_paper.yaml`
- Optional external DataHub providers such as Yahoo/yfinance, FRED, and RSS when
  explicitly configured
- Yahoo/yfinance-backed fundamentals, financial statements, and OHLCV-derived
  technical indicators for advanced LLM trading agents
- Mock `live_readonly` broker scaffolding with `configs/live_readonly.yaml`
- Live approval infrastructure and safety gates, with KIS domestic and overseas
  read-only plus approval-gated stock/ETF limit-order submit/status adapter
  support

For a single-user operator workflow, start with
[docs/personal_operator_mvp.md](docs/personal_operator_mvp.md):

```bash
maestro init-personal --output ~/maestro-operator/maestro_personal.yaml
maestro operator-evidence --config ~/maestro-operator/maestro_personal.yaml --output ~/maestro-operator/evidence-before.json
maestro personal-check --config ~/maestro-operator/maestro_personal.yaml
```

`PaperExecutionEngine` is simulated execution inside Maestro. Mock configs are
for development and tests, not production readiness.

Official v0.1 release scope:

- Paper mode only
- MockDataHub only
- TargetAllocationResult only
- External plugin loading
- Simple portfolio construction
- Simple risk management
- Paper execution
- SQLite state store
- JSONL audit logs
- CLI `run-once`
- CLI `status`

v0.1.1 stabilization adds:

- Semantic ID prefixes for runs, paper orders, and approval requests
- Explicit unsupported execution engine errors
- Explicit missing price errors
- Strict config validation for unknown YAML fields
- Failure audit events with exception type, message, and traceback summary
- Focused regression tests for the stabilization behavior

v0.2 DataHub and dashboard foundation adds:

- Explicit DataHub symbol payloads with `latest_price`, historical `bars`, stale flags, and warnings
- CSV OHLCV validation and timestamp parsing
- RiskDecision persistence for dashboard visibility
- SQLite timeout/busy timeout/WAL settings for dashboard + CLI coexistence
- Dashboard read models and clearer read-only dashboard tables

v0.3 DataHub provider work adds:

- Optional Yahoo/yfinance-style `price`, `ohlcv`, `fundamental`, and
  `financial_statements` provider behind Maestro DataHub
- Native `technical_indicators` support derived from OHLCV bars, covering RSI,
  MACD, SMA, EMA, and Bollinger Bands for LLM trading agents such as
  TradingAgents, Vibe-Trading, and QuantAgent
- FRED `macro` provider behind Maestro DataHub, using API keys from environment variables
- RSS `news` provider behind Maestro DataHub
- Network-free rule-based `sentiment` provider over configured fixture/news text
- Multi-provider DataHub routing with deterministic priority and fallback behavior
- Skipped-by-default live-network smoke tests for Yahoo/yfinance, FRED, and RSS
- Fake-client and fixture-backed provider tests; normal tests do not call live external services

Crypto market data is deferred because the current supported universe is stocks
and ETFs only.

Implemented foundations beyond the core v0.1 scope:

- CSVDataProvider for simple historical data loading
- Optional Streamlit read-only dashboard
- Approval request/decision gate before paper fills
- Telegram approval message formatter and notifier stub
- Telegram Bot API polling approval MVP for paper and live approval modes
- Telegram Bot API live order lifecycle notification adapter
- CLI `approvals`
- `live_readonly` mode config
- KIS read-only adapter interface and deterministic mock client
- KIS REST client foundation for auth and product-specific broker adapter paths,
  including domestic and overseas stock/ETF read-only plus approval-gated
  limit-order submit/status payloads
- CLI `kis-sync` and `kis-account`
- Product/venue-aware universe config for canonical symbols, broker products,
  exchange codes, currency, and precision rules
- Dynamic-universe candidate evaluation in `run_once` for SDK plugins that
  explicitly opt in through `supports_dynamic_universe`
- Live approval order safety contract and explicit KIS domestic/overseas adapter
  split behind that contract
- `run_once` live approval wiring through the live order lifecycle service when
  `mode=live_approval`
- Structural refactor boundaries for system events, live execution gates,
  live-order services, KIS transport/parser/product adapters, config models,
  health checks, and operational preflight helpers
- Persistent safety controls for active, paused, killed, and halted state
- CLI `safety-status`, `pause`, `resume`, and `kill-switch`
- Personal operator config and readiness commands: `init-personal` and
  `personal-check`
- Safe live approval example config:
  [configs/live_approval.example.yaml](configs/live_approval.example.yaml)
- v0.6 release checklist:
  [docs/live_approval_release_checklist.md](docs/live_approval_release_checklist.md)

Deferred real integrations:

- No live auto-trading
- No Telegram webhook
- No direct or unguarded KIS buy/sell/order CLI
- No direct cancel CLI; KIS overseas cancel is available only behind
  `LiveOrderCancellationService` policy gates
- No market orders
- No community sentiment APIs yet
- No crypto market data while the supported universe is stocks and ETFs only
- No web dashboard write controls

## Live Approval Safety

Global safety state is persisted in SQLite as `safety_state` system events and
mirrored to the JSONL audit log. The default state is `active`. Operators can
inspect and change state with CLI commands:

```bash
maestro safety-status --config configs/paper.yaml
maestro pause --config configs/live_approval.example.yaml --reason "operator maintenance"
maestro resume --config configs/live_approval.example.yaml --reason "checks passed"
maestro clear-halt --config configs/live_approval.example.yaml --reason "root cause fixed"
maestro kill-switch --config configs/live_approval.example.yaml --reason "emergency stop"
```

`paused`, `killed`, and `halted` block `live_approval` order submission before
approval or lifecycle execution and record `safety_execution_blocked` events.
Paper mode currently records a `safety_gate_warning` and continues so local
simulation remains usable. The kill switch is intentionally not reset by
`resume` or `clear-halt`. A halted state can be cleared only with
`clear-halt --reason` after the operator reviews state, audit events, broker
account status, and the specific halt cause.

v0.6 starts `live_approval` infrastructure, not `live_auto`. Live order submission
is disabled by default and is available only through the safety interface. The
contract requires a Telegram approval decision, the latest broker reconciliation
to pass within `reconciliation.max_age_seconds`, fresh required DataHub price
data, limit orders only, per-order and daily notional/count caps,
instrument-aware quantity/price/minimum validation, duplicate-order prevention,
persisted live order status snapshots, and halt-on-unknown broker/order state
behavior. Normal tests use fake clients and do not call KIS or Telegram network
endpoints.

Maestro core uses canonical symbols and should remain broker/product agnostic.
`universe.instruments` describes each tradable symbol's asset type, market
region, currency, broker product, broker symbol, exchange code, price tick, and
quantity step. Broker adapters translate canonical symbols into product-specific
request fields. Maestro supports KIS domestic and overseas stock/ETF products as
explicit broker adapter paths. Single-product configs continue to use
`kis.broker_product`. Multi-product operator configs use `kis.broker_products`
and `portfolio.allocation_mode=currency_sleeves` so KRW and USD sleeves
rebalance independently without automatic FX conversion or cross-currency
orders.

Current example configs use static `portfolio.allowed_symbols` and
`universe.instruments` lists such as `AAPL`, `MSFT`, `VOO`, `QQQ`, and `SGOV`.
Those lists are tutorial, test, and conservative paper-mode examples, not a
product limit. The intended production design is a policy-based dynamic universe:
Virtuoso apps may propose candidate symbols and data needs, but Maestro validates,
resolves, and approves tradability before any symbol can receive an allocation or
reach execution.

Maestro distinguishes a broad research universe from a stricter tradable
universe. Research symbols can include analysis inputs such as `SPY`, `VIX`,
`DXY`, FRED macro series, news keywords, or other non-tradable references.
Tradable symbols must pass Maestro-side instrument metadata resolution, DataHub
availability and freshness checks, broker product mapping, broker tradability
checks when required, and risk/safety policy. Virtuoso apps can propose
candidates, but they cannot approve tradability, call broker APIs, submit orders,
or allocate to research-only symbols.

Dynamic-universe flow:

1. A Virtuoso app declares data needs and candidate symbols through the SDK.
2. Maestro validates candidates against `UniversePolicy`.
3. Maestro resolves metadata through an `InstrumentResolver`.
4. Maestro checks DataHub availability and freshness.
5. For `intended_use: tradable`, Maestro checks broker mapping and tradability.
6. Approved candidates become temporary or persistent tradable universe entries.
7. `TargetAllocationResult` allocations are accepted only for approved tradable
   symbols.

The default `UniversePolicy` is intentionally conservative: US stock/ETF
candidates only, USD only, `kis_overseas_stock`, NASD/NYSE/AMEX, one new
tradable symbol per run, operator approval required, and broker tradability plus
DataHub freshness checks required. Denied symbols and denied asset tags can be
configured for products such as leveraged, inverse, OTC, options, or futures.

Daily loss limits are enforced from normalized broker PnL fields in the latest
broker snapshot. Maestro first uses account-level `daily_pnl`/`today_pnl` style
fields when present, then falls back to summed position `unrealized_pnl`.

For real-account rehearsals, `execution.require_broker_risk_validation=true`
adds a broker-snapshot risk gate before approval submission. It checks settled
buying power with `execution.live_order_fee_buffer_pct`, post-order cash reserve,
per-symbol exposure, portfolio exposure, pending broker orders, and whether the
latest broker snapshot is the one that passed reconciliation. For KIS overseas
buy orders, the live-order adapter also rechecks `/inquire-psamount` with the
actual limit price immediately before broker submit and rejects the order if KIS
reports insufficient buying power or max buy quantity.

Partial and full fill reconciliation reads `live_order_status` snapshots,
applies only newly recognized cumulative fill deltas to Maestro portfolio state,
and records `fill_reconciliation` system and audit events. Rejected, canceled,
halted, and unknown statuses do not update the portfolio.

Cancellation is available only behind `LiveOrderCancellationService`. A cancel
request requires Telegram approval, the latest broker reconciliation to pass, and
the latest order status to be `open` or `partially_filled`. Partial-fill
cancellation is allowed only for the remaining open quantity after fill
reconciliation has been recorded. Filled, rejected, canceled, halted, and
unknown orders cannot be canceled; unknown state halts the path instead of
attempting cancel. There is still no direct cancel CLI.

`LiveOrderWorkflowService` composes the safe pieces for one approval-gated
post-order workflow: submit through the safety service, stop on submit halt, poll
broker status, reconcile fills, optionally run broker reconciliation, and persist
a `live_order_workflow` summary with status, broker order ID, applied fills,
reconciliation result, and halt/failure reason.

`LiveOrderLifecycleService` extends that into a bounded multi-poll loop. It polls
until a terminal status or max polls, reconciles fills after every poll, runs
broker reconciliation after fill updates when configured, emits operator
notifications through `LiveOrderNotificationClient`, and records a
`live_order_lifecycle` summary. Max polls do not auto-cancel.

`MaestroOrchestrator.run_once()` remains paper-mode by default, but in
`live_approval` mode it uses the existing proposal and approval path, converts
approved proposed orders into limit-order `LiveOrderRequest` objects, and runs
the bounded lifecycle service. This is product-level wiring for
approval-gated live orders, not live automation.

The dashboard remains read-only. Telegram approval, lifecycle notifications,
and the constrained Telegram operator UI remain available. Telegram resume,
clear-halt, live enablement, direct trading, and risk changes remain deferred;
`/pause` and `/kill_switch` are the only Telegram safety controls.

Use [configs/live_approval.example.yaml](configs/live_approval.example.yaml) as
the safe-by-default operator template and follow
[docs/live_approval_release_checklist.md](docs/live_approval_release_checklist.md)
before enabling live order submission.

Safe execution config defaults:

```yaml
execution:
  engine: paper
  live_order_enabled: false
  require_reconciliation_pass: true
  max_live_order_notional: 0
  max_daily_live_notional: 0
  max_daily_live_order_count: 0
  daily_loss_limit: null
  allowed_order_type: limit
  order_status_poll_interval_seconds: 30
  order_status_max_polls: 20
  order_status_terminal_timeout_seconds: 1800
```

Package version metadata remains `0.1.1` for now. The v0.2 through v0.6 labels
are roadmap capability milestones in this repository, not published Python
package release tags. A package version bump should happen with an explicit
release/versioning policy and lockfile update.

## Optional Yahoo/yfinance Provider

The Yahoo/yfinance provider is optional and is not required for core Maestro
usage. When configured, it supports price history, fundamentals, financial
statements, and OHLCV-derived technical indicators behind Maestro DataHub:

```bash
pip install "maestro[yahoo]"
```

Local development with `uv`:

```bash
uv sync --extra yahoo
```

Example DataHub config:

```yaml
datahub:
  provider: yahoo
  timeout_seconds: 5
  stale_after_seconds: 86400
  symbol_map:
    SAMSUNG: 005930.KS
```

For multiple providers, use `datahub.providers` with lower `priority` values
preferred first. Strategy plugins still request data through Maestro DataHub and
do not call yfinance directly.

Example LLM-agent requests:

```python
DataRequest(
    symbol="AAPL",
    asset_type="stock",
    data_type="financial_statements",
    statement_type="income_statement",
)
DataRequest(
    symbol="AAPL",
    asset_type="stock",
    data_type="technical_indicators",
    indicator="macd",
    lookback=30,
)
```

## FRED Macro Provider

The FRED provider uses stdlib HTTP and does not add a package dependency. Store
the API key in an environment variable and reference only the variable name in
config:

```yaml
datahub:
  provider: fred
  api_key_env: FRED_API_KEY
  timeout_seconds: 5
  stale_after_seconds: 7776000
  symbol_map:
    REAL_GDP: GDPC1
```

Strategy plugins still request macro data through Maestro DataHub and do not
call FRED directly.

## RSS News Provider

The RSS provider uses stdlib HTTP/XML parsing. It can fetch live RSS feeds when
configured with `feed_urls`, while normal tests use fake clients and fixture XML:

```yaml
datahub:
  provider: rss
  feed_urls:
    - https://example.com/rss
  timeout_seconds: 5
  stale_after_seconds: 604800
```

Live RSS checks are optional and skipped by default unless
`MAESTRO_RUN_RSS_INTEGRATION=1` is set.

## Rule-based Sentiment Provider

The first sentiment provider is network-free and analyzes configured
fixture/news text:

```yaml
datahub:
  provider: sentiment
  sentiment_texts:
    - SPY posts strong gains as confidence improves.
  symbol_map:
    SPY: SPY
  source_name: fixture_news
```

Reddit, X/Twitter, Discord, Telegram, and paid sentiment APIs remain future
provider work.

## v0.1 Success Criteria

The following command should complete one full operating cycle:

```bash
maestro run-once --config configs/paper.yaml
```

Expected cycle:

```text
Config loaded
→ Sample strategy plugin loaded
→ Mock data returned
→ Strategy executed
→ Target allocation result validated
→ Portfolio target built
→ Risk constraints applied
→ Paper orders created and filled
→ State updated
→ Audit log written
```

## Repository Layout

```text
maestro/
├── README.md
├── docs/
│   ├── PRD.md
│   ├── TRD.md
│   ├── Implementation_plan.md
│   ├── ROADMAP.md
│   └── TASKS.md
├── pyproject.toml
├── .env.example
├── configs/
│   └── paper.yaml
├── src/
│   └── maestro/
│       ├── sdk/
│       ├── core/
│       ├── config/
│       ├── plugins/
│       ├── datahub/
│       ├── orchestration/
│       ├── signals/
│       ├── portfolio/
│       ├── risk/
│       ├── execution/
│       ├── state/
│       ├── monitoring/
│       ├── approval/
│       ├── integrations/
│       ├── dashboard/
│       └── cli.py
├── examples/
│   └── sample_static_allocation/
└── tests/
```

## Sample Strategy Plugin

The sample strategy lives under:

```text
examples/sample_static_allocation/
```

It is intentionally structured as an independent Python package. This allows it to become a real Virtuoso app later with minimal changes.

The sample strategy should import only from `maestro.sdk`:

```python
from maestro.sdk import (
    BaseStrategyPlugin,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
```

This keeps strategy apps independent from Maestro internals.

To start a new external Virtuoso app repository, generate a wrapper scaffold:

```bash
maestro init-virtuoso-app \
  --output ./my_app \
  --package-name my_virtuoso_app \
  --class-name MyVirtuosoStrategy
```

Strategy-specific wrapper code belongs in that app repository. Maestro core
owns only the SDK contract, loader, and scaffold; it should not gain
strategy-by-strategy adapter modules.

Virtuoso apps may return `TargetAllocationResult` directly, or return
`StrategySignalResult` when their strategy config includes an explicit
`signal_to_allocation` policy. Maestro normalizes signals to target allocations
before validation, risk, approval, and execution.

## Plugin Loading

Strategies are configured by entrypoint string:

```yaml
strategies:
  - id: sample_static_allocation
    enabled: true
    mode: paper
    weight: 1.0
    entrypoint: "sample_static_allocation.strategy:SampleStaticAllocationStrategy"
```

Later, a real Virtuoso package can be installed and configured similarly:

```yaml
strategies:
  - id: ataraxia
    enabled: true
    mode: paper
    weight: 1.0
    entrypoint: "virtuoso_ataraxia.strategy:AtaraxiaStrategy"
```

Maestro core should not change when strategies are added or removed.

## Installation: Development

Recommended flow:

```bash
uv sync
uv pip install -e .
uv pip install -e examples/sample_static_allocation
```

Development checks:

```bash
ruff check .
ruff format --check .
pytest -q
```

Fresh clone v0.1 verification:

```bash
uv sync --extra dev
uv pip install -e examples/sample_static_allocation
maestro run-once --config configs/paper.yaml
ruff check .
ruff format --check .
pytest -q
```

Then run:

```bash
maestro run-once --config configs/paper.yaml
```

To run the same pipeline using CSV-backed sample prices:

```bash
maestro run-once --config configs/csv_paper.yaml
```

To inspect current state from SQLite:

```bash
maestro status --config configs/paper.yaml
```

To run real-data US stock/ETF paper mode:

```bash
uv sync --extra yahoo
maestro run-once --config configs/us_etf_yahoo_paper.yaml
maestro status --config configs/us_etf_yahoo_paper.yaml
```

`configs/us_etf_yahoo_paper.yaml` uses canonical USD symbols
`CASH_USD`, `VOO`, `QQQ`, and `SGOV` in the sample allocation, with AAPL and
MSFT included in the static example US universe. Yahoo/yfinance supplies external
market data through Maestro DataHub. The fixed symbol list is intentionally small
for a runnable paper example; production live flows should use policy-based
candidate validation before adding new tradable symbols. Execution remains
simulated inside
`PaperExecutionEngine`; this path does not call KIS, does not submit live
orders, and does not enable live trading.

To run the paper pipeline with the approval gate enabled:

```bash
maestro run-once --config configs/approval_paper.yaml
maestro approvals --config configs/approval_paper.yaml
```

By default, `configs/approval_paper.yaml` uses the no-network `console`
approval stub and records the configured decision. For the v0.4 Telegram MVP,
start from `configs/telegram_approval_paper.yaml`, set
`TELEGRAM_BOT_TOKEN` in the environment, configure
`telegram_allowed_chat_ids` and `whitelisted_user_ids`, and keep `mode: paper`.
Maestro sends the order proposal through the Bot API and `run-once` blocks while
polling for inline approve/reject button callbacks. Manual typed
`approve <approval_id>` / `reject <approval_id>` replies are ignored. Webhooks,
dashboard write controls, and live auto trading remain deferred. Normal tests
use fake Telegram clients and do not call the Telegram network.
Telegram approval messages include the source strategy IDs and per-order market,
exchange, ticker/code, instrument name, quantity, price, currency, and notional
so operators can inspect the proposal before approving it.

To run the KIS read-only adapter:

```bash
maestro kis-sync --config configs/live_readonly.yaml
maestro kis-account --config configs/live_readonly.yaml
maestro adopt-broker-snapshot --config configs/live_readonly.yaml --reason "operator baseline accepted"
maestro reconcile --config configs/live_readonly.yaml
maestro reconcile-fills --config configs/live_readonly.yaml
maestro recover-live-order --config configs/live_readonly.yaml --reason "broker truth reconciled"
maestro heartbeat --config configs/live_readonly.yaml
maestro ops-alerts --config configs/live_approval.example.yaml --allow-mock
maestro beta-preflight --config configs/live_approval.example.yaml
maestro health --config configs/live_readonly.yaml
```

`recover-live-order` is an operator recovery marker, not a broker action. It
requires a latest broker snapshot and a passing broker reconciliation, reruns
fill reconciliation, records `live_order_recovery_completed`, and allows future
live approval proposals after `live_order_recovery_required` or incomplete
lifecycle state has blocked them.

`adopt-broker-snapshot` is also a state-only operator action. It copies the
latest read-only broker snapshot into Maestro's portfolio state after the
operator has accepted that snapshot as the rehearsal baseline, records
`broker_snapshot_adopted`, and refuses broker positions outside
`portfolio.allowed_symbols`.

`heartbeat` records `maestro_heartbeat` for operator schedulers. When
`execution.heartbeat_max_age_seconds` or
`execution.scheduled_run_max_age_seconds` are set in an operator config,
`maestro health` fails on missed heartbeat or missed scheduled `run-once`.
`ops-alerts` sends current health warnings/failures to configured Telegram
approval chats; `--allow-mock` validates the escalation path without network.
Audit JSONL entries include a hash chain, and health verifies audit integrity.
`beta-preflight` is the private production-beta gate. It requires live approval
mode, real KIS provider, Telegram approval, fresh broker snapshot, passing
reconciliation, audit integrity, market/session/quote/risk gates, daily loss
limit, heartbeat monitoring, and scheduled-run monitoring.
`operator-evidence` is a read-only evidence snapshot. It summarizes readiness
stages, health checks, private-beta failures, latest broker/reconciliation
state, latest approvals, proposal snapshots, dry-run events, lifecycle events,
fill reconciliation, and recovery markers without calling KIS, sending
Telegram messages, submitting broker orders, or running strategies.

For an operator-local real KIS read-only rehearsal:

```bash
maestro live-smoke --config <operator-readonly-config> --check kis-readonly
```

For an operator-local Telegram approval channel rehearsal:

```bash
maestro live-smoke --config <operator-live-approval-config> --check telegram-approval
```

For an approval-gated dry-run rehearsal that records `live_order_dry_run` events
without broker submission:

```bash
maestro live-smoke --config <operator-live-approval-config> --check live-dry-run
maestro operator-evidence --config <operator-live-approval-config> --output <operator-evidence-after.json>
```

The matching pytest smokes are skipped by default and run only when
`MAESTRO_RUN_KIS_LIVE_SMOKE=1` / `MAESTRO_KIS_LIVE_CONFIG` or
`MAESTRO_RUN_TELEGRAM_LIVE_SMOKE=1` / `MAESTRO_TELEGRAM_LIVE_CONFIG` or
`MAESTRO_RUN_LIVE_DRY_RUN_SMOKE=1` / `MAESTRO_LIVE_DRY_RUN_CONFIG` point to
operator-local configs.

`configs/live_readonly.yaml` uses the deterministic no-network mock provider.
`configs/kis_overseas_readonly.example.yaml` documents the real KIS overseas
stock/ETF read-only shape with env var names only. The `kis_overseas_stock`
read-only adapter now covers verified overseas account reads for USD/foreign
cash, positions, buying power, fills, unfilled orders, broker snapshot
normalization, and reconciliation against Maestro canonical US stock/ETF
symbols. It uses these environment variable names:

- `KIS_ACCOUNT_ID`: KIS account number and product code
- `KIS_APP_KEY`: KIS app key
- `KIS_APP_SECRET`: KIS app secret
- `KIS_ACCESS_TOKEN`: optional pre-issued access token; leave unset unless it is
  a real current token
- `KIS_APPROVAL_KEY`: optional pre-issued WebSocket approval key; leave unset
  unless it is a real current key

Maestro CLI commands load `.env` from the current working directory when the
file exists and do not override variables already set by the shell. For local
operator rehearsals, copy `.env.example` to `.env`, fill the KIS and Telegram
values, and run Maestro from the repository root.

The `live_readonly` adapter is read-only. It does not submit, cancel, amend, buy,
sell, enable `live_auto`, or add market orders. Normal tests use fake/fixture KIS
responses and do not call KIS network endpoints.

If `KIS_ACCESS_TOKEN` is unset, Maestro can issue `/oauth2/tokenP` and can
persist the access token when `kis.token_cache_path` is configured. The cache
file is written with owner-only permissions. Access tokens may be stored only in
`kis.token_cache_path`; they must never be written to state, audit logs,
dashboard rows, or test fixtures. App secrets follow the same no-persistence
rule.

For future KIS WebSocket use, Maestro can issue `/oauth2/Approval` when
`KIS_APPROVAL_KEY` is unset. The request uses `grant_type`, `appkey`, and
`secretkey` as defined in the KIS OAuth workbook. The returned approval key is
treated as a secret and must not be written to state, audit logs, dashboard rows,
or test fixtures.

The KIS REST layer is split by broker product. `kis_domestic_stock` covers KRX
stock/ETF account, quote, order/fill, buying-power, and cash limit-order paths.
`kis_overseas_stock` covers US-listed stock/ETF account, quote, order/fill,
buying-power, and limit-order paths. Multi-product configs route each order by
the instrument's `broker_product` behind the existing approval, reconciliation,
safety, and daily-limit gates, based on Korea Investment Securities OpenAPI
examples. Overseas status lookup uses the broker order submission timestamp to
query the relevant US exchange-local date range, avoiding a Korea/US
date-boundary miss.
The overseas cancel adapter is available only behind
`LiveOrderCancellationService` policy gates after Telegram approval, latest safe
order status, and reconciliation checks. There is no direct buy/sell/cancel CLI,
no market order path, and no normal test that calls the KIS network.

`maestro health --config ...` performs local operational checks without live KIS
network calls. `maestro live-preflight --config ...` prints only the live
approval preflight result and exits nonzero when the live approval safety
configuration fails. Health checks cover config loading, SQLite state, audit
path, safety state, recent halt/failure events, DataHub config, KIS env var
presence, token cache path, live approval preflight configuration, latest broker
snapshot age, and latest reconciliation status. Missing KIS env vars or broker
snapshots are reported without printing secret values.

For live approval rehearsal, set `execution.live_order_dry_run=true` in an
operator-local config. `run-once` still performs strategy, risk, reconciliation,
and approval work, then writes `live_order_dry_run` events instead of calling the
broker submit adapter.

Operational docs:

- [Deployment guide](docs/deployment.md)
- [KIS fixture redaction](docs/kis_fixture_redaction.md)
- [Live account promotion](docs/live_account_promotion.md)
- [Personal operator MVP](docs/personal_operator_mvp.md)
- [Virtuoso apps](docs/virtuoso_apps.md)
- [VPS/systemd guide](docs/vps_systemd.md)
- [Backup/restore guide](docs/backup_restore.md)
- [Operator runbook](docs/operator_runbook.md)

To install dashboard dependencies and open the read-only dashboard:

```bash
uv sync --extra dashboard
maestro dashboard --config configs/kis_overseas_readonly.example.yaml
```

The dashboard is read-only. It shows portfolio state, broker account exposure,
snapshot history, strategy/order/approval tables, safety state, health summary,
latest reconciliation status, halt/failure events, live order status/lifecycle
events, fill reconciliation events, and daily live order count/notional usage
when those events exist. The Operations tab includes an operator summary with
attention items for halted safety state, degraded health, failed reconciliation,
daily live-order limit usage, and recent live-order lifecycle issues. Strategy
rows also expose normalized allocation results and preserved source-signal
fields when strategy plugins return signal results.
The Performance tab shows broker-snapshot account value, period return,
cumulative return, drawdown, and reconciliation labeling from persisted state.
It also keeps KRW/USD currency-sleeve returns separate and marks mixed-currency
total portfolio rows as requiring an explicit FX source before base-currency
returns are shown.
Refresh and CSV download are local UI actions only; the dashboard does not call
live KIS endpoints and does not expose state-changing write controls.

If no CLI entrypoint exists yet during early development, use:

```bash
python -m maestro.cli run-once --config configs/paper.yaml
```

## Configuration

Example US-listed live approval universe excerpt:

```yaml
mode: live_approval

portfolio:
  base_currency: USD
  initial_cash: 10000
  allowed_symbols:
    - CASH_USD
    - AAPL
    - MSFT
    - VOO
    - QQQ

universe:
  instruments:
    - symbol: AAPL
      asset_type: stock
      region: US
      currency: USD
      broker: kis
      broker_product: kis_overseas_stock
      broker_symbol: AAPL
      exchange_code: NASD
      quantity_step: 1
      price_tick: 0.01

strategies: []

datahub:
  provider: mock

execution:
  engine: paper

risk:
  max_single_asset_weight: 0.4
  min_cash_weight: 0.05

state:
  sqlite_path: var/maestro_state.db

audit:
  jsonl_path: var/audit.jsonl
```

This static form remains valid for examples, tests, tutorials, and conservative
paper configs. It should not be read as the final universe model. The future
production path is dynamic and policy-gated: strategy apps propose candidates,
Maestro resolves and validates them, and only approved tradable instruments may
appear in target allocations.

## Data Storage

Maestro uses two complementary storage mechanisms:

```text
SQLite = queryable state for dashboard/status
JSONL = append-only audit log
```

Default config paths use `var/` for local runtime artifacts:

```text
var/maestro_state.db
var/audit.jsonl
```

Other configs use the same convention, for example `var/approval_state.db` and
`var/live_readonly_state.db`. The `var/` directory is intentionally gitignored.

## Safety Principles

Maestro must be safe by default.

- v0.1 supports paper trading only.
- Strategy apps cannot execute orders.
- Strategy apps cannot access capital.
- Live trading must require explicit future mode changes.
- New strategies should default to research or paper mode.
- RiskManager is stronger than PortfolioManager.
- Unknown symbols should be rejected.
- Secrets must never be logged.

## Future Roadmap

For version-level planning, see [docs/ROADMAP.md](docs/ROADMAP.md).

For the current execution checklist, see [docs/TASKS.md](docs/TASKS.md).

For DataHub payload details, see [docs/datahub.md](docs/datahub.md).

Short direction:

- v0.1.x: bootable skeleton and stabilization
- v0.2: DataHub and read-only dashboard foundation
- v0.3: external research data providers
- v0.4: Telegram approval in paper mode
- v0.5: KIS read-only broker foundation
- v0.6: KIS live approval trading
- v0.7: production hardening
- v0.7.1: real-data US stock/ETF paper mode
- v0.7.2: KIS overseas read-only adapter
- v0.7.3: operational closeout
- v0.8: KIS overseas live approval beta
- v0.8.x: real-account promotion, DataHub, risk, recovery, and ops hardening
- v0.9: Virtuoso SDK/app integration after live-operations hardening
- v1.0: private approval-gated production beta
- post-v1.1: KIS-backed performance tracking for account, strategy, currency
  sleeve, and portfolio returns with read-only dashboard graphs

## Dashboard Philosophy

The dashboard should be read-only by default.

```text
Dashboard = observe
Telegram = approve
CLI/config = administer
Maestro = decide and execute
Virtuoso = propose
```

Dashboard should show:

- Total equity
- Cash
- Current positions
- Account-level daily/cumulative PnL and return
- Strategy-level daily/cumulative PnL and return
- Currency-sleeve KRW/USD PnL and return
- Total portfolio return and drawdown
- Recent strategy results
- Recent orders/proposals
- Risk status
- KIS status
- Telegram status
- System health

Planned KIS-backed performance tracking should use persisted broker snapshots,
broker reconciliation, order/fill events, and Maestro strategy lineage. The
dashboard should render graphs from persisted read models only; it should not
call KIS live endpoints during page rendering and should not expose trading or
admin write controls.

Dashboard should not initially allow:

- Order execution
- Risk limit changes
- Strategy enable/disable
- Live mode activation
- API key changes

## Telegram Philosophy

Telegram is the approval, urgent notification, and limited operator UI channel.

Initial allowed actions:

- Receive order proposal
- Approve proposal
- Reject proposal
- View details
- Receive fill notification
- Receive error/kill switch notification

Implemented operator commands:

- `/help`
- `/status`
- `/health`
- `/account`
- `/portfolio`
- `/apps`
- `/orders`
- `/approvals`
- `/pause`
- `/kill_switch`

Telegram operator commands are intentionally constrained. Read commands must use
stored SQLite state or the latest broker snapshot only; they must not call KIS
or other broker network endpoints directly. `/pause` and `/kill_switch` require
a whitelisted user, confirmation button, and persisted audit/system event.
Run the polling operator UI with:

```bash
maestro telegram-set-commands --config <operator-live-approval-config>
maestro telegram-operator --config <operator-live-approval-config>
```

Excluded Telegram commands include `/resume`, `/clear-halt`, `/live-on`,
`/dry-run-off`, `/buy`, `/sell`, `/cancel`, reconciliation triggers, direct
broker sync, and risk limit changes. High-risk actions such as enabling live
auto mode or changing risk limits should not be available through Telegram.

## KIS Integration Philosophy

Korea Investment Securities integration should live behind a broker adapter.

```text
execution/brokers/kis/
```

Start with:

```text
live_readonly
```

Then move to:

```text
live_approval
```

Do not start with:

```text
live_auto
```

Initial live trading rules:

- Telegram approval required
- Limit orders only
- Small order size
- Daily order notional cap
- Reconciliation required
- Unknown order status halts new orders

KIS responsibilities should stay broker-focused: authentication, balances,
positions, buying power, fills, broker state, order submission behind the safety
contract, and reconciliation. KIS current price lookup can support broker-side
quote/reference checks, but strategy research data should come through DataHub
providers rather than KIS.

## Development Rule

Do not add strategy-specific logic to Maestro.

Bad:

```python
if strategy_id == "techagora":
    apply_special_logic()
```

Good:

```python
if result.result_type == "target_allocation":
    process_target_allocation(result)
elif result.result_type == "strategy_signal":
    process_strategy_signal(result)
```

Maestro should understand contracts and capabilities, not individual strategy internals.
