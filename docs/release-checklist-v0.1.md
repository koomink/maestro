# Maestro v0.1 Release Checklist

Use this checklist before tagging `v0.1.0`.

## Scope

- [ ] Paper mode is the official v0.1 execution mode.
- [ ] Strategy results are limited to `TargetAllocationResult`.
- [ ] Sample strategy remains an external package under `examples/`.
- [ ] Sample strategy imports only from `maestro.sdk`.
- [ ] Live trading is not implemented.
- [ ] Real Telegram Bot API calls are not implemented.
- [ ] Real KIS REST calls and order submission are not implemented.

## Fresh Environment Verification

Run from a clean clone:

```bash
uv sync --extra dev
uv pip install -e examples/sample_static_allocation
maestro run-once --config configs/paper.yaml
ruff check .
ruff format --check .
pytest -q
```

Expected:

- `maestro run-once --config configs/paper.yaml` exits successfully.
- SQLite state DB is created under `var/`.
- JSONL audit log is created under `var/`.
- Ruff check passes.
- Ruff format check passes.
- Pytest passes.

## Optional Foundation Checks

These are implemented foundations beyond the strict v0.1 paper skeleton:

```bash
maestro run-once --config configs/csv_paper.yaml
maestro run-once --config configs/approval_paper.yaml
maestro approvals --config configs/approval_paper.yaml
maestro kis-sync --config configs/live_readonly.yaml
maestro kis-account --config configs/live_readonly.yaml
```

## Release

```bash
git status --short
git tag v0.1.0
git push origin v0.1.0
```

Create a GitHub release note that calls out:

- v0.1 paper-mode skeleton.
- External strategy SDK/plugin boundary.
- State and audit logging.
- Phase 1/2/3 no-network foundations.
- Deferred real integrations.
