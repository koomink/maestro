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
    market and research data: price, ohlcv, macro, news, sentiment, fundamental
    future providers: Yahoo Finance/yfinance-style OHLCV, FRED, CSV/local,
    RSS/GDELT/News API, sentiment/community data, crypto exchange market data

execution/brokers/
    broker account and execution data: auth, balances, positions, buying power,
    orders, fills, reconciliation, broker_quote reference data
```

Strategy plugins must request data through Maestro DataHub and must not call external research data APIs or broker APIs directly. KIS current price lookup may be used as broker-side `broker_quote` reference data for execution validation or reconciliation, but KIS is not the primary strategy or research data source.

Maestro owns the lifecycle of a portfolio management cycle:

```text
Config
→ Plugin loading
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

`datahub/` owns research and market data normalization, validation, routing, freshness, and provider selection. Its planned data types include `price`, `ohlcv`, `macro`, `news`, `sentiment`, and `fundamental`.

`execution/brokers/` owns broker/account/execution integrations. Broker adapters, including KIS, should expose account state, buying power, positions, order submission/status, fills, reconciliation, and optional `broker_quote` data used only for execution validation or reconciliation.

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

### 3.2 Future Integrations

- Streamlit for early read-only dashboard
- FastAPI for future read-only API
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
- Telegram integration currently formats approval messages without calling Bot API.

### 3.5 Current KIS Read-only Foundation

- `RunMode.LIVE_READONLY` is supported for read-only broker sync commands.
- KIS integration is isolated under `execution/brokers/kis`.
- `KISReadOnlyClient` defines account snapshot, positions, buying power, broker-side quote/reference, order/fill, and unfilled order reads.
- `MockKISReadOnlyClient` provides deterministic no-network responses.
- `KISRestReadOnlyClient` adapts the reference repo's KIS OAuth token issuance, request headers, TR_IDs, and inquiry payloads for read-only REST calls.
- App key and secret are read from configured environment variable names only.
- Access tokens can come from an environment variable or an owner-only cache file after `/oauth2/tokenP` issuance.
- Broker account snapshots are persisted in SQLite and audit JSONL.
- v0.5 exposes no callable KIS order submission, cancel, amend, buy, or sell path.

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
    LIVE_AUTO = "live_auto"

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
    strategy_id: str
    name: str
    version: str
    description: str | None = None
    author: str | None = None
    supported_modes: list[StrategyMode]
    supported_asset_types: list[AssetType]
    result_type: Literal["target_allocation"]
    requires_data: list[str] = []
    default_time_horizon: str | None = None
    can_run_live: bool = False
    can_use_leverage: bool = False
    can_short: bool = False
```

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
    data_type: str  # e.g., "price", "ohlcv", "macro", "news", "sentiment", "fundamental"
    timeframe: str | None = None
    lookback: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    fields: list[str] = []
```

`broker_quote` is reserved for broker-side reference prices used by execution validation or reconciliation. It should not replace DataHub research feeds for strategy decisions.

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
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = []
    metadata: dict[str, Any] = {}
```

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
    modifications: list[str] = []
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

    def run(self, data: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        ...
```

Future optional methods:

```python
def health_check(self) -> HealthStatus: ...
def on_start(self, context: StrategyContext) -> None: ...
def on_stop(self, context: StrategyContext) -> None: ...
def on_error(self, error: Exception, context: StrategyContext) -> None: ...
```

## 7. Plugin Loading

Config example:

```yaml
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
```

Loader behavior:

1. Parse entrypoint `module:class`.
2. Import module via `importlib`.
3. Instantiate class.
4. Verify instance implements required methods.
5. Read manifest.
6. Validate manifest strategy ID against config ID.
7. Register plugin in registry.

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

Maestro v0.1.1 uses strict Pydantic config validation. Unknown YAML fields fail
loudly instead of being ignored.

## 9. Orchestrator Run Cycle

`MaestroOrchestrator.run_once()` should:

1. Create `cycle_id`.
2. Load current portfolio state.
3. Load enabled strategies.
4. For each strategy:
   - Build `StrategyContext`.
   - Request data requests.
   - Fetch data from DataHub.
   - Run plugin.
   - Validate result.
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

- Valid `TargetAllocationResult` objects
- Strategy weights from config

Algorithm:

1. Ignore disabled strategies.
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
- Single symbol weight <= configured cap.
- Cash weight >= minimum.
- No short exposure.
- No leverage.

Risk behavior:

- If an unknown symbol is found, reject.
- If overweight, cap the symbol and move excess to CASH.
- If cash below minimum, reduce largest non-cash allocation to satisfy cash minimum.
- Log all modifications.

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
- modifications_json
- decision_json

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

## 15. Telegram Integration Future Design

Telegram is the action and notification channel.

Initial future components:

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
- Send order proposal messages.
- Support approve/reject/detail inline buttons.
- Expire proposals after timeout.
- Log all decisions.
- Allow future `/status`, `/portfolio`, `/pause`, `/kill-switch` commands.
- Do not allow Telegram to disable risk limits or enable live auto mode.

## 16. KIS Adapter Future Design

KIS adapter should be isolated under:

```text
execution/brokers/kis/
```

Components:

- `auth.py`: OAuth token and approval key management
- `client.py`: read-only client protocol
- `rest_client.py`: read-only REST client wrapper
- future `orders.py`: order submission, cancel, amend after v0.5
- `rest_client.py`: balance, buying power, positions, order/fill inquiry, unfilled orders, and broker-side quote/reference lookup for execution validation or reconciliation
- `websocket.py`: real-time price/fill notification later
- `errors.py`: error code handling

The KIS adapter is a broker adapter, not a research data provider. It should mainly handle authentication, balances, positions, buying power, fills, and reconciliation. KIS current price lookup can produce `broker_quote` data for broker-side checks, but strategy plugins should receive research and market data through DataHub providers.

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

Initial Streamlit pages:

1. Overview
2. Portfolio
3. Performance
4. Strategies
5. Orders and Proposals
6. System Health

Security:

- Prefer localhost/Tailscale access.
- Do not expose public internet without auth/reverse proxy.
- No secrets displayed.
- No write endpoints in early versions.

## 18. Testing Requirements

v0.1 tests:

- Config loads and validates.
- Plugin loader imports sample plugin.
- Sample plugin returns valid manifest and result.
- MockDataHub returns DataBundle.
- Strategy result validation catches invalid allocations.
- PortfolioManager combines allocations correctly.
- RiskManager caps overweight positions and rejects unknown symbols.
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
- KIS secrets via `.env` or secret manager
