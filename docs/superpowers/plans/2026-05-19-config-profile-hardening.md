# Config Profile Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make Maestro operator profiles explicit, diffable, and less brittle while preserving existing single-YAML operator config workflows.

**Architecture:** Add a derived/validated `profile_stage` to `MaestroConfig`, split config identity into state-affecting and runtime fingerprints, add CLI tools for profile diff and production validation, and introduce `proposal_engine` as the clearer name for existing paper proposal generation. Keep existing YAML files backward compatible through migration aliases.

**Tech Stack:** Python, Pydantic config models, Typer CLI, pytest, ruff.

---

### Task 1: Profile Stage

**Files:**
- Modify: `src/maestro/core/enums.py`
- Modify: `src/maestro/config/models.py`
- Test: `tests/test_config_validation.py`

- [x] Write failing tests that load existing configs and assert a derived `profile_stage`.
- [x] Add `ProfileStage` enum and optional `profile_stage` field.
- [x] Derive stage from `mode`, `execution.order_posture`, `kis.paper_trading`, and DataHub provider shape when omitted.
- [x] Reject explicit `profile_stage` values that conflict with the config.

### Task 2: Config Identity Split

**Files:**
- Modify: `src/maestro/config/identity.py`
- Modify: `src/maestro/state/store.py`
- Test: `tests/test_config_validation.py`

- [x] Write failing tests showing runtime-only config changes keep the same state fingerprint.
- [x] Add `state_fingerprint` and `runtime_fingerprint` to `ConfigIdentity`.
- [x] Keep the existing `fingerprint` field as the full YAML fingerprint for compatibility.
- [x] Make StateStore identity validation compare only `state_fingerprint`.

### Task 3: Profile CLI

**Files:**
- Modify: `src/maestro/cli.py`
- Test: `tests/test_health_cli.py`

- [x] Write failing CLI tests for `profile-diff` and `profile-validate`.
- [x] Add `maestro profile-diff --left A --right B`.
- [x] Add `maestro profile-validate --config X --target-stage production_armed`.
- [x] Reuse existing beta preflight failures for production armed validation.

### Task 4: Proposal Engine Alias

**Files:**
- Modify: `src/maestro/config/execution.py`
- Modify: `src/maestro/execution/factory.py`
- Test: `tests/test_config_validation.py`

- [x] Write failing tests for `execution.proposal_engine`.
- [x] Migrate legacy `engine` to `proposal_engine`.
- [x] Keep `config.execution.engine` as a compatibility property.
- [x] Update factory error messages to refer to proposal engines.

### Task 5: Docs And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/TRD.md`
- Modify: `docs/live_account_promotion.md`

- [x] Document `profile_stage`, state/runtime fingerprints, profile diff, and profile validation.
- [x] Run focused tests and ruff.
- [x] Run broader config/health/operator test suites.
