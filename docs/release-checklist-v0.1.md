# Maestro v0.1 / v0.1.1 Release Checklist

Status: completed for `v0.1.0`; updated for `v0.1.1` stabilization.

Release commit: `9f00a4a`

Tag: `v0.1.0`

## Scope

- [x] Paper mode is the official v0.1 execution mode.
- [x] Strategy results are limited to `TargetAllocationResult`.
- [x] Sample strategy remains an external package under `examples/`.
- [x] Sample strategy imports only from `maestro.sdk`.
- [x] Live trading is not implemented.
- [x] Real Telegram Bot API calls are not implemented.
- [x] Real KIS REST calls and order submission are not implemented.

## Fresh Environment Verification

Required commands:

```bash
uv sync --extra dev
uv pip install -e examples/sample_static_allocation
maestro run-once --config configs/paper.yaml
ruff check .
ruff format --check .
pytest -q
```

Verified locally before tagging:

- [x] `maestro run-once --config configs/paper.yaml` exits successfully.
- [x] SQLite state DB is created under `var/`.
- [x] JSONL audit log is created under `var/`.
- [x] `ruff check .` passes.
- [x] `ruff format --check .` passes.
- [x] `pytest -q` passes with 33 tests for v0.1.1.

## v0.1.1 Stabilization

- [x] Separate run, order, and approval ID generators.
- [x] Paper order IDs use `ord_`.
- [x] Approval request IDs use `appr_`.
- [x] Unsupported execution engines fail clearly.
- [x] Missing prices fail with `MissingPriceError`.
- [x] Config models forbid unknown YAML fields.
- [x] Failure audit logs include exception type, message, and traceback.
- [x] README and TRD config examples match actual config models.
- [x] Focused regression tests were added.

## Optional Foundation Checks

These are implemented foundations beyond the strict v0.1 paper skeleton:

```bash
maestro run-once --config configs/examples/paper_csv.yaml
maestro run-once --config configs/examples/paper_approval_console.yaml
maestro approvals --config configs/examples/paper_approval_console.yaml
maestro kis-sync --config configs/live_readonly.yaml
maestro kis-account --config configs/live_readonly.yaml
```

Verified locally:

- [x] CSV-backed paper run-once succeeds.
- [x] Approval-gated paper run-once succeeds.
- [x] Approval history CLI reads recorded decisions.
- [x] KIS read-only mock sync stores a broker account snapshot.
- [x] KIS account CLI reads the latest broker account snapshot.

## Release

```bash
git status --short
git tag v0.1.0
git push origin v0.1.0
```

Completed:

- [x] Working tree was clean before tagging.
- [x] `main` was pushed to `origin/main`.
- [x] Annotated tag `v0.1.0` was created.
- [x] Tag `v0.1.0` was pushed to GitHub.

GitHub release note should call out:

- v0.1 paper-mode skeleton.
- External strategy SDK/plugin boundary.
- State and audit logging.
- Phase 1/2/3 no-network foundations.
- Deferred real integrations.
