# TASKS: Maestro v0.1 Build Plan

## Legend

- `[ ]` Not started
- `[~]` In progress
- `[x]` Done
- Priority: P0 = required for v0.1, P1 = soon after, P2 = future

---

## Epic 0: Project Setup

### P0 Tasks

- [ ] Create Python project structure.
- [ ] Create `pyproject.toml`.
- [ ] Add dependencies: pydantic, pyyaml, typer or argparse, pytest.
- [ ] Create `src/maestro/` package.
- [ ] Create `examples/sample_static_allocation/` package.
- [ ] Create `configs/paper.yaml`.
- [ ] Create `tests/` directory.
- [ ] Add `.env.example`.
- [ ] Add `.gitignore` for `.env`, logs, data DB, caches.

### Acceptance Criteria

- [ ] Project imports successfully.
- [ ] Editable install works for Maestro.
- [ ] Editable install works for sample strategy plugin.

---

## Epic 1: Core Schemas and SDK

### P0 Tasks

- [ ] Implement `src/maestro/core/enums.py`.
- [ ] Implement `RunMode` enum.
- [ ] Implement `StrategyMode` enum.
- [ ] Implement `AssetType` enum.
- [ ] Implement `OrderSide` enum.
- [ ] Implement `OrderType` enum.
- [ ] Implement `OrderStatus` enum.
- [ ] Implement `src/maestro/core/schemas.py`.
- [ ] Implement `StrategyManifest`.
- [ ] Implement `StrategyContext`.
- [ ] Implement `DataRequest`.
- [ ] Implement `DataBundle`.
- [ ] Implement `TargetAllocationResult`.
- [ ] Implement `PortfolioTarget`.
- [ ] Implement `RiskDecision`.
- [ ] Implement `OrderIntent`.
- [ ] Implement `ExecutionResult`.
- [ ] Implement `Position`.
- [ ] Implement `PortfolioState`.
- [ ] Implement `src/maestro/plugins/base.py` or `src/maestro/sdk/strategy.py` with `BaseStrategyPlugin`.
- [ ] Implement `src/maestro/sdk/__init__.py` to export public SDK objects.

### Tests

- [ ] Test model serialization.
- [ ] Test `confidence` validation range.
- [ ] Test invalid allocations fail where appropriate.
- [ ] Test external plugin can import only from `maestro.sdk`.

---

## Epic 2: Config System

### P0 Tasks

- [ ] Implement `src/maestro/config/models.py`.
- [ ] Implement `MaestroConfig`.
- [ ] Implement `PortfolioConfig`.
- [ ] Implement `StrategyPluginConfig`.
- [ ] Implement `DataHubConfig`.
- [ ] Implement `ExecutionConfig`.
- [ ] Implement `RiskConfig`.
- [ ] Implement `StateConfig`.
- [ ] Implement `AuditConfig`.
- [ ] Implement `src/maestro/config/loader.py`.
- [ ] Implement `load_config(path)`.
- [ ] Write default `configs/paper.yaml`.

### Tests

- [ ] Test valid config loads.
- [ ] Test missing required fields fail.
- [ ] Test invalid mode fails.
- [ ] Test strategy entrypoint required when strategy enabled.

---

## Epic 3: Sample Strategy Plugin

### P0 Tasks

- [ ] Create `examples/sample_static_allocation/pyproject.toml`.
- [ ] Create `examples/sample_static_allocation/README.md`.
- [ ] Create `examples/sample_static_allocation/src/sample_static_allocation/__init__.py`.
- [ ] Create `examples/sample_static_allocation/src/sample_static_allocation/strategy.py`.
- [ ] Implement `SampleStaticAllocationStrategy`.
- [ ] Ensure plugin imports only from `maestro.sdk`.
- [ ] Implement `manifest()`.
- [ ] Implement `build_data_requests()`.
- [ ] Implement `run()`.
- [ ] Return `TargetAllocationResult`.

### Tests

- [ ] Test sample plugin manifest.
- [ ] Test sample plugin data requests.
- [ ] Test sample plugin result.
- [ ] Test sample plugin can be loaded by entrypoint string.

---

## Epic 4: Plugin Loader and Registry

### P0 Tasks

- [ ] Implement `src/maestro/plugins/loader.py`.
- [ ] Parse entrypoint format `module:ClassName`.
- [ ] Dynamically import module.
- [ ] Instantiate plugin.
- [ ] Validate required methods.
- [ ] Validate manifest.
- [ ] Check manifest strategy ID vs config strategy ID.
- [ ] Implement `src/maestro/plugins/registry.py`.
- [ ] Register enabled plugins.
- [ ] Skip disabled plugins.
- [ ] Add clear error messages for failed imports.

### Tests

- [ ] Test valid plugin loads.
- [ ] Test invalid entrypoint fails.
- [ ] Test disabled plugin skipped.
- [ ] Test manifest/config ID mismatch fails.

---

## Epic 5: MockDataHub

### P0 Tasks

- [ ] Implement `src/maestro/datahub/base.py`.
- [ ] Implement `BaseDataProvider`.
- [ ] Implement `src/maestro/datahub/mock_provider.py`.
- [ ] Return deterministic mock price/OHLCV data.
- [ ] Include mock symbols: `CASH`, `MOCK_ETF_A`, `MOCK_ETF_B`.
- [ ] Implement basic request validation.

### Tests

- [ ] Test known symbols return data.
- [ ] Test unknown symbols error clearly.
- [ ] Test DataBundle generated with timestamp and source.

---

## Epic 6: Strategy Result Validation

### P0 Tasks

- [ ] Implement `src/maestro/signals/validator.py`.
- [ ] Validate strategy ID is registered.
- [ ] Validate timestamp exists.
- [ ] Validate confidence range.
- [ ] Validate allocations are non-negative.
- [ ] Validate allocations are not empty.
- [ ] Validate only `TargetAllocationResult` is accepted in v0.1.

### Tests

- [ ] Test valid result accepted.
- [ ] Test negative allocation rejected.
- [ ] Test empty allocation rejected.
- [ ] Test unregistered strategy rejected.

---

## Epic 7: PortfolioManager v0.1

### P0 Tasks

- [ ] Implement `src/maestro/portfolio/manager.py`.
- [ ] Aggregate strategy allocations using fixed config weights.
- [ ] Normalize if total allocation > 1.0.
- [ ] Allocate residual to `CASH` if total allocation < 1.0.
- [ ] Preserve source strategy result references.
- [ ] Return `PortfolioTarget`.

### Tests

- [ ] Test single strategy allocation.
- [ ] Test multiple strategy weighted allocation.
- [ ] Test residual to cash.
- [ ] Test normalization.

---

## Epic 8: RiskManager v0.1

### P0 Tasks

- [ ] Implement `src/maestro/risk/limits.py`.
- [ ] Implement `src/maestro/risk/manager.py`.
- [ ] Enforce allowed universe.
- [ ] Enforce non-negative allocations.
- [ ] Enforce max single asset weight.
- [ ] Enforce min cash weight.
- [ ] Enforce gross exposure <= 1.0.
- [ ] Move capped excess to `CASH`.
- [ ] Return `RiskDecision` with modifications and violations.

### Tests

- [ ] Test unknown symbol rejection.
- [ ] Test max single asset cap.
- [ ] Test min cash enforcement.
- [ ] Test risk modifications recorded.
- [ ] Test valid target passes unchanged.

---

## Epic 9: State Store

### P0 Tasks

- [ ] Implement `src/maestro/state/store.py` interface.
- [ ] Implement `src/maestro/state/sqlite_store.py`.
- [ ] Create SQLite DB if missing.
- [ ] Create `portfolio_snapshots` table.
- [ ] Create `strategy_runs` table.
- [ ] Create `orders` table.
- [ ] Create `risk_decisions` table.
- [ ] Create `system_events` table.
- [ ] Implement load latest portfolio state.
- [ ] Implement save portfolio snapshot.
- [ ] Implement save strategy run.
- [ ] Implement save order.
- [ ] Implement save risk decision.
- [ ] Implement save system event.

### Tests

- [ ] Test DB initialization.
- [ ] Test saving and loading latest portfolio state.
- [ ] Test saving orders.
- [ ] Test saving system events.

---

## Epic 10: Paper Execution

### P0 Tasks

- [ ] Implement `src/maestro/execution/base.py`.
- [ ] Implement `src/maestro/execution/order_builder.py`.
- [ ] Implement `src/maestro/execution/paper.py`.
- [ ] Compute current portfolio weights.
- [ ] Compute target notional.
- [ ] Generate order intents.
- [ ] Fill paper orders immediately.
- [ ] Update cash and positions.
- [ ] Return execution results.

### Tests

- [ ] Test initial buy orders from cash.
- [ ] Test rebalance sell/buy orders.
- [ ] Test cash updates correctly.
- [ ] Test positions update correctly.
- [ ] Test execution result statuses are filled.

---

## Epic 11: Audit Logger

### P0 Tasks

- [ ] Implement `src/maestro/monitoring/events.py`.
- [ ] Implement `src/maestro/monitoring/audit_logger.py`.
- [ ] Create log directory if missing.
- [ ] Write JSONL events.
- [ ] Include timestamp, cycle ID, level, component, event type, message, details.
- [ ] Add helper methods for common event types.

### Tests

- [ ] Test JSONL file creation.
- [ ] Test events are valid JSON.
- [ ] Test event includes cycle ID.

---

## Epic 12: Orchestrator

---

## Phase 1: Data and Dashboard Foundation

### Completed

- [x] Add CSVDataProvider.
- [x] Add sample CSV historical data.
- [x] Add CSV-backed paper config.
- [x] Wire DataHub provider selection into orchestrator.
- [x] Add SQLite read APIs for dashboard/status views.
- [x] Add `maestro status`.
- [x] Add optional Streamlit read-only dashboard entrypoint.
- [x] Add tests for CSV provider and CSV run-once pipeline.

---

## Phase 2: Telegram Notifications and Approval Flow

### Completed

- [x] Add approval request and decision models.
- [x] Add approval config.
- [x] Add approval gate before paper execution fills.
- [x] Add SQLite approvals table.
- [x] Add approval audit events.
- [x] Add Telegram approval formatter.
- [x] Add no-network Telegram notifier stub.
- [x] Add `maestro approvals`.
- [x] Add dashboard recent approvals section.
- [x] Add approval flow tests for approved and rejected decisions.

### Deferred

- [ ] Real Telegram Bot API delivery.
- [ ] Telegram inline button callback handling.
- [ ] Whitelisted Telegram user callback enforcement against real updates.

---

## Phase 3: Korea Investment Securities Read-only Adapter

### Completed

- [x] Add `live_readonly` mode.
- [x] Add KIS config model.
- [x] Add live-readonly config file.
- [x] Add KIS read-only client interface.
- [x] Add mock KIS read-only client.
- [x] Add KIS auth manager placeholder.
- [x] Add read-only KIS service.
- [x] Store broker account snapshots in SQLite.
- [x] Add KIS read-only audit event.
- [x] Add `maestro kis-sync`.
- [x] Add `maestro kis-account`.
- [x] Add dashboard broker snapshots section.
- [x] Add tests for KIS read-only service and CLI.

### Deferred

- [ ] Real KIS REST client.
- [ ] OAuth token refresh and persistence.
- [ ] Broker reconciliation against internal portfolio state.
- [ ] KIS order/fill history normalization beyond mock responses.

---

## v0.1 Release Hardening

### Completed

- [x] Add config validation failure tests.
- [x] Add plugin loader failure tests.
- [x] Add signal validator failure tests.
- [x] Add paper execution behavior tests.
- [x] Add audit JSONL content assertion for run-once.
- [x] Add release checklist.
- [x] Clarify README v0.1 scope vs later foundations.

### P0 Tasks

- [ ] Implement `src/maestro/orchestration/cycle.py`.
- [ ] Implement `src/maestro/orchestration/orchestrator.py`.
- [ ] Wire config, state store, plugin registry, DataHub, validator, portfolio, risk, execution, audit logger.
- [ ] Implement `run_once()`.
- [ ] Ensure every major step logs an audit event.
- [ ] Ensure fatal errors are logged and raised/returned clearly.

### Tests

- [ ] Test `run_once()` completes with sample plugin.
- [ ] Test `run_once()` writes audit log.
- [ ] Test `run_once()` writes SQLite portfolio snapshot.
- [ ] Test strategy failure does not crash whole system if other strategies exist.

---

## Epic 13: CLI

### P0 Tasks

- [ ] Implement `src/maestro/cli.py`.
- [ ] Add `run-once --config` command.
- [ ] Add `validate-config --config` command.
- [ ] Add console summary output.
- [ ] Wire CLI entrypoint in `pyproject.toml`.

### Tests

- [ ] Test CLI config validation.
- [ ] Test CLI run-once command through subprocess or runner.

---

## Epic 14: Documentation

### P0 Tasks

- [ ] Update `README.md`.
- [ ] Explain Symphony/Maestro/Virtuoso metaphor.
- [ ] Explain v0.1 scope.
- [ ] Explain setup.
- [ ] Explain sample plugin installation.
- [ ] Explain `maestro run-once`.
- [ ] Explain logs and SQLite state.
- [ ] Explain roadmap.

---

# Future Epics

## Epic 15: CSVDataProvider

Priority: P1

- [ ] Add CSV data loading.
- [ ] Add config for CSV data paths.
- [ ] Support simple historical price series.
- [ ] Add tests.

## Epic 16: Streamlit Read-only Dashboard

Priority: P1

- [ ] Add dashboard app.
- [ ] Overview page.
- [ ] Portfolio page.
- [ ] Strategy runs page.
- [ ] Orders/logs page.
- [ ] System health page.
- [ ] Read SQLite state only.
- [ ] No write controls.

## Epic 17: Telegram Notifications

Priority: P1

- [ ] Add Telegram config.
- [ ] Add bot token from environment.
- [ ] Add allowed user IDs.
- [ ] Send cycle summary.
- [ ] Send error alerts.

## Epic 18: Telegram Approval Flow

Priority: P1

- [ ] Add `OrderProposal` model.
- [ ] Add proposal lifecycle.
- [ ] Send proposal with inline buttons.
- [ ] Handle approve/reject.
- [ ] Enforce timeout.
- [ ] Execute approved proposals in paper mode.
- [ ] Log approval decisions.

## Epic 19: KIS Read-only Adapter

Priority: P2

- [ ] Add KIS auth manager.
- [ ] Add token refresh.
- [ ] Add current price lookup.
- [ ] Add balance inquiry.
- [ ] Add buying power inquiry.
- [ ] Add order/fill inquiry.
- [ ] Add unfilled order inquiry.
- [ ] Add live_readonly mode.
- [ ] Add reconciliation.

## Epic 20: KIS Live Approval Trading

Priority: P2

- [ ] Add KIS live order submission.
- [ ] Limit orders only.
- [ ] Telegram approval required.
- [ ] Add notional limits.
- [ ] Add order status polling.
- [ ] Add partial fill handling.
- [ ] Add broker order ID mapping.
- [ ] Add duplicate-order prevention.
- [ ] Add live order audit logs.

## Epic 21: Production Hardening

Priority: P2

- [ ] Add kill switch.
- [ ] Add reconciliation halt.
- [ ] Add unknown order status halt.
- [ ] Add daily notional limit.
- [ ] Add daily loss limit.
- [ ] Add performance attribution.
- [ ] Add KIS WebSocket.
- [ ] Add systemd deployment guide.
- [ ] Add Tailscale dashboard guide.
