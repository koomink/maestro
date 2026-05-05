# TASKS

## Current Milestone: v0.1.1 Stabilization

- [x] Add separate ID generators: `new_run_id`, `new_order_id`, `new_approval_id`
- [x] Update OrderBuilder to use `new_order_id`
- [x] Update ApprovalManager to use `new_approval_id`
- [x] Add execution engine factory or validation for `execution.engine`
- [x] Reject unsupported `execution.engine` values clearly
- [x] Add `MissingPriceError`
- [x] Raise clear errors when position or target prices are missing
- [x] Add strict Pydantic config validation with `extra="forbid"`
- [x] Align README examples with actual config models
- [x] Remove or mark unimplemented YAML fields from docs
- [x] Add failure audit metadata: error type, message, traceback summary
- [x] Add tests for ID prefixes
- [x] Add tests for unsupported `execution.engine`
- [x] Add tests for missing price behavior
- [x] Add tests for strict config validation
- [x] Ensure sample configs still load
- [x] Ensure run-once still passes with `configs/paper.yaml`
- [x] Run `ruff check .`
- [x] Run `ruff format --check .`
- [x] Run `pytest -q`

## Next Milestone: v0.2 Planning

- [ ] Plan DataHub schema improvements
- [ ] Plan dashboard read models
- [ ] Decide whether to store RiskDecision as a separate SQLite table
- [ ] Decide how much to split MaestroOrchestrator before real integrations
- [ ] Decide SQLite WAL/timeout settings for dashboard + CLI coexistence

## Completed / Historical Notes

- v0.1.0 delivered the bootable paper-mode skeleton described in [ROADMAP.md](ROADMAP.md).
- Phase 1 foundations added CSV data and the read-only dashboard base.
- Phase 2 foundations added approval models, approval gate stubs, and Telegram formatting stubs.
- Phase 3 foundations added the KIS read-only mock adapter and broker snapshot storage.
- v0.1.1 stabilized IDs, config validation, missing price behavior, execution engine validation, failure audit metadata, and docs consistency.

For version-level planning and future milestones, see [ROADMAP.md](ROADMAP.md).
