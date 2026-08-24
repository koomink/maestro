# Phase 3a-5 (v2): upgrade/backfill · rollback preflight — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Supersedes:** `docs/superpowers/plans/2026-08-16-upgrade-backfill-rollback-preflight.md`
(retained as historical context; several of its tasks are obsolete or unsafe — see
"Reconciliation" below).

**Goal:** Give the funding/budget, approval and dispatch subsystems exactly one
authoritative current-generation state interpretation; migrate pre-3a-4 history under a
verified quiesce barrier with an immutable cutoff and fail-closed quarantine; and provide
a read-only rollback preflight that refuses rather than repairs.

**Architecture:** Four new modules behind two CLI commands. `state/migration_state.py`
owns the NOT_STARTED / MIGRATING / COMPLETED / INVALID state machine and the immutable
cutoff. `state/upgrade_backfill.py` owns deterministic legacy classification (funding head
backfill, approval quarantine, dispatch quarantine) and is the only writer during
migration. `state/rollback_preflight.py` owns R0–R4, strictly read-only.
`ops/quiesce.py` owns systemd writer/activator discovery and verification. The CLI is a
thin printer over structured result objects. Current runtime reads authoritative workflow
state; the legacy `contribution_*` terminal events remain **rollback compatibility
projections only**, still written atomically by `complete_workflow`.

**Tech Stack:** Python 3.11+, SQLite (`sqlite3`, WAL, `BEGIN IMMEDIATE`), typer, pytest,
ruff, systemd (`systemctl`).

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` —
「단계 3a는 roll-forward-only」 and 「3a 업그레이드 backfill」.
**Two spec steps are deliberately NOT implemented as written** (see Reconciliation §D).

---

## Verified starting state

- Branch `feat/funding-workflow-head-cas`, HEAD `cbbdb7fe28eb9f260cefd801673abfd359d4a030`
  — identical to the previously reviewed SHA; no commits added since the design review.
- Merge-base with `main` = `209ed4f18ed57773a72ab4a146e49efae1747348`, which is also
  `main`'s tip. Confirmed as the correct pre-3a-4 legacy baseline: it has **no**
  `src/maestro/state/funding_workflow.py` and **no** `signal_dispatch_manifest`.
- `git diff 209ed4f..HEAD -- src/maestro/state/store.py` contains **zero** DDL changes.
  The SQLite schema is byte-identical between the legacy baseline and HEAD. The 3a-4
  upgrade is an *event-semantics* upgrade, not a schema upgrade. Tests must assert this
  rather than pretend a DDL migration happens.
- Baseline verification at HEAD: `pytest -q` → 1778 passed, 9 skipped;
  `ruff check src tests` → clean.

## Mixed-generation inventory

| # | Seam | Old generation | New generation | Current reader(s) | Current writer(s) | Authoritative after 3a-5 | Status |
|---|------|----------------|----------------|-------------------|-------------------|--------------------------|--------|
| 1 | funding/budget request lifecycle | `contribution_funding_request_ack`, `contribution_budget_request_decision` | `funding_workflow_head` / `_claim` / `_completed` / `_superseded` | `_load_pending_funding_request`, `_load_pending_budget_request` (handlers) | `complete_workflow` (atomic dual-write) | workflow events | **Task 3** refactors readers. Legacy events demoted to rollback compatibility projection. |
| 2 | selected budget amount | `contribution_budget_request_decision.selected_budget` | (no equivalent field on `funding_workflow_completed`) | `orchestrator._selected_contribution_budget` | `complete_workflow` | workflow completion decides *whether* the decision counts; the amount is still read from the projection it writes atomically | **Task 3b** gates the read on a corroborating completion for post-cutoff rows; pre-cutoff rows are historical state. |
| 3 | approval acknowledgement | schema-less `telegram_approval_ack` | `telegram_approval_ack` with `schema_version >= 2` + `telegram_approval_resolution_completed` | `_terminal_approval_ids`, `_resume_unresolved_approvals`, `ops/batch_execution` | handlers, `insert_approval_resolution` | versioned ack + resolution | **Already fail-closed at HEAD** (schema-less acks are never auto-executed). 3a-5 adds *operator visibility* only (Task 8 quarantine). No synthesis. |
| 4 | signal dispatch resume | `signal_package_consumed` with no manifest | `signal_dispatch_manifest` → consumed → dispatch → settled | `list_incomplete_signal_dispatches`, `_resume_incomplete_dispatches` | orchestrator | manifest | **Task 9** makes a manifest a prerequisite for automatic resume; manifest-less consumed runs are quarantined and escalated to the operator. |
| 5 | migration ownership | none | `funding_workflow_migration_started` (cutoff only) | `load_migration_cutoff` | nothing yet (3a-5 writes it) | full state machine | **Task 4** replaces the bare cutoff read with NOT_STARTED/MIGRATING/COMPLETED/INVALID. |
| 6 | convergence sweep stale-snapshot race | — | — | — | — | — | **Already solved at HEAD.** Both loops in `converge_workflow_invariants` pin their writes with `require_duplicate_keys`/`forbid_duplicate_keys` re-evaluated inside the same transaction. The old plan's Global Constraint about this is stale. **Do not add a second repair mechanism.** |
| 7 | systemd quiesce barrier | `cli._service_is_active("maestro-telegram-operator.service")` (one unit) | — | `approval-rollback-preflight --require-quiesce` | — | full writer + activator inventory | **Task 5**. The dashboard is a writer (`POST /api/dashboard/refresh`, `POST .../generate-signal`) despite its name. `maestro-run-once.service` has `ExecStopPost=systemctl start maestro-telegram-operator.service` — a shutdown-ordering hazard. |
| 8 | fill watermark baseline | `_migrate_legacy_baseline_fill_watermarks` | same | store | store | store | Historical, self-contained, idempotent, unaffected. No 3a-5 action. |

## Reconciliation of the old 3a-5 plan against current HEAD

**A. Still required as written**
- Old Task 2 (migration marker + immutable cutoff) → **Task 4**, extended to a full
  state machine with INVALID.
- Old Task 3 (legacy pending → v1 head) → **Task 7**, with a stricter case table.
- Old Task 5/6 (CLI + `migration_completed` gate) → **Tasks 10, 11**.
- Old Task 8 (real old-schema DB fixture) → **Task 15**, corrected: the schema is
  identical, so the fixture proves *event-generation* upgrade, and the test asserts DDL
  equivalence explicitly instead of implying a DDL migration.
- Old Task 9 (runbook) → **Task 16**.

**B. Still required but the design must change**
- Old Task 1 (quiesce = a `WRITER_UNITS` tuple checked with `systemctl is-active`) →
  **Task 5**: its unit list is stale and incomplete (it omits the dashboard, all
  read-only/rebalance timers, the `.path` unit, the health/reload/src-watch restart
  helpers). Quiesce now also requires no queued systemd jobs, and the whole operation
  holds the StateStore writer lock (**Task 6**).
- Old Task 7 (rollback preflight, 4 checks, "idempotently backfill the missing legacy
  event and re-check") → **Task 13**, with the backfill **removed**: preflight is an
  inspector, not a repair tool (see D).

**C. Removed — already solved by 3a-4 / current HEAD**
- The old plan's Global Constraint requiring the convergence sweep's stale-snapshot
  defect to be closed before enabling the sweep. HEAD already pins every sweep write to
  atomic head preconditions. Reimplementing it would create a second, competing repair
  mechanism.
- The old plan's premise that enabling the sweep needs a new guard: `cutoff is None`
  already disables it, and Task 4 keeps that property.

**D. Removed — unsafe**
- Old Task 4 (classify legacy approval acks and **backfill
  `telegram_approval_resolution_completed`**, treating "no completion evidence" as
  cancellation). This synthesizes history from absence. A broker side effect may have
  occurred before local persistence completed. Replaced by **Task 8**: classify and
  quarantine, never synthesize. Current HEAD already refuses to auto-execute a
  schema-less ack, so nothing is lost.
- The spec's rollback step "발견 시 롤백 CLI가 legacy 이벤트를 멱등 backfill한 뒤
  재검사" — replaced by **fail and report** (Task 13, R4). A missing projection means
  corruption, manual mutation or an intermediate build; auto-repair destroys the
  evidence.

**E. Newly added (absent from the old plan)**
- **Task 2/3**: the old plan never removed the current runtime's dependency on legacy
  terminal events. Without this the system still has two competing definitions of
  "pending".
- **Task 9**: manifest-as-resume-fence. The manifest did not exist when the old plan
  was written.
- **Task 12**: crash-injection tests across every migration write boundary.
- **Task 14**: rollback → old-code writes → re-upgrade fail-closed detection.
- **Task 6**: whole-operation StateStore writer lock.

## Global Constraints

- **Financial safety priority order** (applies whenever requirements conflict):
  1. no duplicate financial effect; 2. no lost/incorrect workflow ownership;
  3. crash-safe deterministic recovery; 4. operator visibility / quarantine;
  5. convenience / automatic repair.
- **FAIL CLOSED on ambiguous history.** Absence of evidence is never evidence of
  non-execution. Prefer quarantine + operator review over inference.
- **`migration_completed` MUST be the final migration write.** No compatibility write,
  quarantine, head creation or backfill may occur after it is persisted.
- **The cutoff is immutable.** A rerun after a crash reuses the exact same cutoff and
  never chooses a new one.
- **Every migration write is deterministic and idempotent**: `duplicate_key` derived only
  from stable identifiers, payloads free of timestamps and random values. A payload with a
  clock in it makes a crash-resume die with `ValueError` on the content comparison
  (`store.py` `save_system_events_atomic` replay verification).
- **Rollback preflight is read-only.** It may not write, repair, normalize or backfill.
  (`_state_store()` still runs `StateStore._init_db`, which is additive-only DDL the old
  binary also runs — that is unchanged and documented in the existing CLI docstring.)
- **The migration/preflight holds `store.writer_lock` for the whole operation**, not per
  insert. `writer_lock` is re-entrant within a thread, so inner `save_system_events_atomic`
  calls nest safely.
- **Do not build a generic multi-epoch migration framework.** Detect re-upgrade-after-
  rollback and refuse. YAGNI.
- **Do not remove the legacy `contribution_*` terminal events from writes.** They remain
  the rollback compatibility projection, written atomically by `complete_workflow`.
- Tests: `pytest -q`. Lint: `ruff check src tests`.

## File Structure

**Create**
- `src/maestro/state/migration_state.py` — `MigrationPhase`, `MigrationState`,
  `MigrationStateInvalid`, `load_migration_state`, `start_migration`,
  `complete_migration`, event/key constants. No backfill logic.
- `src/maestro/state/upgrade_backfill.py` — legacy classification and the only migration
  writes: funding v1 head backfill, approval quarantine, dispatch quarantine,
  re-upgrade-after-rollback detection, `run_upgrade_backfill` orchestration and its
  result objects.
- `src/maestro/state/rollback_preflight.py` — `RollbackPreflightResult`,
  `InvariantFailure`, `run_rollback_preflight`. Read-only.
- `src/maestro/ops/quiesce.py` — unit inventory, `UnitState`, `verify_quiesced`,
  `capture_unit_states`, `QUIESCE_STOP_ORDER`.
- `tests/test_migration_state.py`
- `tests/test_upgrade_backfill_heads.py`
- `tests/test_upgrade_backfill_quarantine.py`
- `tests/test_upgrade_backfill_cli.py`
- `tests/test_rollback_preflight.py`
- `tests/test_quiesce_units.py`
- `tests/test_migration_runtime_gates.py`
- `tests/test_authoritative_funding_state.py`
- `tests/test_upgrade_backfill_fixture.py`
- `tests/fixtures/legacy_3a_state.sql` + `tests/fixtures/legacy_3a_state.json`
- `scripts/generate_legacy_3a_fixture.py`
- `docs/rollback_and_upgrade_3a.md`

**Modify**
- `src/maestro/state/funding_workflow.py` — add authoritative readers
  (`request_terminal_state`, `is_request_pending`); delegate `load_migration_cutoff` to
  the state machine.
- `src/maestro/integrations/telegram/handlers.py` — authoritative pending loaders,
  migration gates, manifest resume fence.
- `src/maestro/orchestration/orchestrator.py` — gate `_selected_contribution_budget`.
- `src/maestro/state/store.py` — `list_incomplete_signal_dispatches(limit: int | None)`.
- `src/maestro/cli.py` — `upgrade-backfill`, rewritten `rollback-preflight`
  (`approval-rollback-preflight` kept as an alias), `quiesce-status`.
- `docs/operator_runbook.md`, `docs/vps_systemd.md` — link the new runbook.

---

### Task 1: Authoritative funding/budget request state readers

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Test: `tests/test_authoritative_funding_state.py`

**Interfaces:**
- Produces:
  - `TERMINAL_WORKFLOW_EVENTS: tuple[str, str] = ("funding_workflow_completed", "funding_workflow_superseded")`
  - `request_terminal_state(store, request_id: str, phase: str) -> str | None` —
    `"completed"`, `"superseded"` or `None`. Reads **only** workflow events.
  - `is_request_pending(store, request_id: str, phase: str) -> bool` — the request event
    exists with `status == "pending"` and `request_terminal_state(...) is None`.
  - `load_request_payload(store, request_id: str, phase: str) -> dict[str, Any] | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_authoritative_funding_state.py
from maestro.state import funding_workflow as fw


def test_a_completed_workflow_is_terminal_without_any_legacy_projection(store):
    """The current runtime must understand its own completion.

    The legacy contribution_* event is a rollback compatibility projection.
    Deleting it here (a thing production never does -- complete_workflow writes
    both atomically) proves the current reader does not depend on it.
    """
    _publish_pending_funding_request(store, "req-1")
    _claim_and_complete(store, "req-1", phase="funding")
    _delete_events(store, "contribution_funding_request_ack")

    assert fw.request_terminal_state(store, "req-1", "funding") == "completed"
    assert fw.is_request_pending(store, "req-1", "funding") is False


def test_a_legacy_ack_alone_no_longer_makes_a_request_terminal(store):
    """Pre-3a-4 history: an ack with no workflow completion behind it.

    It is historical state, not current authoritative truth. The authoritative
    reader reports the request as still pending; the migration (Task 7) is what
    classifies it, not this reader.
    """
    _publish_pending_funding_request(store, "req-1")
    _write_legacy_ack_only(store, "req-1")

    assert fw.request_terminal_state(store, "req-1", "funding") is None


def test_a_superseded_request_is_terminal(store):
    _publish_pending_funding_request(store, "req-1")
    _publish_pending_funding_request(store, "req-2")  # supersedes req-1

    assert fw.request_terminal_state(store, "req-1", "funding") == "superseded"
    assert fw.is_request_pending(store, "req-1", "funding") is False
    assert fw.is_request_pending(store, "req-2", "funding") is True


def test_phase_is_not_ignored_when_deciding_completion(store):
    """A budget completion must not close the funding request of the same id."""
    _publish_pending_funding_request(store, "req-1")
    _write_completion(store, "req-1", phase="budget")

    assert fw.request_terminal_state(store, "req-1", "funding") is None
```

Helpers live at the top of the test module; build them on `tests/contribution_fixtures.py`
and `publish_contribution_request` / `claim_workflow_attempt` / `complete_workflow`.
`_delete_events` executes `DELETE FROM system_events WHERE event_type = ?` directly on
`store.path` with `sqlite3`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py -q`
Expected: FAIL — `AttributeError: module 'maestro.state.funding_workflow' has no attribute 'request_terminal_state'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/maestro/state/funding_workflow.py`, after `superseded_key`:

```python
TERMINAL_WORKFLOW_EVENTS: tuple[str, str] = (
    "funding_workflow_completed",
    "funding_workflow_superseded",
)


def request_terminal_state(store: StateStore, request_id: str, phase: str) -> str | None:
    """Whether this request's transition is over, per *workflow* state alone.

    Deliberately blind to ``contribution_*_request_ack`` /
    ``contribution_budget_request_decision``. Those are the rollback
    compatibility projection ``complete_workflow`` writes for the old binary,
    not a second opinion the current runtime is allowed to consult: two
    definitions of "finished" is exactly the mixed-generation condition 3a-5
    exists to remove, and the legacy one cannot express phase, attempt or
    lineage. A pre-3a-4 ack with no completion behind it is *history*, and
    reports ``None`` here on purpose -- the upgrade backfill classifies it,
    under a quiesce barrier, rather than this reader guessing at runtime.
    """
    _require_phase(phase)
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("request_id") or "") == request_id
            and str(payload.get("phase") or "") == phase
        ):
            return "completed"
    # Supersession is phase-agnostic: the marker names the request the head
    # moved off, and a request only ever exists in one phase.
    for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
        payload = row.get("payload") or {}
        if str(payload.get("request_id") or "") == request_id:
            return "superseded"
    return None


def load_request_payload(
    store: StateStore, request_id: str, phase: str
) -> dict[str, Any] | None:
    _require_phase(phase)
    for row in store.list_system_events_by_type(_REQUEST_EVENT[phase], limit=None):
        payload = row.get("payload") or {}
        if str(payload.get("request_id") or "") == request_id:
            return dict(payload)
    return None


def is_request_pending(store: StateStore, request_id: str, phase: str) -> bool:
    payload = load_request_payload(store, request_id, phase)
    if payload is None or payload.get("status") != "pending":
        return False
    return request_terminal_state(store, request_id, phase) is None
```

Add all four names to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/funding_workflow.py tests/test_authoritative_funding_state.py
git commit -m "feat(3a-5): read funding/budget request state from the workflow, not the legacy projection"
```

---

### Task 2: Expose the handlers' remaining legacy-read dependency

**Files:**
- Test: `tests/test_authoritative_funding_state.py` (append)

**Interfaces:**
- Consumes: Task 1's readers; `operator_bot` fixture from `tests/test_funding_workflow_resume.py`
  (import the existing fixture module or reuse its construction helper).

- [ ] **Step 1: Write the failing test**

```python
def test_the_pending_loader_does_not_need_the_legacy_projection(operator_bot, store):
    """Current runtime truth != rollback compatibility requirement.

    Same scenario as the R4 rollback test in tests/test_rollback_preflight.py,
    read from the other side: with the projection absent the current loader must
    still see the request as finished, while preflight must still refuse the
    rollback. Neither is derivable from the other.
    """
    _publish_pending_funding_request(store, "req-1")
    _claim_and_complete(store, "req-1", phase="funding")
    _delete_events(store, "contribution_funding_request_ack")

    assert operator_bot._load_pending_funding_request("req-1") is None


def test_a_bare_legacy_ack_no_longer_hides_a_live_request(operator_bot, store):
    """A pre-3a-4 ack is history. It must not be the thing that decides.

    Before 3a-5 this returned None purely because an ack row existed. After
    3a-5 the workflow decides, and a request the workflow still owns stays
    visible so the migration -- not a silent runtime read -- classifies it.
    """
    _publish_pending_funding_request(store, "req-1")
    _write_legacy_ack_only(store, "req-1")

    assert operator_bot._load_pending_funding_request("req-1") is not None


def test_the_budget_loader_follows_the_same_rule(operator_bot, store):
    _publish_pending_budget_request(store, "req-b")
    _claim_and_complete(store, "req-b", phase="budget")
    _delete_events(store, "contribution_budget_request_decision")

    assert operator_bot._load_pending_budget_request("req-b") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py -q`
Expected: FAIL — the first test returns the request payload (the loader keys off the
deleted ack), and the second returns `None`.

- [ ] **Step 3: No implementation yet**

This task is the failing-test gate for Task 3. Do not commit a passing state here.

- [ ] **Step 4: Commit the tests as expected-failing**

Skip. Fold the commit into Task 3.

---

### Task 3: Route the funding/budget runtime through authoritative workflow state

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:4507-4523` (`_load_pending_budget_request`),
  `:4573-4589` (`_load_pending_funding_request`)
- Test: `tests/test_authoritative_funding_state.py`

**Interfaces:**
- Consumes: `funding_workflow.is_request_pending`, `funding_workflow.load_request_payload`

- [ ] **Step 1: Write minimal implementation**

Replace both loaders with:

```python
    def _load_pending_budget_request(self, request_id: str) -> dict[str, Any] | None:
        """The request payload, if the *workflow* still says this request is open.

        Was: "no contribution_budget_request_decision row exists". That test is
        the rollback compatibility projection, and using it here made the legacy
        event a second, competing definition of "pending" -- one that cannot see
        supersession, phase or attempt. It is retained as a projection
        complete_workflow writes atomically, and it is what rollback preflight
        checks (R4), but it is no longer consulted to decide anything.
        """
        if not is_request_pending(self.store, request_id, "budget"):
            return None
        return load_request_payload(self.store, request_id, "budget")

    def _load_pending_funding_request(self, request_id: str) -> dict[str, Any] | None:
        """See _load_pending_budget_request: workflow state decides, not the
        legacy contribution_funding_request_ack projection."""
        if not is_request_pending(self.store, request_id, "funding"):
            return None
        return load_request_payload(self.store, request_id, "funding")
```

Add `is_request_pending, load_request_payload` to the existing
`from maestro.state.funding_workflow import (...)` block.

- [ ] **Step 2: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py -q`
Expected: PASS

- [ ] **Step 3: Run every test that touches these loaders**

Run:
```
.venv/bin/python -m pytest tests/test_funding_workflow_resume.py \
  tests/test_funding_workflow_transitions.py tests/test_telegram_operator_ui.py \
  tests/test_multi_account_contributions.py -q
```
Expected: PASS. If a test fails because it asserted the *legacy* behaviour
(`_load_pending_*` returning None purely because an ack row exists), that assertion
encoded the old generation as current truth — update it to construct a real workflow
completion, and note the change in the commit message. Do not weaken the loaders to keep
such a test green.

- [ ] **Step 4: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/
git commit -m "fix(3a-5): let the workflow, not the legacy ack, decide whether a request is pending"
```

---

### Task 3b: Gate the selected-budget read on an authoritative completion

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:4756-4781` (`_selected_contribution_budget`)
- Test: `tests/test_authoritative_funding_state.py` (append)

**Interfaces:**
- Consumes: `funding_workflow.load_migration_cutoff`, `funding_workflow.request_terminal_state`

**Design note:** `selected_budget` exists *only* on the legacy decision payload — there is
no field for it on `funding_workflow_completed`. Rather than invent a second record of the
amount (a new source of truth, forbidden by the scope rules), the *lifecycle* decision is
made authoritative and the amount is read from the record `complete_workflow` writes in the
same transaction, so the two can never disagree. A post-cutoff decision row with no
completion behind it can only come from an old binary or a manual mutation, and is refused
loudly rather than silently falling back to `available_cash` — falling back would invest
more than the operator chose.

- [ ] **Step 1: Write the failing test**

```python
def test_a_post_cutoff_decision_without_a_completion_is_refused(store, orchestrator):
    """A decision row the workflow never completed is not an amount to invest.

    Silently ignoring it falls back to available_cash -- i.e. investing *more*
    than the operator chose. Fail closed instead.
    """
    _write_migration_markers(store, cutoff=0)
    _write_legacy_decision_only(store, "req-b", selected_budget=500_000.0)

    with pytest.raises(ValueError, match="uncorroborated"):
        orchestrator._selected_contribution_budget(
            "grp", "tranquillo", "acct", "sleeve", "2026-08"
        )


def test_a_pre_cutoff_decision_is_honored_as_history(store, orchestrator):
    _write_legacy_decision_only(store, "req-b", selected_budget=500_000.0)
    cutoff = _max_event_id(store)
    _write_migration_markers(store, cutoff=cutoff)

    assert orchestrator._selected_contribution_budget(
        "grp", "tranquillo", "acct", "sleeve", "2026-08"
    ) == 500_000.0


def test_a_completed_decision_is_honored(store, orchestrator):
    _write_migration_markers(store, cutoff=0)
    _publish_pending_budget_request(store, "req-b")
    _claim_and_complete(store, "req-b", phase="budget", selected_budget=500_000.0)

    assert orchestrator._selected_contribution_budget(
        "grp", "tranquillo", "acct", "sleeve", "2026-08"
    ) == 500_000.0


def test_without_a_migration_cutoff_behaviour_is_unchanged(store, orchestrator):
    """No cutoff means pre- and post-3a-5 rows are indistinguishable.

    Guessing there would be worse than the status quo, and the migration gate
    (Task 10) is what stops the system running in this state for long.
    """
    _write_legacy_decision_only(store, "req-b", selected_budget=500_000.0)

    assert orchestrator._selected_contribution_budget(
        "grp", "tranquillo", "acct", "sleeve", "2026-08"
    ) == 500_000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py -k selected -q`
Expected: FAIL — no `ValueError` is raised; the uncorroborated row is honored.

- [ ] **Step 3: Write minimal implementation**

In `_selected_contribution_budget`, capture the cutoff once before the loop and validate
each matching row:

```python
        cutoff = load_migration_cutoff(self.state_store)
        for row in self.state_store.list_system_events_by_type(
            "contribution_budget_request_decision",
            limit=1000,
        ):
            payload = row.get("payload") or {}
            ...  # existing scope/status filters, unchanged
            request_id = str(payload.get("request_id") or "")
            # The decision event is the rollback compatibility projection, and
            # complete_workflow writes it in the same transaction as the
            # workflow completion -- so for anything this generation wrote, a
            # corroborating completion always exists. One that is missing above
            # the migration cutoff came from an old binary or a manual
            # mutation, and the amount it names is not one this runtime may
            # act on. Refusing is the fail-closed choice: skipping the row
            # would fall through to available_cash and invest *more* than the
            # operator selected. Below the cutoff the row is legitimate
            # pre-3a-4 history and is honored as-is.
            if cutoff is not None and int(row.get("id") or 0) > cutoff:
                if request_terminal_state(
                    self.state_store, request_id, "budget"
                ) != "completed":
                    raise ValueError(
                        "uncorroborated contribution budget decision for "
                        f"request_id={request_id}: no funding_workflow_completed "
                        "backs it. Run `maestro rollback-preflight` and check for "
                        "old-binary writes before trading against this amount."
                    )
            return float(payload["selected_budget"])
```

Import `load_migration_cutoff` and `request_terminal_state` from
`maestro.state.funding_workflow`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_authoritative_funding_state.py tests/test_multi_account_contributions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_authoritative_funding_state.py
git commit -m "fix(3a-5): refuse a budget amount the workflow never completed"
```

---

### Task 4: Migration state machine with an immutable cutoff

**Files:**
- Create: `src/maestro/state/migration_state.py`
- Modify: `src/maestro/state/funding_workflow.py` (`load_migration_cutoff` delegates)
- Test: `tests/test_migration_state.py`

**Interfaces:**
- Produces:
  - `MIGRATION_ID = "3a"`
  - `STARTED_EVENT = "funding_workflow_migration_started"`,
    `COMPLETED_EVENT = "funding_workflow_migration_completed"`
  - `STARTED_KEY = "migration-started:3a"`, `COMPLETED_KEY = "migration-completed:3a"`
  - `class MigrationPhase(StrEnum): NOT_STARTED / MIGRATING / COMPLETED / INVALID`
  - `@dataclass(frozen=True) class MigrationState: phase: MigrationPhase; cutoff: int | None; reason: str | None`
  - `class MigrationStateInvalid(RuntimeError)` carrying `.reason`
  - `load_migration_state(store) -> MigrationState`
  - `start_migration(store, run_id) -> MigrationState` — under a held writer lock:
    reuses an existing cutoff, otherwise `cutoff = MAX(system_events.id)` then writes
    `migration_started`.
  - `complete_migration(store, run_id, *, cutoff: int) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_state.py
import pytest

from maestro.state import migration_state as ms


def test_a_fresh_database_has_not_started(store):
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.NOT_STARTED


def test_starting_pins_the_cutoff_to_the_current_max_event_id(store):
    _write_n_events(store, 5)
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
    assert state.phase is ms.MigrationPhase.MIGRATING
    assert state.cutoff == _max_event_id_excluding_marker(store)


def test_a_second_start_reuses_the_exact_same_cutoff(store):
    with store.writer_lock("test"):
        first = ms.start_migration(store, "run-1")
    _write_n_events(store, 10)          # a crash-and-retry sees a bigger table
    with store.writer_lock("test"):
        second = ms.start_migration(store, "run-2")
    assert second.cutoff == first.cutoff
    assert len(store.list_system_events_by_type(ms.STARTED_EVENT, limit=None)) == 1


def test_completion_records_the_same_cutoff_and_reports_completed(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
        ms.complete_migration(store, "run-1", cutoff=state.cutoff)
    final = ms.load_migration_state(store)
    assert final.phase is ms.MigrationPhase.COMPLETED
    assert final.cutoff == state.cutoff


def test_completing_with_a_different_cutoff_is_refused(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-1")
        with pytest.raises(ms.MigrationStateInvalid):
            ms.complete_migration(store, "run-1", cutoff=state.cutoff + 1)


def test_a_completed_marker_with_no_start_is_invalid(store):
    _raw_event(store, ms.COMPLETED_EVENT, {"cutoff": 7, "duplicate_key": ms.COMPLETED_KEY})
    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.INVALID
    assert state.reason == "completed_without_started"


def test_a_malformed_cutoff_is_invalid_not_zero(store):
    """Reading a malformed marker as cutoff=0 would make every legacy request
    look post-cutoff and expose it to the convergence sweep."""
    _raw_event(store, ms.STARTED_EVENT, {"cutoff": "seven", "duplicate_key": ms.STARTED_KEY})
    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.INVALID
    assert state.reason == "malformed_started_marker"


def test_conflicting_start_cutoffs_are_invalid(store):
    _raw_event(store, ms.STARTED_EVENT, {"cutoff": 7, "duplicate_key": ms.STARTED_KEY})
    _raw_event(store, ms.STARTED_EVENT, {"cutoff": 9, "duplicate_key": "migration-started:3a:dup"})
    assert ms.load_migration_state(store).reason == "conflicting_start_cutoffs"


def test_conflicting_start_and_completed_cutoffs_are_invalid(store):
    _raw_event(store, ms.STARTED_EVENT, {"cutoff": 7, "duplicate_key": ms.STARTED_KEY})
    _raw_event(store, ms.COMPLETED_EVENT, {"cutoff": 9, "duplicate_key": ms.COMPLETED_KEY})
    assert ms.load_migration_state(store).reason == "cutoff_mismatch"


def test_load_migration_cutoff_raises_on_invalid_state(store):
    """funding_workflow.load_migration_cutoff feeds the convergence sweep. An
    INVALID database must stop the sweep, not hand it a guessed boundary."""
    from maestro.state.funding_workflow import load_migration_cutoff

    _raw_event(store, ms.STARTED_EVENT, {"cutoff": "seven", "duplicate_key": ms.STARTED_KEY})
    with pytest.raises(ms.MigrationStateInvalid):
        load_migration_cutoff(store)


def test_load_migration_cutoff_still_returns_none_before_any_migration(store):
    from maestro.state.funding_workflow import load_migration_cutoff

    assert load_migration_cutoff(store) is None
```

`_raw_event` uses `store.save_system_event(run_id, event_type, payload)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_migration_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.migration_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/maestro/state/migration_state.py
"""The 3a migration's own ownership record.

Not a boolean. A migration that can crash between its first and last write
needs four distinguishable answers -- nobody owns this database yet, someone
does and is not finished, someone finished, and the markers contradict each
other -- because three of them demand different behaviour from the runtime and
the fourth must stop it. A flag collapses the middle two, which is exactly the
state where funding callbacks and recovery sweeps must not run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

MIGRATION_ID = "3a"
STARTED_EVENT = "funding_workflow_migration_started"
COMPLETED_EVENT = "funding_workflow_migration_completed"
STARTED_KEY = f"migration-started:{MIGRATION_ID}"
COMPLETED_KEY = f"migration-completed:{MIGRATION_ID}"


class MigrationPhase(StrEnum):
    NOT_STARTED = "not_started"
    MIGRATING = "migrating"
    COMPLETED = "completed"
    INVALID = "invalid"


@dataclass(frozen=True)
class MigrationState:
    phase: MigrationPhase
    cutoff: int | None = None
    reason: str | None = None


class MigrationStateInvalid(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"migration state is invalid: {reason}")
        self.reason = reason


def _cutoffs(store: StateStore, event_type: str) -> list[int] | None:
    """Distinct cutoffs recorded by ``event_type``, or None if any is malformed."""
    values: set[int] = set()
    for row in store.list_system_events_by_type(event_type, limit=None):
        raw = (row.get("payload") or {}).get("cutoff")
        # bool is an int subclass and would silently become 0/1.
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return None
        values.add(raw)
    return sorted(values)


def load_migration_state(store: StateStore) -> MigrationState:
    started = _cutoffs(store, STARTED_EVENT)
    if started is None:
        return MigrationState(MigrationPhase.INVALID, reason="malformed_started_marker")
    completed = _cutoffs(store, COMPLETED_EVENT)
    if completed is None:
        return MigrationState(MigrationPhase.INVALID, reason="malformed_completed_marker")
    if len(started) > 1:
        return MigrationState(MigrationPhase.INVALID, reason="conflicting_start_cutoffs")
    if len(completed) > 1:
        return MigrationState(MigrationPhase.INVALID, reason="conflicting_completed_cutoffs")
    if not started:
        if completed:
            return MigrationState(MigrationPhase.INVALID, reason="completed_without_started")
        return MigrationState(MigrationPhase.NOT_STARTED)
    cutoff = started[0]
    if not completed:
        return MigrationState(MigrationPhase.MIGRATING, cutoff=cutoff)
    if completed[0] != cutoff:
        return MigrationState(MigrationPhase.INVALID, reason="cutoff_mismatch")
    return MigrationState(MigrationPhase.COMPLETED, cutoff=cutoff)


def _max_event_id(store: StateStore) -> int:
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM system_events").fetchone()
    return int(row[0])


def start_migration(store: StateStore, run_id: str) -> MigrationState:
    """Take (or re-adopt) ownership of this database's 3a migration.

    The caller must already hold ``store.writer_lock`` for the whole operation.
    Observing MAX(id) and writing the marker are only one decision if no
    cooperating writer can append between them -- otherwise the cutoff names a
    boundary that had already moved, and requests written into that gap are
    classified as legacy when they are not.

    A crash after this point must never pick a new cutoff: the heads and
    quarantines already written were classified against the old one, and a
    second boundary would reclassify them into a different generation.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("start_migration requires the StateStore writer lock")
    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    if state.phase in (MigrationPhase.MIGRATING, MigrationPhase.COMPLETED):
        return state
    cutoff = _max_event_id(store)
    store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": STARTED_EVENT,
                "payload": {
                    "duplicate_key": STARTED_KEY,
                    "migration_id": MIGRATION_ID,
                    "cutoff": cutoff,
                },
            }
        ],
        forbid_duplicate_keys=(STARTED_KEY,),
    )
    return MigrationState(MigrationPhase.MIGRATING, cutoff=cutoff)


def complete_migration(store: StateStore, run_id: str, *, cutoff: int) -> None:
    """The migration's final write. Nothing may be written after it.

    Requiring the started marker atomically is what stops a completed marker
    from existing over a migration nobody owns; forbidding its own key makes a
    re-run a no-op rather than a conflicting overlap.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("complete_migration requires the StateStore writer lock")
    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    if state.cutoff != cutoff:
        raise MigrationStateInvalid("cutoff_mismatch")
    store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": COMPLETED_EVENT,
                "payload": {
                    "duplicate_key": COMPLETED_KEY,
                    "migration_id": MIGRATION_ID,
                    "cutoff": cutoff,
                },
            }
        ],
        require_duplicate_keys=(STARTED_KEY,),
        forbid_duplicate_keys=(COMPLETED_KEY,),
    )


__all__ = [
    "COMPLETED_EVENT",
    "COMPLETED_KEY",
    "MIGRATION_ID",
    "MigrationPhase",
    "MigrationState",
    "MigrationStateInvalid",
    "STARTED_EVENT",
    "STARTED_KEY",
    "complete_migration",
    "load_migration_state",
    "start_migration",
]
```

Then replace `funding_workflow.load_migration_cutoff`'s body:

```python
def load_migration_cutoff(store: StateStore) -> int | None:
    """3a 업그레이드 backfill이 고정한 경계. 3a-5가 이 이벤트를 기록한다.

    (docstring 유지 -- 아래 두 문단은 그대로.)

    판정은 ``migration_state``가 단독으로 소유한다. 마커가 서로 모순되면
    ``MigrationStateInvalid``를 올린다: 여기서 조용히 ``None``을 돌려주면
    수렴 sweep이 "3a 이전 DB"와 "마커가 깨진 DB"를 구분하지 못한 채 같은
    무행동을 하고, 운영자는 아무 신호도 받지 못한다.
    """
    from maestro.state.migration_state import (
        MigrationPhase,
        MigrationStateInvalid,
        load_migration_state,
    )

    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    return state.cutoff
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_migration_state.py tests/test_funding_workflow_head.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/migration_state.py src/maestro/state/funding_workflow.py tests/test_migration_state.py
git commit -m "feat(3a-5): give the migration four states and one immutable cutoff"
```

---

### Task 5: Systemd writer/activator inventory and quiesce verification

**Files:**
- Create: `src/maestro/ops/quiesce.py`
- Test: `tests/test_quiesce_units.py`

**Interfaces:**
- Produces:
  - `WRITER_UNITS: tuple[str, ...]` — units that write the state DB
  - `ACTIVATOR_UNITS: tuple[str, ...]` — timers/path/watch/restart helpers that can start a writer
  - `NON_WRITER_UNITS: tuple[str, ...]` — explicitly reviewed and cleared
  - `QUIESCE_STOP_ORDER: tuple[str, ...]`
  - `@dataclass(frozen=True) class UnitState: unit: str; active: bool; enabled: str`
  - `capture_unit_states(units=..., *, run=subprocess.run) -> list[UnitState]`
  - `@dataclass(frozen=True) class QuiesceReport: active_units: tuple[str, ...]; queued_jobs: tuple[str, ...]` with `.quiesced -> bool`
  - `verify_quiesced(*, run=subprocess.run) -> QuiesceReport`

**Design note (shutdown ordering):** `maestro-run-once.service` declares
`ExecStartPre=+/bin/systemctl stop maestro-telegram-operator.service` and
`ExecStopPost=+/bin/systemctl start maestro-telegram-operator.service`. Stopping run-once
therefore *starts* the operator. `QUIESCE_STOP_ORDER` puts run-once before the operator so
the operator is stopped after the unit that can resurrect it, and `verify_quiesced` re-checks
every unit afterwards rather than trusting the stop calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quiesce_units.py
from pathlib import Path

from maestro.ops import quiesce

UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def test_every_shipped_unit_is_classified():
    """A new unit file must fail this test rather than escape the barrier.

    The barrier is only as complete as its list, and the list cannot be kept
    correct by intention alone.
    """
    shipped = {path.name for path in UNIT_DIR.iterdir() if path.is_file()}
    classified = set(quiesce.WRITER_UNITS) | set(quiesce.ACTIVATOR_UNITS) | set(
        quiesce.NON_WRITER_UNITS
    )
    assert shipped - classified == set()


def test_no_unit_is_classified_twice():
    names = [*quiesce.WRITER_UNITS, *quiesce.ACTIVATOR_UNITS, *quiesce.NON_WRITER_UNITS]
    assert len(names) == len(set(names))


def test_the_dashboard_is_treated_as_a_writer():
    """It is named "read-only" and is not: POST /api/dashboard/refresh and
    POST /api/dashboard/virtuoso/{id}/generate-signal both write state."""
    assert "maestro-dashboard.service" in quiesce.WRITER_UNITS


def test_every_timer_and_path_unit_is_an_activator():
    shipped = {path.name for path in UNIT_DIR.iterdir() if path.is_file()}
    triggers = {name for name in shipped if name.endswith((".timer", ".path"))}
    assert triggers <= set(quiesce.ACTIVATOR_UNITS)


def test_the_dashboard_restart_helpers_are_activators():
    for unit in (
        "maestro-dashboard-health.service",
        "maestro-dashboard-reload.service",
        "maestro-dashboard-src-watch.service",
    ):
        assert unit in quiesce.ACTIVATOR_UNITS


def test_run_once_is_stopped_before_the_operator_it_restarts():
    """run-once's ExecStopPost starts the telegram operator. Stopping the
    operator first would have it brought back up by the next stop."""
    order = list(quiesce.QUIESCE_STOP_ORDER)
    assert order.index("maestro-run-once.service") < order.index(
        "maestro-telegram-operator.service"
    )


def test_the_stop_order_covers_every_writer_and_activator():
    assert set(quiesce.QUIESCE_STOP_ORDER) == set(quiesce.WRITER_UNITS) | set(
        quiesce.ACTIVATOR_UNITS
    )


def test_a_fully_stopped_system_reports_quiesced():
    report = quiesce.verify_quiesced(run=_fake_systemctl(active=set(), jobs=[]))
    assert report.quiesced is True
    assert report.active_units == ()


def test_one_live_writer_is_named():
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active={"maestro-dashboard.service"}, jobs=[])
    )
    assert report.quiesced is False
    assert report.active_units == ("maestro-dashboard.service",)


def test_a_queued_start_job_breaks_the_barrier_even_with_nothing_active():
    """systemctl is-active says "inactive" for a unit whose start job is still
    queued. It will be running a moment later, inside the migration."""
    report = quiesce.verify_quiesced(
        run=_fake_systemctl(active=set(), jobs=["maestro-heartbeat.service"])
    )
    assert report.quiesced is False
    assert report.queued_jobs == ("maestro-heartbeat.service",)
```

`_fake_systemctl(active, jobs)` returns a callable matching `subprocess.run`'s signature
that answers `is-active` with returncode 0/3 and `list-jobs --no-legend` with one line per
queued unit.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_quiesce_units.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.ops.quiesce'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/maestro/ops/quiesce.py
"""Which systemd units have to be down before the state DB may be migrated.

Stopping the services is not enough on its own. A timer, a .path unit or a
restart helper left enabled brings a writer back between the check and the
operation, and `systemctl is-active` reports "inactive" for a unit whose start
job is merely queued. So the barrier is: every writer inactive, every activator
inactive, no queued jobs -- and, on top of all three, the StateStore writer lock
actually held (see state/upgrade_backfill.py), because none of this constrains a
cooperating `maestro` CLI invocation an operator runs by hand.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Units whose process writes the Maestro state database.
WRITER_UNITS: tuple[str, ...] = (
    "maestro-telegram-operator.service",
    # Named "read-only" and is not: POST /api/dashboard/refresh refreshes
    # broker/FX state and POST /api/dashboard/virtuoso/{id}/generate-signal
    # runs a signal. Both write system_events.
    "maestro-dashboard.service",
    "maestro-heartbeat.service",
    "maestro-fx-refresh.service",
    "maestro-resume-order-tracking.service",
    "maestro-run-once.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
)

#: Units that start or restart a writer without being one themselves.
ACTIVATOR_UNITS: tuple[str, ...] = (
    "maestro-book-performance.timer",
    "maestro-dashboard-health.timer",
    "maestro-dashboard-health.service",
    "maestro-dashboard-reload.service",
    "maestro-dashboard-src-watch.service",
    "maestro-dashboard.path",
    "maestro-fx-refresh.timer",
    "maestro-heartbeat.timer",
    "maestro-resume-order-tracking.timer",
    "maestro-run-once.timer",
    "maestro-symphony-readonly.timer",
    "maestro-symphony-readonly-kr.timer",
    "maestro-symphony-readonly-us.timer",
    "maestro-symphony-signal.timer",
    "maestro-symphony-signal-kr.timer",
    "maestro-symphony-signal-us.timer",
)

#: Reviewed and cleared: writes no Maestro state.
NON_WRITER_UNITS: tuple[str, ...] = (
    # Runs a backtest script in the virtuoso checkout and writes a JSON file.
    "maestro-book-performance.service",
)

#: The order the runbook stops units in. Activators first, so nothing restarts
#: what is about to be stopped; run-once before the telegram operator, because
#: run-once's ExecStopPost *starts* the operator.
QUIESCE_STOP_ORDER: tuple[str, ...] = (
    *(unit for unit in ACTIVATOR_UNITS if unit.endswith((".timer", ".path"))),
    "maestro-dashboard-src-watch.service",
    "maestro-dashboard-health.service",
    "maestro-dashboard-reload.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-fx-refresh.service",
    "maestro-heartbeat.service",
    "maestro-resume-order-tracking.service",
    "maestro-run-once.service",
    "maestro-telegram-operator.service",
    "maestro-dashboard.service",
)

BARRIER_UNITS: tuple[str, ...] = (*WRITER_UNITS, *ACTIVATOR_UNITS)

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class UnitState:
    unit: str
    active: bool
    enabled: str


@dataclass(frozen=True)
class QuiesceReport:
    active_units: tuple[str, ...]
    queued_jobs: tuple[str, ...]

    @property
    def quiesced(self) -> bool:
        return not self.active_units and not self.queued_jobs


def _run(runner: Runner, args: Sequence[str]) -> subprocess.CompletedProcess:
    return runner(  # noqa: S603 - fixed argv, no shell
        list(args), check=False, capture_output=True, text=True
    )


def _is_active(runner: Runner, unit: str) -> bool:
    return _run(runner, ["systemctl", "is-active", unit]).returncode == 0


def _queued_jobs(runner: Runner) -> tuple[str, ...]:
    result = _run(runner, ["systemctl", "list-jobs", "--no-legend"])
    jobs = []
    for line in (result.stdout or "").splitlines():
        for field in line.split():
            if field in BARRIER_UNITS:
                jobs.append(field)
    return tuple(sorted(set(jobs)))


def capture_unit_states(
    units: Sequence[str] = BARRIER_UNITS, *, run: Runner = subprocess.run
) -> list[UnitState]:
    """The states to restore afterwards -- exactly, not `enable --now` on everything.

    Some units are intentionally disabled or masked in this deployment; turning
    them all on after a migration would start writers the operator had
    deliberately turned off.
    """
    states = []
    for unit in units:
        enabled = _run(run, ["systemctl", "is-enabled", unit]).stdout.strip() or "unknown"
        states.append(UnitState(unit=unit, active=_is_active(run, unit), enabled=enabled))
    return states


def verify_quiesced(*, run: Runner = subprocess.run) -> QuiesceReport:
    active = tuple(unit for unit in BARRIER_UNITS if _is_active(run, unit))
    return QuiesceReport(active_units=active, queued_jobs=_queued_jobs(run))


__all__ = [
    "ACTIVATOR_UNITS",
    "BARRIER_UNITS",
    "NON_WRITER_UNITS",
    "QUIESCE_STOP_ORDER",
    "QuiesceReport",
    "UnitState",
    "WRITER_UNITS",
    "capture_unit_states",
    "verify_quiesced",
]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_quiesce_units.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/ops/quiesce.py tests/test_quiesce_units.py
git commit -m "feat(3a-5): enumerate every writer and activator the quiesce barrier must cover"
```

---

### Task 6: Whole-operation writer lock + Task 7: conservative funding head backfill

**Files:**
- Create: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_heads.py`

**Interfaces:**
- Produces:
  - `QUARANTINE_EVENT = "funding_workflow_migration_quarantine"`
  - `@dataclass(frozen=True) class Quarantine: subsystem: str; identifier: str; reason: str; blocking: bool; detail: dict[str, Any]`
  - `@dataclass class BackfillReport: legacy_requests_inspected: int; heads_created: int; heads_already_coherent: int; terminal_skipped: int; superseded_by_newer: int; quarantines: list[Quarantine]` with `.blocking -> list[Quarantine]`
  - `backfill_funding_heads(store, run_id, *, cutoff: int) -> BackfillReport`
  - `write_quarantine(store, run_id, q: Quarantine) -> bool`
  - `list_quarantines(store) -> list[Quarantine]`
  - `quarantine_key(subsystem: str, identifier: str) -> str`

**Case table** (each pre-cutoff `contribution_*_request` row):

| Case | Condition | Action |
|------|-----------|--------|
| A | a legacy `..._ack`/`..._decision` names this request | terminal; skip, `terminal_skipped += 1` |
| B | `funding_workflow_completed`/`_superseded` names it | terminal; skip |
| — | `payload["status"] != "pending"` | not a candidate; skip |
| C | exactly one pending request in this workflow, no head | write v1 head, `heads_created += 1` |
| D | exactly one pending request, head already names it | `heads_already_coherent += 1` |
| E | head names a request recorded **after** the cutoff | post-3a lineage owns the workflow; skip, `superseded_by_newer += 1` |
| F | head names some other request, not provably later | quarantine `head_ownership_conflict`, **blocking** |
| G | ≥2 pending requests in one workflow | quarantine `ambiguous_pending_requests`, **blocking** |
| H | workflow identity cannot be derived (no `month_key`) | quarantine `malformed_workflow_identity`, **blocking** |

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upgrade_backfill_heads.py
import pytest

from maestro.state import upgrade_backfill as ub
from maestro.state.funding_workflow import head_key


def test_one_unambiguous_pending_request_gets_a_v1_head(store):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.heads_created == 1
    head = store.load_funding_workflow_head(_workflow_id("2026-08"))
    assert head["request_id"] == "req-1"
    assert head["version"] == 1
    assert head["reason"] == "legacy_backfill_v1"


def test_a_legacy_acked_request_is_not_resurrected(store):
    """The ack is history, not current truth -- but it still proves this
    request's month is over. Creating a live head for it would put a finished
    request back in front of the operator with a working Confirm button."""
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _legacy_ack(store, "req-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.heads_created == 0
    assert report.terminal_skipped == 1
    assert store.load_funding_workflow_head(_workflow_id("2026-08")) is None


def test_a_legacy_budget_decision_is_terminal_too(store):
    _legacy_pending_budget_request(store, "req-b", month_key="2026-08")
    _legacy_budget_decision(store, "req-b")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.heads_created == 0
    assert report.terminal_skipped == 1


def test_two_pending_requests_in_one_workflow_are_quarantined_not_guessed(store):
    """Picking a winner here assigns this month's investment to one of two
    requests on no evidence. The wrong choice moves money."""
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _legacy_pending_request(store, "req-2", month_key="2026-08")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.heads_created == 0
    assert [q.reason for q in report.blocking] == ["ambiguous_pending_requests"]
    assert store.load_funding_workflow_head(_workflow_id("2026-08")) is None


def test_a_coherent_existing_head_is_idempotent(store):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    cutoff = _max_event_id(store)
    with store.writer_lock("test"):
        ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)
        report = ub.backfill_funding_heads(store, "run-2", cutoff=cutoff)

    assert report.heads_created == 0
    assert report.heads_already_coherent == 1
    heads = store.list_system_events_by_type("funding_workflow_head", limit=None)
    assert len(heads) == 1


def test_a_post_cutoff_successor_head_is_preserved_not_overwritten(store):
    """3a already published a newer request for this workflow. That lineage is
    coherent and current; the pre-cutoff request is inert history."""
    _legacy_pending_request(store, "req-old", month_key="2026-08")
    cutoff = _max_event_id(store)
    _publish_current_generation_request(store, "req-new", month_key="2026-08")

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.heads_created == 0
    assert report.superseded_by_newer == 1
    assert store.load_funding_workflow_head(_workflow_id("2026-08"))["request_id"] == "req-new"


def test_a_head_pointing_somewhere_unprovable_blocks_the_migration(store):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _raw_head(store, _workflow_id("2026-08"), request_id="req-ghost", version=1)
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert [q.reason for q in report.blocking] == ["head_ownership_conflict"]


def test_a_request_with_no_month_key_is_quarantined(store):
    _raw_request(store, "req-bad", payload={"request_id": "req-bad", "status": "pending"})
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert [q.reason for q in report.blocking] == ["malformed_workflow_identity"]


def test_a_post_cutoff_request_is_not_a_backfill_candidate(store):
    cutoff = _max_event_id(store)
    _legacy_pending_request(store, "req-1", month_key="2026-08")

    with store.writer_lock("test"):
        report = ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)

    assert report.legacy_requests_inspected == 0
    assert report.heads_created == 0


def test_the_backfill_refuses_to_run_without_the_writer_lock(store):
    """Observing "no head exists" and writing one are only a single decision if
    nothing can write between them."""
    with pytest.raises(RuntimeError, match="writer lock"):
        ub.backfill_funding_heads(store, "run-1", cutoff=0)


def test_quarantine_rows_are_deterministic_and_idempotent(store):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _legacy_pending_request(store, "req-2", month_key="2026-08")
    cutoff = _max_event_id(store)
    with store.writer_lock("test"):
        ub.backfill_funding_heads(store, "run-1", cutoff=cutoff)
        ub.backfill_funding_heads(store, "run-2", cutoff=cutoff)

    rows = store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)
    assert len(rows) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_heads.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.upgrade_backfill'`

- [ ] **Step 3: Write minimal implementation**

Create `src/maestro/state/upgrade_backfill.py` with the quarantine primitives and
`backfill_funding_heads` implementing the case table above. Key rules the implementation
must honour:

- Refuse unless `store.holds_writer_lock()`.
- Candidates: `contribution_funding_request` / `contribution_budget_request` rows with
  `int(row["id"]) <= cutoff`.
- Head write:

```python
        store.save_system_events_atomic(
            str(row.get("run_id") or request_id),
            [
                {
                    "event_type": "funding_workflow_head",
                    "payload": {
                        "duplicate_key": head_key(workflow_id, 1),
                        "workflow_id": workflow_id,
                        "version": 1,
                        "request_id": request_id,
                        "phase": phase,
                        "status": "pending",
                        "scope": [
                            payload.get("contribution_group_id"),
                            payload.get("account_id"),
                            payload.get("execution_sleeve"),
                            payload.get("currency"),
                        ],
                        # Deterministic and content-identical on every rerun:
                        # save_system_events_atomic verifies a replay by
                        # comparing payloads, so a timestamp here would make a
                        # crash-resume die instead of adopting its own work.
                        "reason": "legacy_backfill_v1",
                    },
                }
            ],
            forbid_duplicate_keys=(head_key(workflow_id, 1),),
        )
```

- `quarantine_key(subsystem, identifier) -> f"migration-quarantine:{subsystem}:{identifier}"`.
- `write_quarantine` uses `save_system_events_atomic` with
  `forbid_duplicate_keys=(key,)` and a payload of only `subsystem`, `identifier`,
  `reason`, `blocking`, `detail` — no clock.
- `Quarantine.detail` values must be JSON-serializable and sorted (e.g. sorted request-id
  lists) so a rerun reproduces byte-identical content.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_heads.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/upgrade_backfill.py tests/test_upgrade_backfill_heads.py
git commit -m "feat(3a-5): backfill v1 heads only where legacy history is unambiguous"
```

---

### Task 8: Legacy approval classification — quarantine, never synthesize

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_quarantine.py`

**Interfaces:**
- Produces:
  - `@dataclass class ApprovalClassificationReport: acks_inspected: int; proven_complete: int; quarantines: list[Quarantine]`
  - `classify_legacy_approvals(store, run_id, *, cutoff: int) -> ApprovalClassificationReport`
  - `completed_legacy_approval_ids(store) -> set[str]` — moved out of
    `handlers._completed_legacy_approval_ids` so migration and runtime share one
    conservative rule (handlers delegates to it).

**Rules** (§10). Candidates: `telegram_approval_ack` rows with `id <= cutoff` and a
non-`int` `schema_version`.

| Evidence | Verdict |
|----------|---------|
| `signal_approval_completed` with a matching `approval_id` | proven complete |
| `signal_approval_completed` with no `approval_id`, and the run has exactly one approval group | proven complete |
| an `approvals` row exists (`store.approval_exists`) and no proof of completion | quarantine `execution_may_have_been_entered` |
| anything else | quarantine `completion_unprovable` |

Both quarantines are **non-blocking**: the current runtime already refuses to
auto-execute a schema-less ack (`_resume_unresolved_approvals` requires
`isinstance(schema_version, int)`; `ops/batch_execution` raises `legacy_ack`). The
quarantine adds operator ownership, not a new gate.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upgrade_backfill_quarantine.py
from maestro.state import upgrade_backfill as ub


def test_a_legacy_ack_is_never_given_a_synthetic_resolution(store):
    """The old plan proposed: no approvals row + no completion => "canceled".

    That reads a broker's behaviour out of a gap in local persistence. The
    order may have gone out before the process died. Nothing is synthesized.
    """
    _schema_less_ack(store, "ap-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    ) == []


def test_an_exact_completion_match_is_proven_complete(store):
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _approval_completed(store, approval_id="ap-1", signal_run_id="sig-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert report.proven_complete == 1
    assert report.quarantines == []


def test_a_single_group_run_completion_without_an_approval_id_counts(store):
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _approval_completed(store, approval_id=None, signal_run_id="sig-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert report.proven_complete == 1


def test_a_multi_group_run_completion_without_an_approval_id_does_not_count(store):
    """One group finishing says nothing about the other's orders."""
    _schema_less_ack(store, "ap-1")
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")
    _pending_envelope(store, "ap-2", signal_run_id="sig-1")
    _approval_completed(store, approval_id=None, signal_run_id="sig-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert report.proven_complete == 0
    assert {q.reason for q in report.quarantines} == {"completion_unprovable"}


def test_an_approvals_row_without_a_completion_is_quarantined_as_maybe_entered(store):
    """The approval was persisted, so execution may have been entered. This is
    strictly worse than "unknown" and is labelled as such."""
    _schema_less_ack(store, "ap-1")
    store.save_approval("run-x", "ap-1", {"decision": {"status": "approved"}})
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert [q.reason for q in report.quarantines] == ["execution_may_have_been_entered"]
    assert report.quarantines[0].blocking is False


def test_a_versioned_ack_is_not_a_legacy_candidate(store):
    _versioned_ack(store, "ap-1")
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)

    assert report.acks_inspected == 0


def test_classification_is_idempotent(store):
    _schema_less_ack(store, "ap-1")
    cutoff = _max_event_id(store)
    with store.writer_lock("test"):
        ub.classify_legacy_approvals(store, "run-1", cutoff=cutoff)
        ub.classify_legacy_approvals(store, "run-2", cutoff=cutoff)

    rows = store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)
    assert len(rows) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_quarantine.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'classify_legacy_approvals'`

- [ ] **Step 3: Write minimal implementation**

Add `completed_legacy_approval_ids` (lifted verbatim from
`handlers._completed_legacy_approval_ids`, including its docstring) and
`classify_legacy_approvals` to `upgrade_backfill.py`. Then make
`handlers._completed_legacy_approval_ids` a one-line delegation so the two can never
diverge:

```python
    def _completed_legacy_approval_ids(self) -> set[str]:
        """See upgrade_backfill.completed_legacy_approval_ids -- one conservative
        rule, shared by the runtime notice and the migration classification, so
        a change to one can never leave the other judging by an older rule."""
        return completed_legacy_approval_ids(self.store)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_quarantine.py tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/upgrade_backfill.py src/maestro/integrations/telegram/handlers.py tests/
git commit -m "feat(3a-5): classify legacy approvals into evidence or quarantine, never into cancellation"
```

---

### Task 9: The manifest is the dispatch resume fence

**Files:**
- Modify: `src/maestro/state/store.py` (`list_incomplete_signal_dispatches` limit),
  `src/maestro/state/upgrade_backfill.py` (`classify_legacy_dispatches`),
  `src/maestro/integrations/telegram/handlers.py:1815-1847` (`_resume_incomplete_dispatches`)
- Test: `tests/test_upgrade_backfill_quarantine.py`, `tests/test_telegram_dispatch_resume.py`

**Interfaces:**
- Produces:
  - `store.list_incomplete_signal_dispatches(limit: int | None = 50)`
  - `dispatch_manifest_key(signal_run_id) -> str` (`f"dispatch-manifest:{signal_run_id}"`)
  - `has_dispatch_manifest(store, signal_run_id) -> bool`
  - `classify_legacy_dispatches(store, run_id, *, cutoff: int) -> DispatchClassificationReport`
    with `dispatches_inspected`, `resumable`, `quarantines`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_dispatch_resume.py (append)
def test_a_consumed_run_with_no_manifest_is_not_auto_resumed(operator_bot, store):
    """Consumed + unsettled + no manifest is pre-manifest history. Its dispatch
    intent was never recorded, so resuming would recompute it from today's
    capacity, buying power and grouping -- and dispatch a set of orders the run
    never actually intended. Absence of a manifest is not proof that nothing
    went out either, so this goes to a person."""
    _consumed_but_unsettled(store, "sig-legacy", with_manifest=False)
    dispatched: list[str] = []
    operator_bot._run_dispatch = dispatched.append

    operator_bot._resume_incomplete_dispatches()

    assert dispatched == []
    notices = store.list_system_events_by_type("telegram_dispatch_needs_attention", limit=None)
    assert any(row["payload"].get("signal_run_id") == "sig-legacy" for row in notices)


def test_a_consumed_run_with_a_manifest_still_resumes(operator_bot, store):
    _consumed_but_unsettled(store, "sig-current", with_manifest=True)
    dispatched: list[str] = []
    operator_bot._run_dispatch = dispatched.append

    operator_bot._resume_incomplete_dispatches()

    assert dispatched == ["sig-current"]


def test_the_attention_notice_is_sent_once_per_run(operator_bot, store):
    _consumed_but_unsettled(store, "sig-legacy", with_manifest=False)
    operator_bot._run_dispatch = lambda _: None

    operator_bot._resume_incomplete_dispatches()
    operator_bot._resume_incomplete_dispatches()

    notices = [
        row
        for row in store.list_system_events_by_type("telegram_dispatch_needs_attention", limit=None)
        if row["payload"].get("signal_run_id") == "sig-legacy"
    ]
    assert len(notices) == 1
```

```python
# tests/test_upgrade_backfill_quarantine.py (append)
def test_a_manifestless_consumed_dispatch_is_quarantined(store):
    _consumed_but_unsettled(store, "sig-legacy", with_manifest=False)
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_dispatches(store, "run-1", cutoff=cutoff)

    assert [q.reason for q in report.quarantines] == ["legacy_dispatch_no_manifest"]
    assert report.quarantines[0].blocking is False


def test_a_dispatch_with_a_manifest_is_current_generation(store):
    _consumed_but_unsettled(store, "sig-current", with_manifest=True)
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_dispatches(store, "run-1", cutoff=cutoff)

    assert report.resumable == 1
    assert report.quarantines == []


def test_the_dispatch_classification_is_exhaustive_not_windowed(store):
    """A default limit of 50 would silently drop the 51st unfinished run."""
    for index in range(60):
        _consumed_but_unsettled(store, f"sig-{index:03d}", with_manifest=False)
    cutoff = _max_event_id(store)

    with store.writer_lock("test"):
        report = ub.classify_legacy_dispatches(store, "run-1", cutoff=cutoff)

    assert report.dispatches_inspected == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_telegram_dispatch_resume.py tests/test_upgrade_backfill_quarantine.py -q`
Expected: FAIL — the manifest-less run is dispatched, and
`classify_legacy_dispatches` does not exist.

- [ ] **Step 3: Write minimal implementation**

`store.list_incomplete_signal_dispatches`: change the signature to
`limit: int | None = 50` and, when `limit is None`, drop the `LIMIT` clause. Document why
an unbounded read exists (rollback preflight and migration must be exhaustive; the poll
loop stays windowed).

In `_resume_incomplete_dispatches`, before the attempt-budget check:

```python
            if not has_dispatch_manifest(self.store, signal_run_id):
                # Pre-manifest history. The manifest is the only durable record
                # of which groups this dispatch meant to place; without one,
                # re-entering dispatch would rebuild that intent out of today's
                # capacity, buying power, account state and approval grouping,
                # and could place a different set of orders than the run
                # actually decided on. Nor does the missing manifest prove
                # nothing went out. Neither replay nor "assume finished" is
                # safe, so it goes to a person -- once.
                self._notify_dispatch_needs_attention(signal_run_id)
                continue
```

`_notify_dispatch_needs_attention` already deduplicates per `signal_run_id` through
`_notify_operator_chats`' per-chat `duplicate_key`, which is what makes the third test
pass without extra state.

Add `classify_legacy_dispatches` to `upgrade_backfill.py`, iterating
`store.list_incomplete_signal_dispatches(limit=None)`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_telegram_dispatch_resume.py tests/test_upgrade_backfill_quarantine.py tests/test_state_store_incomplete_dispatch.py tests/test_signal_approval_handoff.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/store.py src/maestro/state/upgrade_backfill.py src/maestro/integrations/telegram/handlers.py tests/
git commit -m "fix(3a-5): require a durable manifest before a dispatch may be resumed"
```

---

### Task 10: Migration-sensitive runtime gates

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Test: `tests/test_migration_runtime_gates.py`

**Interfaces:**
- Produces: `TelegramOperatorBot._migration_block_reason() -> str | None` — the reason a
  migration-sensitive path must refuse, or `None`.

**Gated paths:** funding callback, budget callback, `/budget` command, workflow Resume
callback, async approval approve/reject callbacks, `_resume_unresolved_approvals`,
`_resume_incomplete_dispatches`, `_sweep_incomplete_workflows`,
`_converge_workflow_invariants`.

**Not gated:** read-only status/health/portfolio/orders views. Production is quiesced
during the real migration; adding a global StateStore write framework for them would be
scope the safety argument does not need.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_runtime_gates.py
import pytest

from maestro.state import migration_state as ms

GATED_SWEEPS = (
    "_resume_unresolved_approvals",
    "_resume_incomplete_dispatches",
    "_sweep_incomplete_workflows",
    "_converge_workflow_invariants",
)


def test_no_block_on_a_completed_migration(operator_bot, store):
    _complete_migration(store)
    assert operator_bot._migration_block_reason() is None


def test_no_block_before_any_migration(operator_bot):
    assert operator_bot._migration_block_reason() is None


def test_migrating_blocks(operator_bot, store):
    _start_migration(store)
    assert operator_bot._migration_block_reason() == "migrating"


def test_invalid_markers_block(operator_bot, store):
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY})
    assert operator_bot._migration_block_reason() == "invalid:malformed_started_marker"


@pytest.mark.parametrize("sweep", GATED_SWEEPS)
def test_every_recovery_sweep_stands_down_while_migrating(operator_bot, store, sweep, monkeypatch):
    _start_migration(store)
    called = []
    monkeypatch.setattr(operator_bot, "_run_dispatch", lambda *_: called.append("dispatch"))
    monkeypatch.setattr(operator_bot, "_resume_one_approval", lambda *_: called.append("approval"))
    _stalled_workflow(store)
    _consumed_but_unsettled(store, "sig-1", with_manifest=True)

    getattr(operator_bot, sweep)()

    assert called == []


def test_the_convergence_sweep_writes_nothing_while_migrating(operator_bot, store):
    _start_migration(store)
    _orphan_request_after_cutoff(store)
    before = _event_count(store)

    operator_bot._converge_workflow_invariants()

    assert _event_count(store) == before


def test_a_funding_confirm_callback_is_refused_while_migrating(operator_bot, store):
    _start_migration(store)
    _publish_pending_funding_request(store, "req-1")

    handled = operator_bot.process_update(_funding_confirm_callback("req-1"))

    assert handled is True
    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []


def test_a_budget_command_is_refused_while_migrating(operator_bot, store):
    _start_migration(store)
    _publish_pending_budget_request(store, "req-b")

    operator_bot._process_budget_command("/budget req-b 100000", 1, 1, "op")

    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []


def test_an_approval_callback_is_refused_while_migrating(operator_bot, store):
    _start_migration(store)
    _pending_envelope(store, "ap-1", signal_run_id="sig-1")

    operator_bot.process_update(_approval_callback("ap-1", "approve"))

    assert store.list_system_events_by_type("telegram_approval_ack", limit=None) == []


def test_a_workflow_resume_callback_is_refused_while_migrating(operator_bot, store):
    _start_migration(store)
    _stalled_workflow(store)
    before = len(store.list_system_events_by_type("funding_workflow_claim", limit=None))

    operator_bot.process_update(_wfresume_callback("funding", "req-1"))

    assert len(store.list_system_events_by_type("funding_workflow_claim", limit=None)) == before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_migration_runtime_gates.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_migration_block_reason'`

- [ ] **Step 3: Write minimal implementation**

```python
    def _migration_block_reason(self) -> str | None:
        """Why a migration-sensitive path must stand down, or None.

        MIGRATING means some heads and quarantines exist and others do not yet,
        so any decision made from this half-classified history can be wrong in
        the one direction that costs money. INVALID means the markers
        contradict each other and nothing can be trusted to say which
        generation a row belongs to. Both fail closed.

        Read-only views are deliberately not gated: production is quiesced for
        the real migration, and a global write framework for `status` would be
        scope this argument does not need.
        """
        state = load_migration_state(self.store)
        if state.phase is MigrationPhase.MIGRATING:
            return "migrating"
        if state.phase is MigrationPhase.INVALID:
            return f"invalid:{state.reason}"
        return None
```

Each sweep begins with `if self._migration_block_reason() is not None: return`. Each
operator-facing callback/command answers with `ui_catalog.MIGRATION_IN_PROGRESS` and
records the interaction with status `"migration_blocked"`. Add
`MIGRATION_IN_PROGRESS = "A state migration is in progress. This action is paused until it finishes."`
to `src/maestro/integrations/telegram/ui/catalog.py`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_migration_runtime_gates.py tests/test_telegram_operator_ui.py tests/test_funding_workflow_resume.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/integrations/telegram/ tests/test_migration_runtime_gates.py
git commit -m "feat(3a-5): stand every financial recovery path down while a migration owns the database"
```

---

### Task 11: `maestro upgrade-backfill` orchestration and CLI

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`, `src/maestro/cli.py`
- Test: `tests/test_upgrade_backfill_cli.py`

**Interfaces:**
- Produces:
  - `@dataclass class UpgradeResult: state: MigrationState; backfill: BackfillReport | None; approvals: ApprovalClassificationReport | None; dispatches: DispatchClassificationReport | None; completed: bool; aborted_reason: str | None; reupgrade_evidence: list[dict[str, Any]]`
  - `run_upgrade_backfill(store, run_id) -> UpgradeResult` — acquires
    `store.writer_lock("upgrade_backfill")` and holds it for the entire operation.
  - CLI `maestro upgrade-backfill [--config PATH] [--require-quiesce/--no-require-quiesce]`
    (default: require quiesce). Exit 0 on completed/no-op, 1 on abort or blocking
    quarantine.

**Order inside the lock** — `migration_completed` is last, unconditionally:

1. `load_migration_state`; INVALID → abort.
2. COMPLETED → run `detect_reupgrade_after_rollback` (Task 14); evidence → abort;
   otherwise report a no-op and return **without writing anything**.
3. `start_migration` (reuses an existing cutoff).
4. `backfill_funding_heads`
5. `classify_legacy_approvals`
6. `classify_legacy_dispatches`
7. If any blocking quarantine exists → abort **without** completing. The database stays
   MIGRATING, so the runtime gates stay closed and the operator must not restart services.
8. `complete_migration` — the final write.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upgrade_backfill_cli.py
from typer.testing import CliRunner

from maestro import cli
from maestro.state import migration_state as ms
from maestro.state import upgrade_backfill as ub


def test_a_clean_upgrade_completes_and_reports_each_category(store, config_path):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 0
    assert "heads_created=1" in result.stdout
    assert "state=completed" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED


def test_migration_completed_is_the_last_row_written(store, config_path):
    """Anything after it is a write into a database that already declared
    itself migrated -- and would be invisible to the next run's classification."""
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _schema_less_ack(store, "ap-1")
    CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                 "--no-require-quiesce"])

    assert _last_event_type(store) == ms.COMPLETED_EVENT


def test_a_blocking_quarantine_aborts_before_completion(store, config_path):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    _legacy_pending_request(store, "req-2", month_key="2026-08")

    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 1
    assert "ambiguous_pending_requests" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_a_completed_migration_reruns_as_a_no_op(store, config_path):
    CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                 "--no-require-quiesce"])
    before = _event_count(store)
    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 0
    assert "state=completed" in result.stdout
    assert _event_count(store) == before


def test_an_invalid_marker_aborts_without_writing(store, config_path):
    store.save_system_event("r", ms.COMPLETED_EVENT, {"cutoff": 3, "duplicate_key": ms.COMPLETED_KEY})
    before = _event_count(store)

    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 1
    assert "completed_without_started" in result.stdout
    assert _event_count(store) == before


def test_a_live_writer_unit_refuses_the_run(store, config_path, monkeypatch):
    monkeypatch.setattr(
        cli.quiesce,
        "verify_quiesced",
        lambda **_: cli.quiesce.QuiesceReport(
            active_units=("maestro-dashboard.service",), queued_jobs=()
        ),
    )
    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "maestro-dashboard.service" in result.stdout


def test_the_writer_lock_is_held_for_the_whole_operation(store, config_path, monkeypatch):
    """Not per insert. The cutoff only means anything if no cooperating writer
    can append between observing it and completing."""
    held = []
    original = ub.backfill_funding_heads

    def spy(store_, run_id, *, cutoff):
        held.append(store_.holds_writer_lock())
        return original(store_, run_id, cutoff=cutoff)

    monkeypatch.setattr(ub, "backfill_funding_heads", spy)
    CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                 "--no-require-quiesce"])

    assert held == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_cli.py -q`
Expected: FAIL — `No such command 'upgrade-backfill'`

- [ ] **Step 3: Write minimal implementation**

Add `run_upgrade_backfill` to `upgrade_backfill.py` implementing the eight steps, and to
`cli.py`:

```python
@app.command("upgrade-backfill")
def upgrade_backfill(
    config: Path | None = CONFIG_OPTION,
    require_quiesce: bool = typer.Option(
        True,
        "--require-quiesce/--no-require-quiesce",
        help="Refuse to run unless every writer unit and activator is stopped.",
    ),
) -> None:
    """3a 업그레이드 backfill. quiesce 장벽 아래에서만 실행한다.

    브로커 주문, 승인 재실행, 시그널 생성, 현금흐름 기록을 절대 하지 않는다.
    쓰는 것은 마이그레이션 소유권 마커, 결정적 v1 head, 격리 레코드뿐이다.
    """
    if require_quiesce:
        report = quiesce.verify_quiesced()
        if not report.quiesced:
            for unit in report.active_units:
                typer.echo(f"upgrade_backfill status=fail reason=writer_active unit={unit}")
            for unit in report.queued_jobs:
                typer.echo(f"upgrade_backfill status=fail reason=queued_job unit={unit}")
            raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    result = run_upgrade_backfill(store, new_run_id())
    _echo_upgrade_result(result)
    if result.aborted_reason is not None:
        raise typer.Exit(1)
```

`_echo_upgrade_result` prints, one `key=value` line each: `state`, `cutoff`,
`legacy_rows_inspected`, `heads_created`, `heads_already_coherent`, `terminal_skipped`,
`superseded_by_newer`, `approval_acks_inspected`, `approvals_proven_complete`,
`dispatches_inspected`, `dispatches_resumable`, one line per quarantine
(`quarantine subsystem=… identifier=… reason=… blocking=…`), one line per re-upgrade
evidence row, and finally `status=completed` or `status=aborted reason=…`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maestro/state/upgrade_backfill.py src/maestro/cli.py tests/test_upgrade_backfill_cli.py
git commit -m "feat(3a-5): add maestro upgrade-backfill, completed last and under one held lock"
```

---

### Task 12: Crash injection and idempotent resume

**Files:**
- Test: `tests/test_upgrade_backfill_cli.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_a_crash_before_the_start_marker_leaves_no_ownership(store, config_path, monkeypatch):
    monkeypatch.setattr(ms, "start_migration", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")

    assert ms.load_migration_state(store).phase is ms.MigrationPhase.NOT_STARTED


def test_a_crash_after_the_start_marker_reuses_the_exact_cutoff(store, config_path, monkeypatch):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    monkeypatch.setattr(ub, "backfill_funding_heads", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    first = ms.load_migration_state(store)
    _legacy_pending_request(store, "req-late", month_key="2026-09")  # would move MAX(id)

    monkeypatch.undo()
    ub.run_upgrade_backfill(store, "run-2")

    assert ms.load_migration_state(store).cutoff == first.cutoff


def test_a_crash_midway_through_head_backfill_adopts_what_landed(store, monkeypatch):
    _legacy_pending_request(store, "req-a", month_key="2026-08")
    _legacy_pending_request(store, "req-b", month_key="2026-09")
    monkeypatch.setattr(ub, "classify_legacy_approvals", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    landed = _head_request_ids(store)

    monkeypatch.undo()
    result = ub.run_upgrade_backfill(store, "run-2")

    assert _head_request_ids(store) == landed
    assert result.backfill.heads_created == 0
    assert result.backfill.heads_already_coherent == 2


def test_a_crash_during_quarantine_classification_resumes_deterministically(store, monkeypatch):
    _schema_less_ack(store, "ap-1")
    _schema_less_ack(store, "ap-2")
    monkeypatch.setattr(ub, "classify_legacy_dispatches", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    first = _quarantine_payloads(store)

    monkeypatch.undo()
    ub.run_upgrade_backfill(store, "run-2")

    assert _quarantine_payloads(store)[: len(first)] == first


def test_the_runtime_stays_gated_until_completion_lands(store, operator_bot, monkeypatch):
    monkeypatch.setattr(ms, "complete_migration", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")

    assert operator_bot._migration_block_reason() == "migrating"


def test_a_completed_migration_leaves_no_pending_migration_writes(store):
    _legacy_pending_request(store, "req-1", month_key="2026-08")
    ub.run_upgrade_backfill(store, "run-1")
    after = _event_count(store)

    ub.run_upgrade_backfill(store, "run-2")

    assert _event_count(store) == after
```

`_boom(*args, **kwargs)` raises `RuntimeError("injected crash")`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_cli.py -k crash -q`
Expected: FAIL until `run_upgrade_backfill` calls the module-level names through the
module (so `monkeypatch.setattr` reaches them) and adopts already-written work.

- [ ] **Step 3: Fix the implementation as the tests require**

`run_upgrade_backfill` must reference `backfill_funding_heads` etc. through the module
namespace, and each classifier must treat an existing deterministic row as its own prior
work rather than a conflict.

- [ ] **Step 4: Run tests / Step 5: Commit**

```bash
git add tests/test_upgrade_backfill_cli.py src/maestro/state/upgrade_backfill.py
git commit -m "test(3a-5): prove every migration crash window resumes on the same cutoff"
```

---

### Task 13: Read-only rollback preflight, R0–R4

**Files:**
- Create: `src/maestro/state/rollback_preflight.py`
- Modify: `src/maestro/cli.py`
- Test: `tests/test_rollback_preflight.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class InvariantFailure: invariant: str; identifier: str; detail: str; event_ids: tuple[int, ...]`
  - `@dataclass(frozen=True) class RollbackPreflightResult: failures: tuple[InvariantFailure, ...]` with `.safe -> bool`
  - `run_rollback_preflight(store) -> RollbackPreflightResult`
- CLI: `maestro rollback-preflight` (and the retained alias
  `maestro approval-rollback-preflight`), `--require-quiesce` now checking the full
  barrier.

**Invariants:**
- **R0 migration_state** — MIGRATING or INVALID fails. NOT_STARTED is allowed only if
  R1–R4 also pass (they are what detect current-generation state the old binary cannot
  read).
- **R1 workflow_claim_unresolved** — every `funding_workflow_claim`, read exhaustively
  from the raw events (**not** `list_incomplete_workflows`, which intentionally filters
  out non-live history an operator card should not show — rollback safety needs all of it),
  must have a `funding_workflow_completed` for the same `(request_id, phase)`.
- **R2 dispatch_unsettled** — every `signal_package_consumed` run must satisfy
  `store.signal_dispatch_settled()`. Implemented as
  `store.list_incomplete_signal_dispatches(limit=None)` so the authoritative settled SQL
  is reused rather than re-derived.
- **R3 approval_unresolved** — every `telegram_approval_ack` with an `int`
  `schema_version` must have a `telegram_approval_resolution_completed`.
- **R4 missing_legacy_projection** — every `funding_workflow_completed` must have its
  compatibility projection (`duplicate_key` = `funding-ack:<request_id>` or
  `budget-decision:<request_id>`). **Never repaired.**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rollback_preflight.py
from maestro.state import migration_state as ms
from maestro.state.rollback_preflight import run_rollback_preflight


def test_a_clean_current_generation_database_is_safe(store):
    _publish_pending_funding_request(store, "req-1")
    _claim_and_complete(store, "req-1", phase="funding")
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


def test_r0_refuses_a_half_finished_migration(store):
    _start_migration(store)
    failures = run_rollback_preflight(store).failures
    assert [f.invariant for f in failures] == ["R0_migration_state"]


def test_r0_refuses_contradictory_markers(store):
    store.save_system_event("r", ms.COMPLETED_EVENT, {"cutoff": 1, "duplicate_key": ms.COMPLETED_KEY})
    assert "completed_without_started" in run_rollback_preflight(store).failures[0].detail


def test_r1_names_every_unresolved_claim_including_superseded_history(store):
    """list_incomplete_workflows drops a claim whose head has moved on, because
    an operator cannot act on it. The old binary can still trip over it."""
    _publish_pending_funding_request(store, "req-1")
    _claim_only(store, "req-1", phase="funding")
    _publish_pending_funding_request(store, "req-2")  # head moves off req-1
    _complete_migration(store)

    failures = run_rollback_preflight(store).failures
    assert [(f.invariant, f.identifier) for f in failures] == [
        ("R1_workflow_claim_unresolved", "req-1:funding")
    ]


def test_r2_uses_the_authoritative_settled_definition(store):
    _consumed_but_unsettled(store, "sig-1", with_manifest=True)
    _complete_migration(store)

    failures = run_rollback_preflight(store).failures
    assert [(f.invariant, f.identifier) for f in failures] == [
        ("R2_dispatch_unsettled", "sig-1")
    ]


def test_r2_is_not_windowed_at_fifty(store):
    for index in range(60):
        _consumed_but_unsettled(store, f"sig-{index:03d}", with_manifest=True)
    _complete_migration(store)

    failures = [f for f in run_rollback_preflight(store).failures
                if f.invariant == "R2_dispatch_unsettled"]
    assert len(failures) == 60


def test_r3_flags_a_versioned_ack_with_no_resolution(store):
    _versioned_ack(store, "ap-1")
    _complete_migration(store)

    failures = run_rollback_preflight(store).failures
    assert [(f.invariant, f.identifier) for f in failures] == [
        ("R3_approval_unresolved", "ap-1")
    ]


def test_r3_ignores_a_schema_less_ack(store):
    """The old binary reads a schema-less ack as terminal, exactly as this one
    does. It is not an incompatibility."""
    _schema_less_ack(store, "ap-1")
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


def test_r4_fails_on_a_missing_projection_and_does_not_repair_it(store):
    """complete_workflow writes both legs atomically, so a missing projection
    means corruption, a manual mutation, or an intermediate build. Writing the
    ack here would erase the evidence of whichever it was and hand a rollback a
    story that was never true."""
    _publish_pending_funding_request(store, "req-1")
    _claim_and_complete(store, "req-1", phase="funding")
    _delete_events(store, "contribution_funding_request_ack")
    _complete_migration(store)

    result = run_rollback_preflight(store)
    assert [(f.invariant, f.identifier) for f in result.failures] == [
        ("R4_missing_legacy_projection", "req-1:funding")
    ]
    assert store.list_system_events_by_type("contribution_funding_request_ack", limit=None) == []


def test_preflight_writes_nothing_at_all(store):
    """Every branch, not just the happy one."""
    _publish_pending_funding_request(store, "req-1")
    _claim_only(store, "req-1", phase="funding")
    _consumed_but_unsettled(store, "sig-1", with_manifest=False)
    _versioned_ack(store, "ap-1")
    _start_migration(store)
    before = _event_count(store)

    run_rollback_preflight(store)

    assert _event_count(store) == before


def test_every_failure_carries_its_own_invariant_and_identifier(store):
    _publish_pending_funding_request(store, "req-1")
    _claim_only(store, "req-1", phase="funding")
    _consumed_but_unsettled(store, "sig-1", with_manifest=True)
    _versioned_ack(store, "ap-1")
    _complete_migration(store)

    names = {f.invariant for f in run_rollback_preflight(store).failures}
    assert names == {
        "R1_workflow_claim_unresolved",
        "R2_dispatch_unsettled",
        "R3_approval_unresolved",
    }


def test_the_cli_reports_each_failed_invariant_separately(store, config_path):
    _versioned_ack(store, "ap-1")
    _consumed_but_unsettled(store, "sig-1", with_manifest=True)
    _complete_migration(store)

    result = CliRunner().invoke(cli.app, ["rollback-preflight", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 1
    assert "invariant=R2_dispatch_unsettled" in result.stdout
    assert "invariant=R3_approval_unresolved" in result.stdout


def test_the_legacy_command_name_still_works(store, config_path):
    _complete_migration(store)
    result = CliRunner().invoke(cli.app, ["approval-rollback-preflight",
                                          "--config", str(config_path),
                                          "--no-require-quiesce"])
    assert result.exit_code == 0


def test_quiesce_now_covers_every_writer_not_just_the_operator(store, config_path, monkeypatch):
    monkeypatch.setattr(
        cli.quiesce, "verify_quiesced",
        lambda **_: cli.quiesce.QuiesceReport(
            active_units=("maestro-symphony-signal-kr.timer",), queued_jobs=()
        ),
    )
    result = CliRunner().invoke(cli.app, ["rollback-preflight", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "maestro-symphony-signal-kr.timer" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_rollback_preflight.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.rollback_preflight'`

- [ ] **Step 3: Write minimal implementation**

Create `rollback_preflight.py` with the five checks, then rewrite the CLI command and
register the alias:

```python
app.command("approval-rollback-preflight")(rollback_preflight)
```

Keep the existing docstring paragraph about `_init_db` being additive-only, and add:

```
    이 명령은 어떤 호환성 상태도 복구하지 않는다. funding_workflow_completed에
    대응하는 legacy 종결 이벤트가 없으면 그 자리에서 실패한다 --
    complete_workflow가 둘을 한 트랜잭션으로 쓰므로, 없다는 것은 손상·수동
    변경·중간 빌드 중 하나라는 뜻이고, 여기서 지어내면 무엇이었는지 알 수
    없게 된다.
```

- [ ] **Step 4: Run tests / Step 5: Commit**

```bash
git add src/maestro/state/rollback_preflight.py src/maestro/cli.py tests/test_rollback_preflight.py
git commit -m "feat(3a-5): make rollback preflight exhaustive, read-only, and specific about why"
```

---

### Task 14: Re-upgrade after rollback fails closed

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_cli.py` (append)

**Interfaces:**
- Produces: `detect_reupgrade_after_rollback(store, *, cutoff: int) -> list[dict[str, Any]]`

**Detectors** (each is positive durable evidence that an *old* binary wrote after the
cutoff, not an inference from absence of new state):

1. `legacy_terminal_without_completion` — a `contribution_*_request_ack` /
   `..._decision` row with `id > cutoff` and no `funding_workflow_completed` for its
   `(request_id, phase)`. Current code writes both in one transaction, so this split can
   only come from a writer that does not know about the workflow.
2. `request_without_head` — a `contribution_*_request` row with `id > cutoff` whose
   `request_id` is named by no `funding_workflow_head` payload at any version.
   `publish_contribution_request` / `plan_contribution_request` commit the request and its
   head in one batch, so this too is only reachable from an old writer.

- [ ] **Step 1: Write the failing test**

```python
def test_old_code_writes_after_a_completed_migration_refuse_the_rerun(store, config_path):
    """migration_completed(cutoff=N) then a rollback, then old-binary writes at
    N+1.., then this binary again. Seeing only the completed marker and calling
    it done would leave that old-generation state unclassified forever."""
    ub.run_upgrade_backfill(store, "run-1")
    _legacy_ack_only(store, "req-rolled-back")   # old binary, post-cutoff

    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 1
    assert "reupgrade_after_rollback" in result.stdout
    assert "req-rolled-back" in result.stdout


def test_a_post_cutoff_request_with_no_head_is_also_evidence(store, config_path):
    ub.run_upgrade_backfill(store, "run-1")
    _legacy_pending_request(store, "req-old-writer", month_key="2026-09")

    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 1
    assert "request_without_head" in result.stdout


def test_ordinary_current_generation_activity_is_not_mistaken_for_a_rollback(store, config_path):
    ub.run_upgrade_backfill(store, "run-1")
    _publish_current_generation_request(store, "req-new", month_key="2026-09")
    _claim_and_complete(store, "req-new", phase="funding")

    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                          "--no-require-quiesce"])

    assert result.exit_code == 0
    assert "reupgrade_after_rollback" not in result.stdout


def test_the_refusal_does_not_start_a_second_migration_epoch(store, config_path):
    ub.run_upgrade_backfill(store, "run-1")
    cutoff = ms.load_migration_state(store).cutoff
    _legacy_ack_only(store, "req-rolled-back")
    CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path),
                                 "--no-require-quiesce"])

    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.COMPLETED
    assert state.cutoff == cutoff
    assert len(store.list_system_events_by_type(ms.STARTED_EVENT, limit=None)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_cli.py -k reupgrade -q`
Expected: FAIL — the rerun reports a clean no-op.

- [ ] **Step 3: Write minimal implementation**

Add `detect_reupgrade_after_rollback` and call it from `run_upgrade_backfill`'s COMPLETED
branch. On evidence, set `aborted_reason = "reupgrade_after_rollback"` and populate
`reupgrade_evidence` with `{"detector", "event_id", "identifier"}` dicts. Write nothing.

- [ ] **Step 4: Run tests / Step 5: Commit**

```bash
git add src/maestro/state/upgrade_backfill.py tests/test_upgrade_backfill_cli.py
git commit -m "feat(3a-5): refuse a re-upgrade over a database an old binary wrote to"
```

---

### Task 15: Real legacy-baseline database fixture and end-to-end migration

**Files:**
- Create: `scripts/generate_legacy_3a_fixture.py`, `tests/fixtures/legacy_3a_state.sql`,
  `tests/fixtures/legacy_3a_state.json`, `tests/test_upgrade_backfill_fixture.py`

**Design note:** `git diff 209ed4f..HEAD -- src/maestro/state/store.py` contains no DDL
changes — the SQLite schema is identical at the legacy baseline. The fixture's value is
therefore the *rows the old code actually wrote* and *what the old code believed about
them*, not a DDL upgrade. The test asserts the DDL equivalence explicitly rather than
pretending a table migration happens.

**Generation procedure** (documented in the script's module docstring and in
`docs/rollback_and_upgrade_3a.md`):

```bash
git worktree add /tmp/maestro-legacy 209ed4f18ed57773a72ab4a146e49efae1747348
cd /tmp/maestro-legacy
/home/symphony/maestro/.venv/bin/python \
    /home/symphony/maestro/scripts/generate_legacy_3a_fixture.py \
    --out /home/symphony/maestro/tests/fixtures
git worktree remove /tmp/maestro-legacy
```

The script imports `maestro.state.store.StateStore` **from the worktree** (it inserts the
worktree's `src` at the front of `sys.path` and refuses to run if
`maestro.state.funding_workflow` is importable — that module's presence means the current
generation is loaded and the fixture would not be legacy at all). It writes:

- `legacy_3a_state.sql` — `sqlite3 .dump` output, rows ordered by `id`, with a header
  comment naming the source SHA `209ed4f18ed57773a72ab4a146e49efae1747348`.
- `legacy_3a_state.json` — the **old binary's own answers**, produced by calling the
  baseline `TelegramOperatorBot._load_pending_funding_request` /
  `_load_pending_budget_request` for every request in the fixture. This is what pins the
  test to the legacy interpretation.

**Scenarios written into the fixture** (one per §22 item):

| id | Scenario |
|----|----------|
| `req-clean` | one unambiguous pending funding request |
| `req-amb-a`, `req-amb-b` | two pending funding requests in one workflow (same scope + month) |
| `req-acked` | pending funding request with a `contribution_funding_request_ack` |
| `req-budget-decided` | pending budget request with a `contribution_budget_request_decision` |
| `ap-legacy-done` | schema-less ack + `signal_approval_completed` on a one-group run |
| `ap-legacy-entered` | schema-less ack + an `approvals` row, no resolution |
| `ap-legacy-unknown` | schema-less ack, two groups on the run, one group-level completion |
| `sig-nomanifest` | `signal_package_consumed` with no settle and no manifest |

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upgrade_backfill_fixture.py
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from maestro.state import migration_state as ms
from maestro.state import upgrade_backfill as ub
from maestro.state.store import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_SHA = "209ed4f18ed57773a72ab4a146e49efae1747348"


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / "legacy_3a_state.sql").read_text())
    return path


def test_the_fixture_records_the_commit_it_was_generated_from(legacy_db):
    assert LEGACY_SHA in (FIXTURES / "legacy_3a_state.sql").read_text()


def test_opening_a_legacy_database_needs_no_ddl_change(legacy_db):
    """The 3a-4 upgrade is an event-semantics upgrade, not a schema upgrade.
    Asserting it keeps a future schema change from slipping in unnoticed."""
    before = _schema_sql(legacy_db)
    StateStore(legacy_db)
    assert _schema_sql(legacy_db) == before


def test_the_new_reader_agrees_with_the_old_binary_on_pre_migration_state(legacy_db):
    """Pinned to the legacy binary's own answers, recorded at generation time."""
    expected = json.loads((FIXTURES / "legacy_3a_state.json").read_text())
    store = StateStore(legacy_db)
    for request_id, was_pending in expected["pending_funding"].items():
        payload = _load_request(store, request_id, "funding")
        assert (payload is not None and payload["status"] == "pending") == was_pending


def test_a_full_migration_of_the_legacy_database_blocks_on_the_ambiguous_pair(legacy_db):
    store = StateStore(legacy_db)
    result = ub.run_upgrade_backfill(store, "run-1")

    assert result.aborted_reason == "blocking_quarantine"
    assert {q.reason for q in result.backfill.blocking} == {"ambiguous_pending_requests"}
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_after_the_operator_resolves_the_ambiguity_the_migration_completes(legacy_db):
    store = StateStore(legacy_db)
    ub.run_upgrade_backfill(store, "run-1")
    _operator_supersedes(store, "req-amb-b")     # the documented manual step

    result = ub.run_upgrade_backfill(store, "run-2")

    assert result.completed is True
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED


def test_the_clean_pending_request_keeps_a_live_head(legacy_db):
    store = StateStore(legacy_db)
    ub.run_upgrade_backfill(store, "run-1")
    _operator_supersedes(store, "req-amb-b")
    ub.run_upgrade_backfill(store, "run-2")

    heads = {h["request_id"] for h in store.list_funding_workflow_heads()}
    assert "req-clean" in heads


def test_the_legacy_acked_and_decided_requests_are_not_resurrected(legacy_db):
    store = StateStore(legacy_db)
    ub.run_upgrade_backfill(store, "run-1")
    _operator_supersedes(store, "req-amb-b")
    ub.run_upgrade_backfill(store, "run-2")

    heads = {h["request_id"] for h in store.list_funding_workflow_heads()}
    assert "req-acked" not in heads
    assert "req-budget-decided" not in heads


def test_no_approval_is_re_executed_and_none_gets_a_synthetic_resolution(legacy_db):
    store = StateStore(legacy_db)
    ub.run_upgrade_backfill(store, "run-1")
    _operator_supersedes(store, "req-amb-b")
    ub.run_upgrade_backfill(store, "run-2")

    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    ) == []
    quarantined = {q.identifier for q in ub.list_quarantines(store) if q.subsystem == "approval"}
    assert quarantined == {"ap-legacy-entered", "ap-legacy-unknown"}


def test_the_manifestless_dispatch_is_quarantined_not_replayed(legacy_db):
    store = StateStore(legacy_db)
    ub.run_upgrade_backfill(store, "run-1")

    quarantined = {q.identifier for q in ub.list_quarantines(store) if q.subsystem == "dispatch"}
    assert quarantined == {"sig-nomanifest"}


def test_the_fixture_is_reproducible_from_the_recorded_commit():
    """Regenerating from the pinned SHA must produce byte-identical output --
    otherwise the fixture drifts and stops representing the old binary."""
    if subprocess.run(["git", "cat-file", "-e", LEGACY_SHA], capture_output=True).returncode:
        pytest.skip("legacy baseline commit is not present in this clone")
    ...  # regenerate into a tmp dir via scripts/generate_legacy_3a_fixture.py and compare
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_upgrade_backfill_fixture.py -q`
Expected: FAIL — `tests/fixtures/legacy_3a_state.sql` does not exist.

- [ ] **Step 3: Write the generator, produce the fixture, commit both**

Run the worktree procedure above, then verify the fixture contains no
`funding_workflow_*` and no `signal_dispatch_manifest` rows:

```bash
grep -c funding_workflow tests/fixtures/legacy_3a_state.sql   # expect 0
grep -c signal_dispatch_manifest tests/fixtures/legacy_3a_state.sql  # expect 0
```

- [ ] **Step 4: Run tests / Step 5: Commit**

```bash
git add scripts/generate_legacy_3a_fixture.py tests/fixtures/ tests/test_upgrade_backfill_fixture.py
git commit -m "test(3a-5): migrate a database the pre-3a-4 binary actually wrote"
```

---

### Task 16: Runbook and authoritative-state documentation

**Files:**
- Create: `docs/rollback_and_upgrade_3a.md`
- Modify: `docs/operator_runbook.md`, `docs/vps_systemd.md`

Content, derived from the actual units in `deploy/systemd/`:

1. **Authoritative state model** — the five categories (current authoritative / rollback
   compatibility projection / historical / quarantine / migration metadata) and the table
   from this plan's inventory, stating explicitly that
   `contribution_funding_request_ack` and `contribution_budget_request_decision` are
   compatibility projections written atomically by `complete_workflow`, and that the
   current runtime never consults them to decide lifecycle.
2. **Upgrade flow** — the 23 steps: identify release; `capture_unit_states` output saved
   to a file; DB backup; `PRAGMA integrity_check`; disable/mask activators; stop in
   `QUIESCE_STOP_ORDER` (with the `maestro-run-once` ExecStopPost hazard called out);
   verify inactive; verify no queued jobs; `maestro upgrade-backfill`; inspect
   quarantines; **do not restart services while a blocking funding-ownership quarantine
   exists**; `PRAGMA integrity_check`; restore exactly the captured states; start;
   health/smoke; verify no unexpected automatic recovery fired.
3. **Rollback flow** — same quiesce; backup; integrity check; `maestro rollback-preflight`;
   any failed invariant aborts; deploy the old binary while still quiesced; verify old-code
   startup; restore exact states; health.
4. **Quarantine handling** — per reason: what evidence to gather, what the operator must
   decide, and which quarantines block completion (`ambiguous_pending_requests`,
   `head_ownership_conflict`, `malformed_workflow_identity`) versus which only isolate
   (`execution_may_have_been_entered`, `completion_unprovable`,
   `legacy_dispatch_no_manifest`).
5. **Explicit warning:** if the old binary writes state after a rollback, an ordinary
   future 3a re-upgrade will intentionally fail closed with `reupgrade_after_rollback` and
   requires a dedicated migration procedure. Do not force it.
6. **Deferred:** a future **Phase 3a-6 — legacy compatibility retirement** should, once
   the rollback window has expired, remove the legacy dual-write from
   `complete_workflow`, drop R4 from preflight, and drop the cutoff-gated legacy read in
   `_selected_contribution_budget`.

- [ ] **Step 1: Write the document**
- [ ] **Step 2: Add the link line to `docs/operator_runbook.md` and `docs/vps_systemd.md`**
- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs(3a-5): write the upgrade/rollback runbook against the units we actually ship"
```

---

### Task 17: Final compatibility-read inventory

**Files:**
- Test: `tests/test_authoritative_funding_state.py` (append)

- [ ] **Step 1: Re-run the inventory greps and record the result**

```bash
grep -rn 'contribution_funding_request_ack\|contribution_budget_request_decision' src/
```

Every remaining hit in `src/` must be one of exactly three things, and the test below
enforces it:
- `funding_workflow.LEGACY_TERMINAL_EVENT` / `_LEGACY_TERMINAL_KEY_PREFIX` (the projection
  definition),
- `complete_workflow`'s atomic dual-write,
- `rollback_preflight` R4 and `upgrade_backfill`'s legacy-terminal classification,
- `orchestrator._selected_contribution_budget`'s cutoff-gated amount read (Task 3b).

- [ ] **Step 2: Write the guard test**

```python
ALLOWED_LEGACY_READERS = {
    "src/maestro/state/funding_workflow.py",
    "src/maestro/state/upgrade_backfill.py",
    "src/maestro/state/rollback_preflight.py",
    "src/maestro/orchestration/orchestrator.py",
    "src/maestro/cli.py",
}


def test_no_new_module_starts_reading_the_compatibility_projection():
    """The projection exists for the old binary. A new reader here is a second
    definition of "finished" reappearing, which is the whole condition 3a-5
    removed."""
    root = Path(__file__).resolve().parents[1]
    offenders = {
        str(path.relative_to(root))
        for path in (root / "src").rglob("*.py")
        if "contribution_funding_request_ack" in path.read_text()
        or "contribution_budget_request_decision" in path.read_text()
    }
    assert offenders <= ALLOWED_LEGACY_READERS
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_authoritative_funding_state.py
git commit -m "test(3a-5): keep the compatibility projection from growing a second reader"
```

---

### Task 18: Full verification

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` — expect ≥1778 passed, 9 skipped, 0 failed.
- [ ] **Step 2:** `.venv/bin/python -m ruff check src tests` — expect "All checks passed!".
- [ ] **Step 3:** Focused re-run of every new module's tests.
- [ ] **Step 4:** Commit any fixes; do not claim completion until both commands pass.

---

## Plan self-review

1. **Every safety requirement maps to a task** — priority order & fail-closed: Global
   Constraints + Tasks 7/8/9/14; authoritative model: 1/3/3b; projection retained: 3/13;
   no synthesis: 8; manifest fence: 9; state machine: 4; immutable cutoff: 4/12;
   completed-last: 11/12; whole-operation lock: 6/11; quiesce: 5/11/13/16; backfill case
   table: 7; quarantine semantics: 7/8/9/16; runtime gates: 10; preflight read-only: 13;
   R0–R4: 13; re-upgrade: 14; legacy fixture: 15; crash model: 12; runbook: 16; cleanup
   inventory: 17; verification: 18.
2. **Every inventory seam has a disposition** — table rows 1–8 each name a task or state
   "already solved / no action".
3. **No obsolete task reintroduced** — old Task 4 (synthetic resolution) and the preflight
   auto-backfill are explicitly removed in Reconciliation §D; the stale-snapshot task is
   removed in §C.
4. **No 3a-4 safety property weakened** — `converge_workflow_invariants` is untouched;
   `complete_workflow`'s dual-write and fencing are untouched; the schema-less-ack refusal
   is preserved and now shared via `completed_legacy_approval_ids`.
5. **State categories explicit** — Task 16 §1 and the inventory table.
6. **Preflight read-only** — Task 13 `test_preflight_writes_nothing_at_all` plus the R4
   non-repair assertion.
7. **Completion is the final write** — Task 11 step 8 and
   `test_migration_completed_is_the_last_row_written`.
8. **Whole-operation locking** — Task 11 `test_the_writer_lock_is_held_for_the_whole_operation`
   and the `holds_writer_lock()` guards in Tasks 4 and 7.
9. **Quiesce reflects the real deployment** — Task 5 enumerates every file in
   `deploy/systemd/` and fails if a new one appears.
10. **Fixture exercises the real old writer** — Task 15 generates from `209ed4f` in a
    worktree and refuses to run if `funding_workflow` is importable.
11. **Crash/retry specified** — Task 12, six windows.
12. **Re-upgrade fails closed** — Task 14.
13. **No task depends on synthetic historical inference** — the only writes are
    deterministic v1 heads over provably-unique history, quarantine records, and the two
    migration markers.
