# Daily Signal Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class daily Symphony orchestration command that refreshes broker truth, generates signal packages, sends a Telegram summary, and conditionally creates strategy-grouped approval requests.

**Architecture:** Reuse existing `run_signal()` and `approve_signal()` orchestrator APIs. Add CLI orchestration and summary notification in `src/maestro/cli.py`, and update approval grouping in `src/maestro/orchestration/orchestrator.py`.

**Tech Stack:** Python, Typer CLI, SQLite StateStore, Maestro orchestrator, pytest, ruff.

---

### Task 1: Strategy-Grouped Approval

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py`
- Test: `tests/test_signal_approval_handoff.py`

- [ ] Add a failing test where two strategies produce approval orders and `approve_signal()` persists two approval records.
- [ ] Group `approval_orders` by `order.metadata["source_strategy_ids"]`.
- [ ] Request approval once per group and persist each approval payload with its group strategy ids.
- [ ] Keep single-strategy behavior compatible with existing tests.
- [ ] Run `pytest tests/test_signal_approval_handoff.py -q`.

### Task 2: Daily CLI Orchestration

**Files:**
- Modify: `src/maestro/cli.py`
- Test: `tests/test_signal_approval_handoff.py`

- [x] Add `maestro daily-signal-approval`.
- [x] Resolve `--readonly-config`, `--signal-config`, and `--approval-config` from CLI options or `MAESTRO_*_CONFIG` env vars.
- [x] Run read-only KIS sync and reconciliation before signal generation.
- [x] Run `run_signal()` and echo `symphony_daily status=...` lines.
- [x] Send Telegram signal summary when Telegram config and token are available.
- [x] Call `approve_signal()` only when `action_required=true`.
- [x] Stop and restart `maestro-telegram-operator.service` around approval polling when requested.
- [x] Run focused CLI tests.

### Task 3: Systemd and Docs

**Files:**
- Modify: `deploy/systemd/maestro-symphony-signal.service`
- Modify: `docs/vps_systemd.md`
- Test: `tests/test_operator_deployment_wiring.py`

- [x] Replace the preferred scheduled signal path with `maestro daily-signal-approval`.
- [x] Keep the legacy shell wrapper documented as compatibility only.
- [x] Update deployment wiring tests to assert the new CLI command.
- [x] Run `pytest tests/test_operator_deployment_wiring.py -q`.

### Task 4: Verification

**Files:**
- No new files.

- [x] Run `pytest tests/test_signal_approval_handoff.py tests/test_operator_deployment_wiring.py -q`.
- [x] Run `ruff check src/maestro/cli.py src/maestro/orchestration/orchestrator.py tests/test_signal_approval_handoff.py tests/test_operator_deployment_wiring.py`.
- [x] Review `git diff` to ensure changes only cover daily signal approval.
