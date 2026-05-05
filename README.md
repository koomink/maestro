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

## Project Status

Maestro v0.1 is intended to be a bootable skeleton, not a production trading system.

Current implementation includes the v0.1 skeleton plus Phase 1, Phase 2, and Phase 3 foundations:

- Paper mode only
- MockDataHub only
- CSVDataProvider for simple historical data loading
- TargetAllocationResult only
- External plugin loading
- Simple portfolio construction
- Simple risk management
- Paper execution
- SQLite state store
- JSONL audit logs
- CLI `run-once`
- CLI `status`
- Optional Streamlit read-only dashboard
- Approval request/decision gate before paper fills
- Telegram approval message formatter and notifier stub
- CLI `approvals`
- `live_readonly` mode config
- KIS read-only adapter interface and deterministic mock client
- CLI `kis-sync` and `kis-account`

No live trading is included in v0.1.

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
│       ├── api/
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

To run the paper pipeline with the Phase 2 approval gate enabled:

```bash
maestro run-once --config configs/approval_paper.yaml
maestro approvals --config configs/approval_paper.yaml
```

The Phase 2 Telegram integration is intentionally a no-network notifier stub. It formats the approval payload and records the configured decision so the orchestration, state, and audit contracts exist before a real Bot API client is added.

To run the Phase 3 KIS read-only mock adapter:

```bash
maestro kis-sync --config configs/live_readonly.yaml
maestro kis-account --config configs/live_readonly.yaml
```

The current KIS adapter is read-only and no-network. It stores deterministic mock broker account snapshots so the state, audit, dashboard, and CLI contracts are ready before a real KIS REST client is connected.

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
  min_cash_weight: 0.05
  max_single_asset_weight: 0.3
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
  allow_market_orders: false

risk:
  max_single_asset_weight: 0.3
  min_cash_weight: 0.05
  allow_short: false
  allow_leverage: false

state:
  sqlite_path: "data/maestro_state.db"

audit:
  jsonl_path: "logs/audit.jsonl"
```

## Data Storage

Maestro uses two complementary storage mechanisms:

```text
SQLite = queryable state for dashboard/status
JSONL = append-only audit log
```

Suggested paths:

```text
data/maestro_state.db
logs/audit.jsonl
```

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

### v0.1: Core Skeleton

- Paper mode
- MockDataHub
- Sample external plugin
- Simple PortfolioManager
- Simple RiskManager
- PaperExecutionEngine
- SQLite state
- JSONL audit logs

### v0.2: Data and Dashboard

- CSVDataProvider
- Minimal Streamlit read-only dashboard
- Portfolio and system status pages

### v0.3: Telegram Approval in Paper Mode

- Telegram notifications
- Order proposals
- Approve/reject buttons
- Whitelisted user IDs
- Approval timeout
- Paper execution after approval

### v0.4: Korea Investment Securities Read-only

- KIS OAuth token management
- Current price lookup
- Balance inquiry
- Buying power inquiry
- Fill/order inquiry
- Reconciliation
- `live_readonly` mode

### v0.5: KIS Live Approval Trading

- Telegram-approved live orders
- Limit orders only
- Small notional limits
- Fill polling
- Partial fill handling
- Broker/internal state reconciliation

### v0.6: Hardening

- KIS WebSocket
- Kill switch
- Advanced risk rules
- Performance attribution
- Enhanced dashboard
- Deployment guide

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
