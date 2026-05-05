# TASKS

## Current Milestone: v0.2 DataHub and Dashboard Foundation

### DataHub

- [x] Add v0.2 DataHub schema models:
  - `PricePoint`
  - `OHLCVBar`
  - `SymbolData`
- [x] Clarify latest price versus historical bars
- [x] Update MockDataHub to emit v0.2-compatible payloads
- [x] Update CSVDataProvider to emit v0.2-compatible payloads
- [x] Preserve backward compatibility where practical for current orchestrator price extraction
- [x] Parse CSV timestamps into datetime objects
- [x] Validate OHLCV rows:
  - open/high/low/close > 0
  - volume >= 0
  - high >= low
  - high >= open and high >= close
  - low <= open and low <= close
- [x] Define missing data behavior clearly
- [x] Add basic freshness or stale data fields to `SymbolData`
- [x] Add tests for CSV schema validation
- [x] Add tests for missing symbols and invalid OHLCV rows

### Symbol Metadata

- [x] Add a simple `SymbolMetadata` model
- [x] Include fields such as:
  - symbol
  - asset_type
  - currency
  - tradable
  - quantity_step
  - min_order_quantity
  - min_order_notional
- [x] Keep metadata lightweight and in-memory for v0.2
- [x] Do not build a full asset registry database yet

### State / Persistence

- [x] Add a `risk_decisions` SQLite table
- [x] Add `save_risk_decision` method
- [x] Add `list_risk_decisions` method
- [x] Persist RiskDecision after every risk check
- [x] Add SQLite connection timeout
- [x] Add PRAGMA `busy_timeout`
- [x] Add PRAGMA `journal_mode=WAL` where appropriate
- [x] Preserve existing tables and behavior
- [x] Add tests for risk decision persistence
- [x] Add tests confirming existing state methods still work

### Dashboard Read Models

- [x] Add dashboard read model helpers
- [x] Keep StateStore focused on persistence
- [x] Put display transformations in `dashboard/read_models.py`
- [x] Add overview read model
- [x] Add portfolio table read model
- [x] Add strategy runs table read model
- [x] Add orders table read model
- [x] Add approvals table read model
- [x] Add risk decisions table read model
- [x] Add broker snapshots table read model
- [x] Add system events table read model

### Dashboard UI

- [x] Improve the Streamlit dashboard while keeping it read-only
- [x] Add overview metrics:
  - cash
  - positions count
  - strategy runs count
  - orders count
  - approvals count
  - broker snapshots count
  - latest run time if available
- [x] Add portfolio section with readable table
- [x] Add strategy runs section
- [x] Add orders section
- [x] Add approvals section
- [x] Add risk decisions section
- [x] Add broker snapshots section
- [x] Add system events or recent errors section
- [x] Do not add buttons that execute orders
- [x] Do not add controls to change risk limits
- [x] Do not add live mode activation
- [x] Do not add strategy enable/disable controls

### Orchestration

- [x] Persist RiskDecision after risk check
- [x] Keep current run-once behavior intact
- [x] Optionally split run_once into small private methods only where it improves readability
- [x] Do not create a large workflow engine
- [x] Do not change strategy plugin contracts unless needed for DataHub compatibility

### Documentation

- [x] Update README if needed
- [x] Update ROADMAP.md only if v0.2 scope needs clarification
- [x] Document the v0.2 DataHub payload shape
- [x] Document that dashboard is read-only
- [x] Document that real integrations remain deferred

### Verification

- [x] `ruff check .`
- [x] `ruff format --check .`
- [x] `pytest -q`
- [x] `maestro run-once --config configs/paper.yaml`
- [x] `maestro run-once --config configs/csv_paper.yaml`
- [x] `maestro status --config configs/paper.yaml`

## Next Milestone: v0.3 Planning

- [ ] Plan real Telegram Bot API integration boundary
- [ ] Plan approval callback persistence and duplicate-decision prevention
- [ ] Decide polling versus webhook for the first Telegram implementation
- [ ] Define paper-only approval behavior before live trading work begins

## Completed / Historical Notes

- v0.1.0 delivered the bootable paper-mode skeleton described in [ROADMAP.md](ROADMAP.md).
- v0.1.1 stabilized IDs, config validation, missing price behavior, execution engine validation, failure audit metadata, and docs consistency.
- v0.2 delivered DataHub schema clarity, stronger CSV/mock providers, risk decision persistence, dashboard read models, and read-only dashboard improvements.

For version-level planning and future milestones, see [ROADMAP.md](ROADMAP.md).
