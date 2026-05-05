# TASKS

## Current Milestone: v0.2 DataHub and Dashboard Foundation

### DataHub

- [ ] Add v0.2 DataHub schema models:
  - `PricePoint`
  - `OHLCVBar`
  - `SymbolData`
- [ ] Clarify latest price versus historical bars
- [ ] Update MockDataHub to emit v0.2-compatible payloads
- [ ] Update CSVDataProvider to emit v0.2-compatible payloads
- [ ] Preserve backward compatibility where practical for current orchestrator price extraction
- [ ] Parse CSV timestamps into datetime objects
- [ ] Validate OHLCV rows:
  - open/high/low/close > 0
  - volume >= 0
  - high >= low
  - high >= open and high >= close
  - low <= open and low <= close
- [ ] Define missing data behavior clearly
- [ ] Add basic freshness or stale data fields to `SymbolData`
- [ ] Add tests for CSV schema validation
- [ ] Add tests for missing symbols and invalid OHLCV rows

### Symbol Metadata

- [ ] Add a simple `SymbolMetadata` model
- [ ] Include fields such as:
  - symbol
  - asset_type
  - currency
  - tradable
  - quantity_step
  - min_order_quantity
  - min_order_notional
- [ ] Keep metadata lightweight and in-memory for v0.2
- [ ] Do not build a full asset registry database yet

### State / Persistence

- [ ] Add a `risk_decisions` SQLite table
- [ ] Add `save_risk_decision` method
- [ ] Add `list_risk_decisions` method
- [ ] Persist RiskDecision after every risk check
- [ ] Add SQLite connection timeout
- [ ] Add PRAGMA `busy_timeout`
- [ ] Add PRAGMA `journal_mode=WAL` where appropriate
- [ ] Preserve existing tables and behavior
- [ ] Add tests for risk decision persistence
- [ ] Add tests confirming existing state methods still work

### Dashboard Read Models

- [ ] Add dashboard read model helpers
- [ ] Keep StateStore focused on persistence
- [ ] Put display transformations in `dashboard/read_models.py` or similar
- [ ] Add overview read model
- [ ] Add portfolio table read model
- [ ] Add strategy runs table read model
- [ ] Add orders table read model
- [ ] Add approvals table read model
- [ ] Add risk decisions table read model
- [ ] Add broker snapshots table read model
- [ ] Add system events table read model

### Dashboard UI

- [ ] Improve the Streamlit dashboard while keeping it read-only
- [ ] Add overview metrics:
  - cash
  - positions count
  - strategy runs count
  - orders count
  - approvals count
  - broker snapshots count
  - latest run time if available
- [ ] Add portfolio section with readable table
- [ ] Add strategy runs section
- [ ] Add orders section
- [ ] Add approvals section
- [ ] Add risk decisions section
- [ ] Add broker snapshots section
- [ ] Add system events or recent errors section
- [ ] Do not add buttons that execute orders
- [ ] Do not add controls to change risk limits
- [ ] Do not add live mode activation
- [ ] Do not add strategy enable/disable controls

### Orchestration

- [ ] Persist RiskDecision after risk check
- [ ] Keep current run-once behavior intact
- [ ] Optionally split run_once into small private methods only where it improves readability
- [ ] Do not create a large workflow engine
- [ ] Do not change strategy plugin contracts unless needed for DataHub compatibility

### Documentation

- [ ] Update README if needed
- [ ] Update ROADMAP.md only if v0.2 scope needs clarification
- [ ] Document the v0.2 DataHub payload shape
- [ ] Document that dashboard is read-only
- [ ] Document that real integrations remain deferred

### Verification

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `pytest -q`
- [ ] `maestro run-once --config configs/paper.yaml`
- [ ] `maestro run-once --config configs/csv_paper.yaml`
- [ ] `maestro status --config configs/paper.yaml`

## Next Milestone: v0.3 Planning

- [ ] Plan real Telegram Bot API integration boundary
- [ ] Plan approval callback persistence and duplicate-decision prevention
- [ ] Decide polling versus webhook for the first Telegram implementation
- [ ] Define paper-only approval behavior before live trading work begins

## Completed / Historical Notes

- v0.1.0 delivered the bootable paper-mode skeleton described in [ROADMAP.md](ROADMAP.md).
- v0.1.1 stabilized IDs, config validation, missing price behavior, execution engine validation, failure audit metadata, and docs consistency.

For version-level planning and future milestones, see [ROADMAP.md](ROADMAP.md).
