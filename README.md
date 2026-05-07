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
│   ├── Plugin Manager
│   ├── Strategy Orchestrator
│   ├── Signal Validator
│   ├── Portfolio Manager
│   ├── Risk Manager
│   ├── Approval Manager
│   ├── Execution Engine
│   ├── State Manager
│   ├── Audit Logger
│   ├── Dashboard / API
│   └── Integrations
│       ├── Telegram
│       └── Korea Investment Securities Open API
│
└── Virtuoso Apps
    ├── Ataraxia
    ├── TechAgora
    ├── Macro
    ├── Fundamental
    └── TradingAgents
```

## Data and Execution Boundaries

Maestro separates research/market data from broker account and execution data.

```text
Research / strategy data
Yahoo Finance, FRED, CSV/local, RSS feeds,
rule-based sentiment, future GDELT/News API and community APIs
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
Execution engine
        │
        ▼
Broker adapters, including KIS
auth, balances, positions, buying power, orders, fills, reconciliation,
and broker-side quote/reference data for execution checks
```

DataHub is the market and research data layer. Broker adapters are the account and execution layer. Strategy plugins must request data through Maestro DataHub and should not call external market, macro, news, sentiment, or broker APIs directly.

Korea Investment Securities current price lookup may be used as broker-side quote/reference data for execution validation or reconciliation, but KIS is not the primary strategy or research data source.

## Project Status

Maestro v0.1.1 is a stabilization patch on top of the v0.1 bootable skeleton. It is
not a production trading system.

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

- Optional Yahoo/yfinance-style `price` and `ohlcv` provider behind Maestro DataHub
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
- Telegram Bot API polling approval MVP for paper mode
- CLI `approvals`
- `live_readonly` mode config
- KIS read-only adapter interface and deterministic mock client
- KIS read-only REST client for auth, balance, positions, buying power, order/fill inquiry, unfilled order inquiry, and broker-side quote lookup
- CLI `kis-sync` and `kis-account`

Deferred real integrations:

- No live trading
- No Telegram webhook or inline callback buttons
- No KIS order submission
- No GDELT/News API or community sentiment APIs yet
- No crypto market data while the supported universe is stocks and ETFs only
- No web dashboard write controls

## Optional Yahoo/yfinance Provider

The Yahoo/yfinance provider is optional and is not required for core Maestro
usage:

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
polling for `approve <approval_id>` or `reject <approval_id>` replies. Inline
keyboards, callback queries, webhooks, dashboard write controls, KIS order
submission, and live trading remain deferred. Normal tests use fake Telegram
clients and do not call the Telegram network.

To run the KIS read-only adapter:

```bash
maestro kis-sync --config configs/live_readonly.yaml
maestro kis-account --config configs/live_readonly.yaml
```

`configs/live_readonly.yaml` uses the deterministic no-network mock provider.
For the real KIS read-only REST provider, start from
`configs/kis_live_readonly.example.yaml`, set `kis.provider: kis`,
`kis.account_id`, and these environment variable names:

- `KIS_APP_KEY`: KIS app key
- `KIS_APP_SECRET`: KIS app secret
- `KIS_ACCESS_TOKEN`: optional pre-issued access token

If `KIS_ACCESS_TOKEN` is unset, Maestro can issue `/oauth2/tokenP` and can
persist the access token when `kis.token_cache_path` is configured. The cache
file is written with owner-only permissions. Access tokens may be stored only in
`kis.token_cache_path`; they must never be written to state, audit logs,
dashboard rows, or test fixtures. App secrets follow the same no-persistence
rule.

The KIS client is read-only in v0.5. It adapts OAuth/header/TR_ID/payload logic
from `koomink/open-trading-api` for these inquiry APIs only:
`inquire-balance`, `inquire-psbl-order`, `inquire-daily-ccld`, and
`inquire-price`. This is domestic-stock read-only first. Overseas stock/ETF
endpoints, pagination/continuation handling, canonical symbol mapping, and
state-vs-broker reconciliation remain future work. Order submission samples from
the reference repo were not copied or exposed.

To install dashboard dependencies and open the read-only dashboard:

```bash
uv sync --extra dashboard
maestro dashboard --config configs/paper.yaml
```

If no CLI entrypoint exists yet during early development, use:

```bash
python -m maestro.cli run-once --config configs/paper.yaml
```

## Configuration

Example `configs/paper.yaml`:

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
    mode: paper
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
  engine: paper

risk:
  max_single_asset_weight: 0.4
  min_cash_weight: 0.05

state:
  sqlite_path: var/maestro_state.db

audit:
  jsonl_path: var/audit.jsonl
```

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
- v0.5: KIS read-only broker integration
- v0.6: KIS live approval trading
- v0.7: production hardening

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
- Daily/cumulative PnL
- Recent strategy results
- Recent orders/proposals
- Risk status
- KIS status
- Telegram status
- System health

Dashboard should not initially allow:

- Order execution
- Risk limit changes
- Strategy enable/disable
- Live mode activation
- API key changes

## Telegram Philosophy

Telegram is the approval and urgent notification channel.

Initial allowed actions:

- Receive order proposal
- Approve proposal
- Reject proposal
- View details
- Receive fill notification
- Receive error/kill switch notification

Future allowed actions may include:

- `/status`
- `/portfolio`
- `/pause`
- `/kill-switch`

High-risk actions such as enabling live auto mode or changing risk limits should not be available through Telegram.

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

KIS responsibilities should stay broker-focused: authentication, balances, positions, buying power, fills, broker state, and reconciliation. KIS current price lookup can support broker-side quote/reference checks, but strategy research data should come through DataHub providers rather than KIS. v0.5 deliberately has no callable KIS buy/sell/order submission path.

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
```

Maestro should understand contracts and capabilities, not individual strategy internals.
