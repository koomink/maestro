# Implementation Plan: Maestro v0.1 and Beyond

## 1. Implementation Strategy

Build Maestro incrementally as a standalone portfolio operating system. Do not start with live trading, Korea Investment Securities integration, Telegram approval, or a complex dashboard. Start with a bootable skeleton that proves the end-to-end operating pipeline.

The first milestone is not profitability. The first milestone is a reliable OS spine:

```text
Config
→ Plugin loading
→ Mock data
→ Strategy execution
→ Target allocation result
→ Validation
→ Portfolio construction
→ Risk check
→ Paper execution
→ State update
→ Audit log
```

## 2. Fixed v0.1 Decisions

The following decisions are fixed for Maestro v0.1:

1. **Mode**: paper mode only.
2. **Strategy result**: `TargetAllocationResult` only.
3. **DataHub**: `MockDataHub` only.
4. **Storage**: SQLite state store + JSONL audit log.
5. **Sample strategy**: lives under `examples/sample_static_allocation`, but is structured as an independent installable plugin package.
6. **Dashboard**: not implemented in v0.1, but state/log design must be dashboard-ready.
7. **Telegram**: not implemented in v0.1.
8. **KIS API**: not implemented in v0.1.

## 3. Milestone Overview

```text
M0: Project scaffold
M1: Core schemas and SDK
M2: Config loader
M3: Example strategy plugin
M4: Plugin loader and registry
M5: MockDataHub
M6: Orchestrator run_once
M7: PortfolioManager v0.1
M8: RiskManager v0.1
M9: PaperExecutionEngine and StateStore
M10: Audit logging
M11: CLI
M12: Tests and documentation
```

## Phase 1: Data and Dashboard Foundation

Implemented scope:

- `CSVDataProvider` for simple OHLCV CSV files.
- `configs/paper.yaml`, fixture configs, and `data/sample_prices.csv`.
- Provider selection through `datahub.provider`.
- SQLite read APIs for portfolio snapshots, strategy runs, orders, and system events.
- CLI `maestro status --config ...`.
- Optional FastAPI/React read-only dashboard entrypoint through `maestro dashboard --config ...`.

Out of scope remains unchanged:

- No live trading.
- No Telegram approval.
- No Korea Investment Securities integration.
- No dashboard write controls.

## Phase 2: Telegram Notifications and Approval Flow

Implemented scope:

- Approval request and decision schemas.
- ApprovalManager gate before paper fills.
- Paper approval fixture config.
- SQLite `approvals` table and read APIs.
- JSONL audit event for approval decisions.
- Telegram approval formatter and no-network notifier stub.
- CLI `maestro approvals --config ...`.
- Dashboard section for recent approvals.

The current notifier does not call Telegram Bot API. It deliberately records a configured decision (`approved`, `rejected`, or `expired`) so the Maestro approval contract can be tested without live credentials or network side effects.

Out of scope remains unchanged:

- No live trading.
- No real Telegram Bot API polling/webhook.
- No Korea Investment Securities integration.
- No dashboard write controls.

## Phase 3: Korea Investment Securities Read-only Adapter

Implemented scope:

- `live_readonly` run mode.
- `configs/live_readonly.yaml`.
- KIS read-only client interface.
- Deterministic no-network mock KIS client.
- KIS auth manager placeholder for environment-based credentials.
- Read-only service for account snapshot, current prices, daily orders, and unfilled orders.
- SQLite `broker_account_snapshots` table and read APIs.
- JSONL audit event for KIS read-only snapshots.
- CLI `maestro kis-sync --config ...`.
- CLI `maestro kis-account --config ...`.
- Dashboard section for recent broker account snapshots.

The real KIS REST client is intentionally deferred. This phase creates the adapter boundary and persisted read model without broker side effects.

Out of scope remains unchanged:

- No KIS order submission.
- No KIS WebSocket.
- No real OAuth token refresh.
- No live trading.

## 4. Milestone M0: Project Scaffold

### Goal

Create the repository structure and development setup.

### Deliverables

- `pyproject.toml`
- `README.md`
- `.env.example`
- `configs/paper.yaml`
- `src/maestro/` package
- `examples/sample_static_allocation/` package
- `tests/` folder

### Recommended dependencies

- pydantic
- pyyaml
- typer or argparse
- pytest
- rich optional for console output

### Acceptance Criteria

- `uv sync` or equivalent environment setup works.
- `python -m maestro` or `maestro --help` can be wired later.
- Package imports do not fail.

## 5. Milestone M1: Core Schemas and SDK

### Goal

Define the stable contract between Maestro and Virtuoso apps.

### Files

```text
src/maestro/core/enums.py
src/maestro/core/schemas.py
src/maestro/core/exceptions.py
src/maestro/core/ids.py
src/maestro/core/clock.py
src/maestro/sdk/__init__.py
src/maestro/sdk/strategy.py
src/maestro/sdk/schemas.py
```

### Implement

- `RunMode`
- `StrategyMode`
- `AssetType`
- `OrderSide`
- `OrderType`
- `OrderStatus`
- `StrategyManifest`
- `StrategyContext`
- `DataRequest`
- `DataBundle`
- `TargetAllocationResult`
- `PortfolioTarget`
- `RiskDecision`
- `OrderIntent`
- `ExecutionResult`
- `Position`
- `PortfolioState`
- `BaseStrategyPlugin` protocol or abstract base class

### Acceptance Criteria

- External plugin can import everything it needs from `maestro.sdk`.
- Schemas validate key constraints such as confidence between 0 and 1.
- Pydantic models can be serialized to JSON.

## 6. Milestone M2: Config Loader

### Goal

Load and validate YAML configuration.

### Files

```text
src/maestro/config/models.py
src/maestro/config/loader.py
configs/paper.yaml
```

### Implement

- `MaestroConfig`
- `PortfolioConfig`
- `StrategyPluginConfig`
- `DataHubConfig`
- `ExecutionConfig`
- `RiskConfig`
- `StateConfig`
- `AuditConfig`
- `load_config(path)`

### Example config requirements

- mode: paper
- initial cash
- allowed symbols
- strategy entrypoints
- strategy weights
- risk limits
- SQLite path
- audit log path

### Acceptance Criteria

- Config loads from `configs/paper.yaml`.
- Invalid config fails fast with a useful message.
- Environment variable expansion can be stubbed or included.

## 7. Milestone M3: Example Strategy Plugin

### Goal

Create a reference Virtuoso app inside `examples/`, but package it as if it were external.

### Files

```text
examples/sample_static_allocation/pyproject.toml
examples/sample_static_allocation/README.md
examples/sample_static_allocation/src/sample_static_allocation/__init__.py
examples/sample_static_allocation/src/sample_static_allocation/strategy.py
```

### Implement

`SampleStaticAllocationStrategy`:

- Imports only from `maestro.sdk`.
- Implements `manifest()`.
- Implements `build_data_requests()`.
- Implements `run()`.
- Returns `TargetAllocationResult`.

### Example behavior

Return a static allocation from plugin config or default:

```text
CASH: 50%
MOCK_ETF_A: 30%
MOCK_ETF_B: 20%
```

### Acceptance Criteria

- The sample plugin can be installed separately with editable install.
- The plugin does not import Maestro internals except `maestro.sdk`.
- The plugin can be loaded by entrypoint string.

## 8. Milestone M4: Plugin Loader and Registry

### Goal

Load external strategy plugins dynamically from config.

### Files

```text
src/maestro/plugins/base.py
src/maestro/plugins/loader.py
src/maestro/plugins/registry.py
src/maestro/plugins/permissions.py
```

### Implement

- Entrypoint parser: `module.path:ClassName`
- Dynamic import via `importlib`
- Instantiation of plugin class
- Manifest validation
- Registry of enabled plugins
- Basic error handling for import/manifest issues

### Acceptance Criteria

- `sample_static_allocation.strategy:SampleStaticAllocationStrategy` loads.
- Disabled strategies are skipped.
- Manifest and config ID mismatch is detected.
- Loader logs failures clearly.

## 9. Milestone M5: MockDataHub

### Goal

Provide deterministic mock data for strategy execution and paper pricing.

### Files

```text
src/maestro/datahub/base.py
src/maestro/datahub/mock_provider.py
src/maestro/datahub/validators.py
src/maestro/datahub/normalizer.py
```

### Implement

- `BaseDataProvider`
- `MockDataHub.get_data(requests)`
- Mock OHLCV or price data for `MOCK_ETF_A`, `MOCK_ETF_B`, and `CASH`
- Basic validation that requests include known symbols

### Acceptance Criteria

- DataHub returns a valid `DataBundle`.
- Unknown symbols are reported clearly.
- Mock data is deterministic for tests.

## 10. Milestone M6: Orchestrator run_once

### Goal

Implement the main operating cycle.

### Files

```text
src/maestro/orchestration/orchestrator.py
src/maestro/orchestration/cycle.py
```

### Implement

`MaestroOrchestrator.run_once()`:

1. Create cycle ID.
2. Load current state.
3. For each plugin:
   - Build context.
   - Build data requests.
   - Fetch data.
   - Run plugin.
   - Validate result.
4. Send valid results to PortfolioManager.
5. Send target to RiskManager.
6. Send approved target to PaperExecutionEngine.
7. Update state.
8. Log all events.

### Acceptance Criteria

- `run_once()` completes with the sample plugin.
- Invalid strategy results are rejected and logged.
- If all strategies fail, no execution occurs.

## 11. Milestone M7: PortfolioManager v0.1

### Goal

Convert strategy target allocation results into one portfolio target.

### Files

```text
src/maestro/portfolio/manager.py
src/maestro/portfolio/rebalancer.py
```

### Implement

- Fixed strategy-weighted aggregation
- Residual allocation to `CASH`
- Normalize overweight aggregate allocation
- Preserve source result metadata

### Acceptance Criteria

- Multiple target allocations combine correctly.
- Allocation sum is 1.0 after processing.
- Missing residual goes to `CASH`.

## 12. Milestone M8: RiskManager v0.1

### Goal

Enforce basic portfolio safety constraints.

### Files

```text
src/maestro/risk/manager.py
src/maestro/risk/limits.py
```

### Implement

- Allowed universe check
- No negative weights
- Max single asset weight
- Min cash weight
- Gross exposure <= 1.0
- No short
- No leverage

### Acceptance Criteria

- Unknown symbols reject the target.
- Gross exposure above 1.0 rejects the target.

## 13. Milestone M9: PaperExecutionEngine and StateStore

### Goal

Generate and execute paper orders, then persist state.

### Files

```text
src/maestro/execution/base.py
src/maestro/execution/order_builder.py
src/maestro/execution/paper.py
src/maestro/state/store.py
src/maestro/state/sqlite_store.py
src/maestro/state/portfolio_state.py
```

### Implement

- Current state initialization from config initial cash
- Current allocation calculation
- Target notional calculation
- Paper order intent generation
- Immediate paper fill
- Cash and position update
- SQLite snapshot persistence

### Acceptance Criteria

- Initial portfolio state is created if none exists.
- Paper orders update cash and positions correctly.
- SQLite database file is created.
- Portfolio snapshot is stored.

## 14. Milestone M10: Audit Logging

### Goal

Write append-only JSONL audit events for every cycle.

### Files

```text
src/maestro/monitoring/audit_logger.py
src/maestro/monitoring/events.py
src/maestro/monitoring/health.py
```

### Implement

- Structured audit event model
- JSONL writer
- Log levels: INFO, WARNING, ERROR, CRITICAL
- Events for:
  - cycle started
  - plugin loaded
  - strategy run completed/failed
  - portfolio target built
  - risk decision made
  - orders created
  - execution completed
  - state updated
  - cycle completed

### Acceptance Criteria

- Every run writes JSONL events.
- Events include cycle ID and timestamp.
- Log directory is created automatically.

## 15. Milestone M11: CLI

### Goal

Provide a simple user-facing command.

### Files

```text
src/maestro/cli.py
```

### Implement

```bash
maestro run-once --config configs/paper.yaml
maestro validate-config --config configs/paper.yaml
```

Optional:

```bash
maestro status
```

### Acceptance Criteria

- `maestro run-once` runs end-to-end.
- Command exits non-zero on fatal errors.
- Console output summarizes the cycle.

## 16. Milestone M12: Tests and Documentation

### Goal

Make v0.1 reliable and understandable.

### Tests

- Config loading test
- Plugin loader test
- Sample plugin contract test
- MockDataHub test
- PortfolioManager test
- RiskManager test
- PaperExecutionEngine test
- SQLite state store test
- Audit logger test
- End-to-end `run_once()` integration test

### Docs

- Update README with setup and run instructions.
- Explain Symphony/Maestro/Virtuoso model.
- Explain sample plugin structure.
- Explain future roadmap.

## 17. Future Implementation Phases

### v0.2: CSV Data and Minimal Dashboard

- Add CSVDataProvider.
- Add FastAPI/React read-only dashboard.
- Display portfolio snapshots, recent strategy runs, recent orders, and system events.

### v0.3: Telegram Notification and Approval in Paper Mode

- Add Telegram bot.
- Send cycle summaries.
- Create `OrderProposal` lifecycle.
- Send approval request with approve/reject buttons.
- Execute paper orders only after Telegram approval.

### v0.4: Korea Investment Securities Read-only Adapter

- Add KIS auth manager.
- Add current price, balance, buying power, order/fill inquiry.
- Add `live_readonly` mode.
- Add account reconciliation.

### v0.5: KIS Live Approval Trading

- Add live order submission.
- Require Telegram approval.
- Limit orders only.
- Enforce small notional limits.
- Handle order status polling.
- Handle partial fills.
- Reconcile broker state.

### v0.6: Hardening and Monitoring

- Add KIS WebSocket.
- Add kill switch.
- Add more risk rules.
- Improve dashboard.
- Add performance attribution.
- Add strategy lifecycle management.

## 18. Implementation Guardrails for Codex

When implementing:

1. Do not implement live trading in v0.1.
2. Do not import strategy code inside Maestro core manually.
3. Use entrypoint-based loading.
4. Keep sample plugin outside `src/maestro`.
5. Keep `maestro.sdk` as the public plugin API.
6. Do not add strategy-specific if/else logic.
7. Write tests for each module.
8. Keep all models serializable.
9. Do not log secrets.
10. Prefer clear, simple implementations over over-engineering.
