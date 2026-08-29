# Phase 3b Monthly Funding Workflow Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Telegram Operator the sole actionable funding/budget card sender and project each durable monthly funding workflow into one truthful, lifecycle-managed, workflow-scoped card without changing Phase 3a financial authority.

**Architecture:** Durable `funding_workflow_head`, request, supersession, claim, completion, and migration events remain the only financial authority. A new ephemeral `FundingWorkflowCardModel` is rebuilt from those events, rendered by the existing pure Telegram UI layer, and synchronized per chat through the existing intent-first lifecycle projection; legacy request-card copies are adopted through a provenance-bearing card event, never represented as a new send. Both the periodic sweep and immediate transition refresh call the same projection/synchronization service, while a separate pre-claim admission helper prevents a budget successor from acting before its funding predecessor is durably complete.

**Tech Stack:** Python >=3.11, Pydantic >=2.0, SQLite-backed `StateStore`, Telegram Bot API client, pytest >=7.0, Ruff >=0.8.0.

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md`, canonical section `B-1. Phase 3b: 월간 워크플로우 카드 아키텍처`, including revision 16 corrections.

## Global Constraints

- Plan and implement against repository baseline `df23fba205be410cf4b39acb391e9baafe2e3205` or the actual newer `main` after inspecting every intervening commit. Never reset or discard unrelated work.
- This is GitHub/development-repository work only. Production VPS deployment, production migration state, and production live-order incident remediation remain separate and must not be inferred from GitHub completion.
- Telegram Operator is the only network owner for actionable funding/budget cards. Do not preserve dual CLI/operator sending and do not add a cross-process Telegram delivery lease or CAS.
- `card_delivery_version` is Telegram delivery provenance only: missing/default `0` is legacy/raw-send generation and explicit `1` is lifecycle-owned generation. It never enters `funding_workflow_head.version`, head CAS, or financial claim logic.
- No deployable intermediate commit may persist `card_delivery_version=1` while any CLI raw actionable funding/budget sender remains active. The field describes actual delivery ownership, not capability prepared before Task 9.
- The logical card key is exactly `funding-workflow:<funding_workflow_id>`. Every funding/budget action callback continues to carry the exact `request_id`; never route a stale request callback to the current head.
- Unknown delivery is not non-delivery. `unknown` is never replayed automatically; only explicit `failed` evidence permits a retry, and edit replacement requires proof that the target is absent.
- Delivery and adoption decisions are per `(workflow_id, chat_id)`. Unknown in one chat does not suppress a safe send/edit in another chat.
- Version-0 workflows never expand to newly configured chats. Version-1 workflows may pin the current configured audience when the workflow-scoped lifecycle is first created.
- `MIGRATING` and `INVALID` stand down: no initial actionable send, no refresh that exposes a new financial action, no financial mutation, no head repair, and no migration repair.
- The monthly card is a display projection. Do not persist a second `active_request_id`, derived financial stage, selected budget, predecessor completion, or terminal financial result as Telegram authority.
- The monthly card never offers Resume. Existing incomplete-workflow recovery retains Resume. Approval/Reject remains on approval cards.
- No Phase 3b detail/fold callback is required. Do not add a workflow token registry or put the unbounded canonical `funding_workflow_id` in `callback_data`.
- Preserve the existing fail-closed child handoff: a parent workflow does not complete when the child request card outcome for the acting chat is failed or unknown.
- One malformed workflow must not stop projection or delivery for other workflows.
- Do not add best-effort stale-duplicate terminalization in this minimal cutover. Known stale callbacks remain financially fenced by request/head authority, and cleanup failure is not allowed to become a workflow dependency.
- Keep legacy terminal dual-write and rollback compatibility intact.
- **Explicitly out of scope:** Phase 3a head/CAS redesign; claim/fencing redesign; child-lineage redesign; automatic financial recovery; legacy dual-write removal; rollback preflight removal or weakening; Phase 3a-6; production VPS migration; production live-order incident remediation; Phase 4b exception wizard; Phase 5 global Telegram cleanup; broad unrelated `handlers.py`/lifecycle refactoring; a mandatory workflow-token registry; dual CLI/operator actionable-card ownership.

## Existing Authority and API Map

- `StateStore.load_funding_workflow_head(workflow_id)` and `StateStore.list_funding_workflow_heads()` select the highest durable head version. They are sufficient for workflow discovery; no workflow index table is needed.
- `load_request_payload(store, request_id, phase)` loads the authoritative funding/budget request event without a row limit.
- `funding_workflow_superseded` records durable predecessor/successor lineage. A legitimate child successor has `legitimate_successor=true` and `successor_of_phase`.
- `funding_workflow_claim` carries `attempt`, `intent`, and, for budget confirmation, `selected_budget`. `funding_workflow_completed` identifies the exact completed `(workflow_id, request_id, phase, attempt)`; terminal rendering must join these records.
- `list_incomplete_workflows(store)` reports claimed-but-uncompleted transitions to the existing recovery sweep, but durable claim state alone cannot distinguish an ordinary call still executing from work abandoned at a process boundary. The monthly projector marks `incomplete_transition` only after the existing recovery path has durably surfaced the exact attempt through `funding_workflow_stalled_notice` or `funding_workflow_needs_attention`; it never invents a timeout or invokes recovery.
- `StateStore.record_card_event`, `load_card_delivery_state`, `record_card_audience`, and `load_card_audience` already provide atomic event/projection persistence and per-card audience pinning. Adoption fits this event model; no new table is justified.
- `CardLifecycleManager.refresh` already preserves send ambiguity per chat, skips confirmed equal renders, retries proven failed sends, and escalates unknown/repeated failures. Phase 3b needs a strict edit-replacement policy without changing the approval-card default.
- `TelegramOperatorCommandRouter.poll_once` currently runs pending approvals, recovery notifications, approval lifecycle cards, incomplete workflow notices, and workflow convergence in that order. The monthly-card sweep can run immediately after the existing lifecycle-card sweep and tolerate one poll of convergence lag because it is read-only, validates each head/request, and isolates malformed workflows.
- `_confirm_budget_request` and `_cancel_budget_request` are the smallest common pre-claim budget transition entries: button callbacks, `/budget`, and workflow Resume all reuse them.
- `_deliver_child_signal_outcome` is the existing pre-completion handoff boundary. Phase 3b must replace its request-scoped send with workflow-card synchronization while preserving `_require_card_delivered`.
- `_run_daily_signal_approval` can classify funding/budget requirements from `StateStore.load_signal_package(signal_run_id)`; it no longer needs `RequestNotification` or any request-card network sender.

## Implementation File Map

| File | Responsibility in Phase 3b |
|---|---|
| `src/maestro/execution/funding_requests.py` | Backward-compatible funding delivery generation field; Task 9 activates builder generation 1 in the ownership-cutover commit. |
| `src/maestro/execution/budget_requests.py` | Backward-compatible budget delivery generation field; Task 9 activates builder generation 1 in the ownership-cutover commit. |
| `src/maestro/state/funding_workflow.py` | Read-only predecessor-completion admission predicate built on existing supersession/completion events; no claim/CAS changes. |
| `src/maestro/integrations/telegram/ui/funding_workflow.py` (new) | Ephemeral workflow lineage and `FundingWorkflowCardModel` projection from authoritative state. |
| `src/maestro/integrations/telegram/ui/funding_workflow_delivery.py` (new) | Per-chat workflow-card adoption and lifecycle synchronization; no financial writes. |
| `src/maestro/integrations/telegram/ui/card_state.py` | Provenance-bearing adoption event in the existing card event/projection convention. |
| `src/maestro/integrations/telegram/ui/cards.py` | Pure monthly workflow-card renderer; callbacks remain request-scoped. |
| `src/maestro/integrations/telegram/ui/catalog.py` | Exact Korean stage, terminal, and attention copy. |
| `src/maestro/integrations/telegram/ui/lifecycle.py` | Optional per-chat refresh subset and strict evidence-based edit replacement policy used only by monthly cards. |
| `src/maestro/integrations/telegram/bot.py` | Preserve Telegram rejection `method`, `error_code`, and `description`. |
| `src/maestro/integrations/telegram/handlers.py` | Minimal sweep insertion, shared immediate refresh calls, successor admission calls, and child handoff conversion. |
| `src/maestro/cli.py` | Remove actionable request-card network ownership and classify durable request presence truthfully. |
| `tests/test_contribution_funding_requests.py` (new) | Funding model legacy deserialization contract. |
| `tests/test_contribution_budget_requests.py` | Budget model legacy deserialization contract. |
| `tests/test_funding_workflow_card_projection.py` (new) | Authoritative state-to-card projection matrix and restart determinism. |
| `tests/test_funding_workflow_card_delivery.py` (new) | Adoption precedence, audience generation, per-chat independence, and no-replay behavior. |
| `tests/test_telegram_ui_cards.py` | Monthly renderer copy, buttons, callback identity, and callback byte limits. |
| `tests/test_telegram_card_lifecycle.py` | Rejection metadata classifications and strict replacement behavior. |
| `tests/test_funding_workflow_resume.py` | Successor admission, callbacks, child handoff, and immediate refresh crash boundaries. |
| `tests/test_telegram_monthly_funding_workflow.py` (new) | Periodic sweep, malformed isolation, migration stand-down, poll ordering, and restart integration. |
| `tests/test_cli_no_action_notice.py` | Persisted-request classification and preservation of informational no-action delivery. |
| `tests/test_signal_approval_handoff.py` | Request generation 1 assertions and removal of direct CLI actionable sender expectations. |
| `tests/test_multi_account_contributions.py` | Durable budget request generation 1 assertion. |
| `README.md` | Operator-facing summary of monthly workflow cards and ownership. |
| `docs/TRD.md` | Technical component/read-model/sender-ownership description. |
| `docs/ROADMAP.md` | Mark Phase 3a GitHub engineering and Phase 3b implementation accurately; keep production status separate. |
| `docs/TASKS.md` | Record the Phase 3b engineering deliverable. |
| `docs/operator_runbook.md` | Workflow-scoped card health, unknown behavior, per-chat inspection, and lack of an automatic replay/resolution command. |
| `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` | Update only the implementation-status sentence after verification; do not alter B-1 architecture. |

## Open Operational Detail (Not an Implementation Blocker)

The repository has no safe mechanism that turns an active request's `unknown` lifecycle copy into confirmed absence or confirmed presence. Phase 3b therefore records/preserves the unknown workflow copy, emits the existing buttonless ambiguity notice, reports it through `telegram_ui` health, and creates no second actionable representation. This plan does **not** invent a clear/reset/force-send command. The financial callback on the possibly existing original request card remains request-scoped and can proceed once predecessor admission is satisfied. A dedicated operator resolution procedure requires a separate approved architecture decision, but its absence does not block the specified fail-closed Phase 3b cutover.

## Deployability / Cutover Invariant

1. Tasks 1-8 are preparatory commits and must preserve the existing CLI/request-scoped delivery paths as the only runtime owners for their current scopes. They may add model compatibility, financial admission, pure projection/rendering, strict lifecycle semantics, adoption/delivery coordination, migration-fenced refresh/sweep helpers, and integration seams, but **no production scheduler, callback, Resume path, or child handoff invokes workflow-card network synchronization automatically**.
2. Generation 1 must not be written until Telegram Operator sole ownership is active in the same binary. Code being capable of processing generation 1 is not sufficient provenance.
3. Generation 0 is not by itself an ownership fence. An absent lifecycle copy blocks an initial send, but a legacy/request-scoped `failed` copy proves non-delivery and is retryable after adoption. Therefore the ownership barrier covers initial send, edit, adoption, and retry paths; pre-cutover runtime workflow synchronization remains disabled.
4. **Task 9 is the delivery ownership cutover commit and is the first commit allowed to produce generation-1 funding/budget requests or activate workflow-card delivery at runtime.** That one semantic unit activates both builders, schedules the workflow sweep, wires immediate refresh, routes child handoff through the workflow key, retires all CLI raw actionable funding/budget sends, and removes the Operator's request-scoped child sender.
5. No Phase 3b intermediate commit is approved for production deployment merely because it is independently testable; production deployability requires the explicit designation above and a separate deployment decision.
6. GitHub engineering completion remains separate from VPS deployment and migration verification. This plan is not a production deployment runbook.

Independent-commit safety follows the dependency order: Task 1 is additive default-0 compatibility only; Task 2 adds a fail-closed admission precondition; Tasks 3-4 add unused pure projection/rendering; Task 5 keeps stricter edit behavior opt-in; Task 6 adds an uninvoked coordinator; Tasks 7-8 add directly testable helpers but no production runtime caller; Task 9 activates every new runtime call site while removing every old actionable owner in the same commit; Task 10 changes documentation only after verification. Thus no preparatory commit can advertise generation 1, retry a legacy failed copy under a second card identity, weaken a financial gate, or create dual actionable ownership.

---

### Task 1: Add Backward-Compatible Delivery Generation Fields

**Files:**
- Create: `tests/test_contribution_funding_requests.py`
- Modify: `src/maestro/execution/funding_requests.py`
- Modify: `src/maestro/execution/budget_requests.py`
- Modify: `tests/test_contribution_budget_requests.py`

**Interfaces:**
- Consumes: Existing `ContributionFundingRequest`, `ContributionBudgetRequest`, `build_contribution_funding_request(...)`, and `build_contribution_budget_request(...)` APIs.
- Produces: `ContributionFundingRequest.card_delivery_version: int = 0` and `ContributionBudgetRequest.card_delivery_version: int = 0`.
- Invariant: `model_validate()` of a persisted payload missing the field yields `0`; direct model construction and both builders still emit/persist `0` until Task 9 atomically changes delivery ownership.

- [ ] **Step 1: Write failing legacy-deserialization tests**

Create the funding test and add the budget twin. Build a valid model, remove the field from its JSON payload, and validate it again so the test contains every required Pydantic field without duplicating a fragile fixture:

```python
def test_missing_card_delivery_version_is_legacy_generation():
    request = ContributionFundingRequest(
        request_id="fund_legacy",
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        account_id="kis_ps",
        execution_sleeve="tranquillo_ps",
        currency="KRW",
        available_cash=0.0,
        min_monthly_budget=1_000_000.0,
        required_shortfall=1_000_000.0,
        month_key="2026-08",
        created_at=datetime(2026, 8, 1),
        expires_at=datetime(2026, 8, 2),
    )
    payload = request.model_dump(mode="json")
    payload.pop("card_delivery_version", None)

    restored = ContributionFundingRequest.model_validate(payload)

    assert restored.card_delivery_version == 0
```

Use the same shape in `tests/test_contribution_budget_requests.py` for `ContributionBudgetRequest`.

- [ ] **Step 2: Write failing legacy-safe builder tests**

Call each real builder with the smallest existing valid fixture and assert the preparatory binary does not advertise ownership it does not yet have:

```python
assert funding_request is not None
assert funding_request.card_delivery_version == 0
assert budget_request is not None
assert budget_request.card_delivery_version == 0
```

These assertions are intentionally changed to generation 1 only in Task 9.

- [ ] **Step 3: Run the red tests**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py -q
```

Expected: FAIL because both models lack `card_delivery_version`.

- [ ] **Step 4: Add only the backward-compatible model fields**

Add this field to each request model immediately before `status`:

```python
card_delivery_version: int = 0
```

Do not add explicit builder arguments yet. Pydantic's default keeps both new-request builders at generation 0. Do not add the field in `plan_contribution_request`, head payloads, claims, completions, or CAS keys.

- [ ] **Step 5: Run focused and adjacent tests**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py -q
```

Expected: PASS. Missing persisted fields, direct constructors, and real builders all resolve to generation 0. This commit is safe with the still-active CLI sender because it does not claim Operator ownership.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/execution/funding_requests.py src/maestro/execution/budget_requests.py tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py
git commit -m "feat(3b): add legacy-safe card delivery generation"
```

### Task 2: Gate Successor Financial Admission on Predecessor Completion

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Modify: `tests/test_funding_workflow_resume.py`

**Interfaces:**
- Consumes: `StateStore.load_funding_workflow_head`, `funding_workflow_superseded`, `completed_key(...)`, and the existing `WorkflowClaimRefused` response path.
- Produces:

```python
def require_completed_predecessor(
    store: StateStore,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
) -> None:
    """Fail closed before claim when a legitimate successor's predecessor is incomplete."""
```

- Raises: `WorkflowClaimRefused("no_head")`, `WorkflowClaimRefused("not_head")`, `WorkflowClaimRefused("predecessor_ambiguous")`, or `WorkflowClaimRefused("predecessor_incomplete")`.
- Call boundary: `_confirm_budget_request` and `_cancel_budget_request` call it after deriving `workflow_id`/`request_id` and before `claim_workflow_attempt`. Funding requests and initial budget heads with no legitimate-successor marker have no predecessor gate.
- Operator response boundary: `_funding_claim_refusal_response` maps `predecessor_incomplete` to truthful “funding confirmation is still finishing” wording and `predecessor_ambiguous` to manual-attention wording; neither may fall through to “processed or superseded”.

- [ ] **Step 1: Write the failing stale-budget callback test**

In `tests/test_funding_workflow_resume.py`, create funding A, claim it, publish budget B as A's legitimate successor, leave A incomplete, and invoke the real B callback. Spy on the side-effect boundaries:

```python
before_claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
monkeypatch.setattr(operator_bot, "_run_child_signal", pytest.fail)
monkeypatch.setattr(operator_bot, "_refresh_portfolio_from_broker_snapshot", pytest.fail)

operator_bot.process_update(
    callback_update("operator:budget:sel:req-b:r")
)

after_claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
assert after_claims == before_claims
assert store.list_system_events_by_type("account_cash_flow", limit=None) == []
assert store.list_system_events_by_type("strategy_cash_flow", limit=None) == []
```

Assert the callback response is stale/fail-closed and contains no success acknowledgement. Add the cancellation twin so both budget transition helpers are covered.

- [ ] **Step 2: Write the failing completed-predecessor admission test**

Complete A with its exact attempt after B has become head, then call `require_completed_predecessor(...)` and assert it returns. Stub the B transition's downstream work and assert B can create its first claim. This proves the gate is not a permanent workflow freeze.

- [ ] **Step 3: Run the red tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_resume.py -k "predecessor or valid_head_with_incomplete_funding" -q
```

Expected: FAIL because a valid budget head currently enters `claim_workflow_attempt` without checking its durable predecessor completion.

- [ ] **Step 4: Implement the minimal read-only admission helper**

Implement `require_completed_predecessor` in `state/funding_workflow.py` with this exact decision order:

1. Validate `phase` with `_require_phase`.
2. Load the head; refuse `no_head` or `not_head` before examining lineage.
3. If `phase != "budget"`, return.
4. Scan `funding_workflow_superseded` for direct markers with the same `workflow_id`, `superseded_by == request_id`, `legitimate_successor is True`, and `successor_of_phase == "funding"`.
5. No matching marker means this is not a budget successor published from an open funding transition; return.
6. More than one distinct predecessor request means contradictory lineage; refuse `predecessor_ambiguous`.
7. Require `completed_key(workflow_id, predecessor_request_id, "funding")`; otherwise refuse `predecessor_incomplete`.

Export the helper through `__all__`. Do not edit `claim_workflow_attempt` or its atomic preconditions.

- [ ] **Step 5: Call the helper at both common budget admission points**

At the start of `_confirm_budget_request` and `_cancel_budget_request`, after extracting identifiers and before any validation that can trigger portfolio/network/financial work, call:

```python
require_completed_predecessor(
    self.store,
    workflow_id=workflow_id,
    request_id=request_id,
    phase="budget",
)
```

Because the predecessor completion event is append-only, a pre-claim read cannot become unsafe: absence rejects conservatively, while presence cannot disappear. The existing atomic claim still repeats the decisive current-head/CAS check.

Extend `_funding_claim_refusal_response` with these exact audit statuses:

```python
"predecessor_incomplete" -> "claim_predecessor_incomplete"
"predecessor_ambiguous" -> "claim_predecessor_ambiguous"
```

The first response tells the operator that funding confirmation has not finished and budget selection is not active. The second says lineage needs manual inspection. Do not describe either as a processed/superseded budget request.

- [ ] **Step 6: Run focused and adjacent workflow tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_resume.py tests/test_funding_workflow_transitions.py tests/test_funding_workflow_head.py tests/test_migration_runtime_gates.py -q
```

Expected: PASS. The new rejection occurs before a B claim, child run, broker refresh, cash-flow write, or completion; existing claim fencing and migration gates remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/state/funding_workflow.py src/maestro/integrations/telegram/handlers.py tests/test_funding_workflow_resume.py
git commit -m "fix(3b): gate successor actions on predecessor completion"
```

### Task 3: Project Authoritative Workflow State into an Ephemeral Read Model

**Files:**
- Create: `src/maestro/integrations/telegram/ui/funding_workflow.py`
- Create: `tests/test_funding_workflow_card_projection.py`
- Modify: `tests/test_funding_workflow_resume.py`

**Interfaces:**
- Consumes: `StateStore.load_funding_workflow_head`, `load_request_payload`, `list_incomplete_workflows`, `funding_workflow_superseded`, `funding_workflow_claim`, `funding_workflow_completed`, and the existing recovery path's `funding_workflow_stalled_notice` / `funding_workflow_needs_attention` events.
- Produces:

```python
FundingWorkflowPhase = Literal["funding", "budget"]
FundingWorkflowCardStage = Literal[
    "funding_pending",
    "funding_confirming",
    "funding_canceling",
    "budget_pending",
    "budget_applying",
    "budget_canceling",
    "funding_canceled",
    "budget_canceled",
    "budget_completed",
    "funding_completed",
]
FundingWorkflowAttention = Literal[
    "incomplete_transition",
    "predecessor_incomplete",
]

@dataclass(frozen=True)
class FundingWorkflowRequestRef:
    request_id: str
    phase: FundingWorkflowPhase
    lineage_distance: int  # current request is 0; direct predecessor is 1

@dataclass(frozen=True)
class FundingWorkflowCardModel:
    workflow_id: str
    month_key: str
    scope: tuple[object, object, object, object]
    phase: FundingWorkflowPhase
    request_id: str
    request: Mapping[str, Any]
    stage: FundingWorkflowCardStage
    attention: FundingWorkflowAttention | None
    financial_actions_allowed: bool
    terminal_intent: Literal["confirm", "cancel"] | None
    selected_budget: float | None
    predecessor_request_id: str | None
    predecessor_completed: bool | None
    card_delivery_version: int
    lineage: tuple[FundingWorkflowRequestRef, ...]

    @property
    def lifecycle_stage(self) -> CardStage: ...

def funding_workflow_card_key(workflow_id: str) -> str:
    return f"funding-workflow:{workflow_id}"

def recovery_owned_incomplete_attempts(
    store: StateStore,
    workflow_id: str,
) -> frozenset[tuple[str, FundingWorkflowPhase, int]]:
    """Return exact (request_id, phase, attempt) identities already surfaced by recovery."""

def project_funding_workflow_card(
    store: StateStore,
    workflow_id: str,
) -> FundingWorkflowCardModel: ...
```

- Authority rule: the model is never saved. A process restart over the same durable events must produce an equal model.

- [ ] **Step 1: Write the failing stage matrix tests**

Use event-building helpers in the new test file and parameterize all required states:

```python
@pytest.mark.parametrize(
    ("phase", "intent", "completed", "expected_stage", "actions"),
    [
        ("funding", None, False, "funding_pending", True),
        ("funding", "confirm", False, "funding_confirming", False),
        ("funding", "cancel", False, "funding_canceling", False),
        ("budget", None, False, "budget_pending", True),
        ("budget", "confirm", False, "budget_applying", False),
        ("budget", "cancel", False, "budget_canceling", False),
        ("funding", "cancel", True, "funding_canceled", False),
        ("budget", "cancel", True, "budget_canceled", False),
        ("budget", "confirm", True, "budget_completed", False),
        ("funding", "confirm", True, "funding_completed", False),
    ],
)
def test_projects_truthful_monthly_stage(...): ...
```

The funding-completed case must have no successor. The budget-completed case asserts `selected_budget` equals the completed claim's recorded input.

- [ ] **Step 2: Write failing attention and lineage tests**

Cover these exact projections:

- Funding A has an open claim, legitimate budget B is head, and A is not completed: B has `stage == "budget_pending"`, `attention == "predecessor_incomplete"`, `predecessor_completed is False`, and `financial_actions_allowed is False`.
- After completing A: the same B has `attention is None`, `predecessor_completed is True`, and `financial_actions_allowed is True`.
- An ordinary current funding confirm/cancel or budget confirm/cancel claim, with no existing recovery notice for its exact attempt, keeps its truthful in-progress stage and has `attention is None`.
- In `tests/test_funding_workflow_resume.py`, create the same open claim, call the existing `_sweep_incomplete_workflows()` so the repository's current recovery path durably emits `funding_workflow_stalled_notice`, then project it and assert the in-progress stage is unchanged while `attention == "incomplete_transition"`. Add the exhausted-attempt twin using the existing `funding_workflow_needs_attention` path.
- A recovery notice for another request, phase, workflow, or attempt does not add attention to the current claim.
- The lineage tuple is current-first and follows `superseded_by` links, never event timestamps or event IDs.
- A funding completion with an approval child but no budget successor remains `funding_completed`, not `budget_completed`.

The repository has no durable "process is currently executing this claim" marker. `list_incomplete_workflows()` deliberately includes any claimed-but-uncompleted transition because the recovery UI is conservative about a process that may only look dead. Therefore raw list membership is not sufficient for monthly-card warning copy. Reusing the recovery sweep's exact-attempt notice event is the smallest deterministic boundary: it may delay the overlay until recovery has surfaced the attempt, but it neither labels normal in-call work as stalled nor creates a timeout or second recovery state machine.

- [ ] **Step 3: Write the failing legacy/default and restart tests**

Assert a stored current request without `card_delivery_version` projects `0`. Reopen the same SQLite file with a new `StateStore`, project again, and assert exact dataclass equality. Assert no new system event or card-state row was written by either projection.

- [ ] **Step 4: Run the red projection suite**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_card_projection.py -q
```

Expected: FAIL on import because the new module and interfaces do not exist.

- [ ] **Step 5: Implement the minimal projector**

Implement read-only helpers inside the new file:

- `_request_phase(store, request_id) -> FundingWorkflowPhase` probes funding then budget payloads and raises on missing or dual-phase corruption.
- `_lineage(store, workflow_id, current_request_id) -> tuple[FundingWorkflowRequestRef, ...]` builds a predecessor map from exact `superseded_by` links and rejects cycles/ambiguous direct predecessors.
- `_claims_by_identity(...)` and `_completions_by_identity(...)` join completion to its exact attempt; a terminal state without the corresponding claim raises `ValueError` rather than guessing from legacy ack/decision events.
- `_required_funding_predecessor(...)` recognizes only the direct legitimate funding successor marker used by Task 2.
- `recovery_owned_incomplete_attempts(...)` intersects `list_incomplete_workflows(store)` with exact `(workflow_id, request_id, phase, attempt)` identities already recorded by `funding_workflow_stalled_notice` or `funding_workflow_needs_attention`. It reads existing events only and returns an ephemeral set.
- `project_funding_workflow_card(...)` validates the head phase/request/scope, derives the active claim/completion, applies the stage matrix, and overlays `incomplete_transition` only when the exact open claim is in `recovery_owned_incomplete_attempts(...)`. A raw open claim still produces the truthful in-progress stage but no warning.

Do not consult `contribution_funding_request_ack` or `contribution_budget_request_decision` for terminal truth.

- [ ] **Step 6: Run focused and authority regression tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_card_projection.py tests/test_funding_workflow_resume.py tests/test_authoritative_funding_state.py tests/test_funding_workflow_head.py tests/test_funding_workflow_transitions.py -q
```

Expected: PASS. Ordinary in-flight claims are not mislabeled; only the existing recovery authority's durable exact-attempt evidence adds the attention overlay. No Phase 3a authority test changes.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/ui/funding_workflow.py tests/test_funding_workflow_card_projection.py tests/test_funding_workflow_resume.py
git commit -m "feat(3b): project monthly funding workflow cards"
```

### Task 4: Render the Unified Monthly Card with Request-Scoped Actions

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/cards.py`
- Modify: `src/maestro/integrations/telegram/ui/catalog.py`
- Modify: `tests/test_telegram_ui_cards.py`

**Interfaces:**
- Consumes: `FundingWorkflowCardModel`, existing `RenderedCard`, `funding_request_reply_markup(request_id)`, and `budget_request_reply_markup(request)`.
- Produces:

```python
def render_funding_workflow_card(model: FundingWorkflowCardModel) -> RenderedCard: ...
```

- Copy contract:
  - `funding_pending`: `📥 입금이 필요해요`
  - `funding_confirming`: `⏳ 입금을 확인하고 있어요`
  - `funding_canceling`: `⏳ 취소를 처리하고 있어요`
  - `budget_pending`: `💰 이번 달 예산을 선택해 주세요`
  - `budget_applying`: `⏳ 예산을 적용하고 있어요`
  - `budget_canceling`: `⏳ 취소를 처리하고 있어요`
  - `funding_canceled`: `🛑 이번 달 입금 요청을 취소했어요`
  - `budget_canceled`: `🛑 이번 달 예산 선택을 취소했어요`
  - `budget_completed`: `✅ 이번 달 예산을 확정했어요`
  - `funding_completed`: `✅ 자금 확인을 마쳤어요`
  - `predecessor_incomplete` first line: `⚠️ 자금 확인을 마무리하고 있어요`; body states that the budget request is prepared but cannot yet be selected.
  - `incomplete_transition` retains the truthful stage first line and adds `⚠️ 처리가 끝나지 않아 확인이 필요해요.` as an overlay line.

- [ ] **Step 1: Write failing renderer snapshots for every stage**

Parameterize `FundingWorkflowCardModel` fixtures and assert exact first lines, button presence, and selected-budget display. In particular:

```python
rendered = render_funding_workflow_card(model)
assert rendered.text.splitlines()[0] == expected_first_line
if model.financial_actions_allowed:
    assert rendered.reply_markup is not None
else:
    assert rendered.reply_markup is None
```

For `budget_completed`, assert the completed claim amount is shown using the existing Korean money formatter. For `funding_completed`, assert no text claims that a budget was selected.

- [ ] **Step 2: Write failing authority/identity tests for buttons**

Assert:

- Funding buttons contain `operator:funding:complete:<request_id>` and `operator:funding:cancel:<request_id>`.
- Budget buttons contain the existing bounded `operator:budget:sel:<request_id>:m|r|f` and cancel payload.
- No callback contains `funding_workflow_id`, `operator:wfresume`, or `operator:appr`.
- Every callback payload is at most 64 UTF-8 bytes for IDs produced by `new_funding_request_id()` and `new_budget_request_id()`.
- Attention and terminal cards are buttonless.
- There is no detail/fold button for this Phase 3b card.

- [ ] **Step 3: Run the red renderer tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_ui_cards.py -k "funding_workflow or monthly" -q
```

Expected: FAIL because `render_funding_workflow_card` and its catalog strings do not exist.

- [ ] **Step 4: Implement the pure renderer and catalog data**

Add only Phase 3b strings to `catalog.py`. Implement `render_funding_workflow_card` as a pure mapping from model fields to text and markup. Reuse the existing request-markup helpers so financial callback spelling and byte-bounded budget selection tokens do not fork.

The renderer must not read `StateStore`, lifecycle state, migration state, or Telegram configuration.

- [ ] **Step 5: Run renderer and existing UI regressions**

Run:

```bash
.venv/bin/pytest tests/test_telegram_ui_cards.py tests/test_telegram_approval_stage.py tests/test_contribution_budget_requests.py -q
```

Expected: PASS. Approval card detail/fold behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui/cards.py src/maestro/integrations/telegram/ui/catalog.py tests/test_telegram_ui_cards.py
git commit -m "feat(3b): render monthly funding workflow cards"
```

### Task 5: Classify Telegram Edit Rejections by Evidence

**Files:**
- Modify: `src/maestro/integrations/telegram/bot.py`
- Modify: `src/maestro/integrations/telegram/ui/card_state.py`
- Modify: `src/maestro/integrations/telegram/ui/lifecycle.py`
- Modify: `tests/test_telegram_card_lifecycle.py`
- Modify: `tests/test_telegram_card_state.py`
- Modify: `tests/test_telegram_approval.py`

**Interfaces:**
- Consumes: Existing `TelegramApiRejected`, `CardLifecycleManager.refresh`, `card_failure_event`, and operation-id intent/result/failure events stored as `telegram_ui_card` system events.
- Produces:

```python
class TelegramApiRejected(RuntimeError):
    method: str | None
    error_code: int | None
    description: str | None

    def __init__(
        self,
        message: str | None = None,
        *,
        method: str | None = None,
        error_code: int | None = None,
        description: str | None = None,
    ) -> None: ...

EditReplacementPolicy = Literal[
    "replace_on_rejection",
    "replace_on_target_absence",
]

def card_failure_event(
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    error: str,
    *,
    method: str | None = None,
    error_code: int | None = None,
    description: str | None = None,
) -> dict[str, Any]: ...

def CardLifecycleManager.refresh(
    self,
    run_id: str,
    card_key: str,
    stage: str,
    rendered: RenderedCard,
    *,
    chat_ids: Sequence[int] | None = None,
    edit_replacement_policy: EditReplacementPolicy = "replace_on_rejection",
) -> dict[str, Any]: ...
```

- Compatibility boundary: existing approval callers omit both keywords and retain their tested `replace_on_rejection` behavior. Monthly funding delivery passes `replace_on_target_absence`.

- [ ] **Step 1: Write the failing Bot API metadata test**

Patch `urllib.request.urlopen` first to return, and then to raise an `urllib.error.HTTPError` whose body contains:

```json
{"ok": false, "error_code": 400, "description": "Bad Request: message to edit not found"}
```

Call `TelegramBotAPIClient.edit_message_text(...)` in both cases and assert the exception has `method == "editMessageText"`, `error_code == 400`, and the exact description. This ensures non-2xx Bot API JSON is not collapsed into a transport `RuntimeError`. Existing `TelegramApiRejected("text")` construction must remain valid for current fakes.

- [ ] **Step 2: Replace the broad edit-rejection test with four failing strict-policy tests**

In `tests/test_telegram_card_lifecycle.py`, call `refresh(..., edit_replacement_policy="replace_on_target_absence")` and assert:

1. Description contains `message is not modified`: result converges on the existing `message_id`, projection becomes confirmed with the desired render hash, and no send occurs.
2. Description contains `message to edit not found`: replacement send occurs once.
3. A generic explicit rejection such as `Bad Request: can't parse entities`: a failure event is recorded, the existing `message_id` remains, no replacement send occurs, and a subsequent sweep retries the edit rather than sending.
4. Edit raises `TimeoutError`: the intent remains `unknown`, no replacement occurs, and a subsequent refresh emits only the buttonless ambiguity notice.

Also retain the explicit send rejection retry and send timeout/no-replay tests.

In `tests/test_telegram_card_state.py`, add direct event/fold regressions:

```python
legacy = card_failure_event(
    "approval:a1",
    100,
    "pending",
    "hash",
    "op-1",
    "rejected",
)
enriched = card_failure_event(
    "funding-workflow:w1",
    100,
    "budget_pending",
    "hash-2",
    "op-2",
    "Bad Request: can't parse entities",
    method="editMessageText",
    error_code=400,
    description="Bad Request: can't parse entities",
)

assert "method" not in legacy
assert enriched["method"] == "editMessageText"
assert enriched["error_code"] == 400
assert enriched["description"] == "Bad Request: can't parse entities"

store.record_card_event("run-1", enriched)
row = store.list_system_events_by_type("telegram_ui_card", limit=None)[0]
assert row["payload"]["method"] == "editMessageText"
assert row["payload"]["error_code"] == 400
assert row["payload"]["description"] == "Bad Request: can't parse entities"
```

`card_failure_event()` returns the event payload dictionary directly; only the stored system-event row wraps that dictionary as `row["payload"]`. Persist and fold the enriched event after a confirmed copy. Assert delivery remains `failed`, the prior `message_id` remains available exactly as before, and the existing consecutive-failure count is unchanged except for the same increment the legacy failure event already caused. This proves metadata is diagnostic event payload only, does not alter card projection semantics, and cannot become financial authority.

- [ ] **Step 3: Run the red lifecycle tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_approval.py tests/test_telegram_card_lifecycle.py tests/test_telegram_card_state.py -q
```

Expected: FAIL because rejection metadata is discarded and every explicit edit rejection currently triggers a fresh send.

- [ ] **Step 4: Preserve rejection metadata in the Bot API client**

On `HTTPError`, read its response body and pass it through the same JSON decoding/classification path; only a body that cannot be decoded remains transport ambiguity. When `decoded["ok"]` is false, normalize only typed evidence:

```python
raw_code = decoded.get("error_code")
error_code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
raw_description = decoded.get("description")
description = raw_description if isinstance(raw_description, str) else None
raise TelegramApiRejected(
    method=method,
    error_code=error_code,
    description=description,
)
```

Keep the exception a `RuntimeError` subclass. Update its docstring to mean “Telegram explicitly rejected this API operation”; only a rejected `sendMessage` proves that no new message was created, while an edit rejection requires the evidence classification below.

- [ ] **Step 5: Implement strict edit classification without changing approval defaults**

First extend `card_failure_event(...)` in `ui/card_state.py` with the exact optional keyword-only parameters shown in **Interfaces**. Add only non-`None` values to the existing `telegram_ui_card` event payload:

```python
payload = {"error": error}
if method is not None:
    payload["method"] = method
if error_code is not None:
    payload["error_code"] = error_code
if description is not None:
    payload["description"] = description
return _event(
    "failure",
    card_key,
    chat_id,
    stage,
    render_hash,
    operation_id,
    **payload,
)
```

There is no SQLite schema change: these fields live only in the existing system-event JSON payload. Existing callers that pass only `error` keep byte-for-byte equivalent event payloads, and `resolve_card_copies` continues to fold on the existing operation/delivery fields without consulting rejection metadata.

For `replace_on_target_absence`, classify only case-insensitive description evidence:

- Contains `message is not modified` -> write `card_result_event` for the same message and desired hash; no send.
- Contains `message to edit not found` -> write failure, then allow `_deliver_one` replacement.
- Every other explicit rejection -> write failure, run repeated-failure escalation when the counter reaches `FALLBACK_AFTER_FAILURES`, and do not send a replacement.
- Transport/parsing exception -> leave the edit intent as unknown and do not replay.

Apply `chat_ids` only as an intersection with the lifecycle's already resolved/pinned audience; it must never introduce an unpinned chat.

When writing a rejection failure, pass `exc.method`, `exc.error_code`, and `exc.description` into the extended `card_failure_event`. Add an assertion that these fields survive in the `telegram_ui_card` event payload; this is existing-event persistence, not a schema change.

- [ ] **Step 6: Run lifecycle and approval regressions**

Run:

```bash
.venv/bin/pytest tests/test_telegram_card_lifecycle.py tests/test_telegram_card_state.py tests/test_telegram_approval_card.py tests/test_telegram_approval_stage.py tests/test_telegram_operator_ui.py -q
```

Expected: PASS. Strict replacement is opt-in; approval-card network behavior is unchanged. Legacy error-only callers, message-id preservation, and consecutive-failure folding remain backward-compatible, while rejection metadata survives only as diagnostic `telegram_ui_card` payload.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/bot.py src/maestro/integrations/telegram/ui/card_state.py src/maestro/integrations/telegram/ui/lifecycle.py tests/test_telegram_card_lifecycle.py tests/test_telegram_card_state.py tests/test_telegram_approval.py
git commit -m "fix(3b): classify telegram edit rejection evidence"
```

### Task 6: Adopt Legacy Request Cards Per Chat

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/card_state.py`
- Create: `src/maestro/integrations/telegram/ui/funding_workflow_delivery.py`
- Create: `tests/test_funding_workflow_card_delivery.py`

**Interfaces:**
- Consumes: `FundingWorkflowCardModel.lineage`, `funding_workflow_card_key`, request-scoped keys `funding-request:<request_id>` and `budget-request:<request_id>`, `StateStore` card/audience APIs, `render_funding_workflow_card`, and strict lifecycle refresh from Task 5.
- Produces:

```python
def card_adoption_event(
    card_key: str,
    chat_id: int,
    *,
    source: CardCopy,
    source_request_id: str,
    source_phase: str,
) -> dict[str, Any]: ...

WorkflowCardSyncOutcome = Literal[
    "sent", "edited", "skipped", "failed", "unknown", "blocked"
]

@dataclass(frozen=True)
class FundingWorkflowCardSyncResult:
    card_key: str
    outcomes: Mapping[int, WorkflowCardSyncOutcome]

    def outcome_for(self, chat_id: int) -> WorkflowCardSyncOutcome:
        return self.outcomes.get(chat_id, "blocked")

class FundingWorkflowCardDelivery:
    def __init__(self, store: StateStore, lifecycle: CardLifecycleManager) -> None: ...

    def sync(
        self,
        run_id: str,
        model: FundingWorkflowCardModel,
    ) -> FundingWorkflowCardSyncResult: ...
```

- Adoption event payload: `phase="adoption"`, inherited `delivery`, inherited `message_id`, target `card_key`, `chat_id`, **inherited source stage/render hash**, `operation_id="adopt:<source-operation-id>"`, `adopted_from_card_key`, `adopted_from_operation_id`, `adopted_from_request_id`, `adopted_from_phase`, and deterministic duplicate key `telegram-ui-card:adoption:<target-card-key>:<chat-id>:<source-card-key>:<source-operation-id>`. It must not use phase `result` and must not claim that Telegram sent a message. Inheriting the old render hash is required: recording the desired workflow render hash before the physical edit would make `refresh` skip that edit as already converged.

- [ ] **Step 1: Write failing adoption precedence tests**

Build durable workflows and request-scoped card copies with `card_intent_event`, `card_result_event`, and `card_failure_event`. Cover each exact per-chat case:

- Current request confirmed -> adopt its `message_id`, edit it to the workflow projection, and send nothing.
- Current request failed -> adopt known non-delivery and allow a retry in that chat.
- Current request unknown -> adopt unknown, emit buttonless ambiguity notice, do not send, and do not edit a predecessor.
- No current copy + nearest confirmed predecessor -> adopt predecessor and edit it.
- Current unknown + confirmed predecessor -> current unknown wins; predecessor is not promoted.
- Superseded predecessor unknown + current generation 1 with no current evidence -> the stale unknown remains visible under its legacy key but does not block the safe current initial send.
- Multiple confirmed predecessors -> choose the smallest `lineage_distance`, even when an older lineage candidate has the newest event timestamp/ID.
- Existing workflow-scoped copy in a chat -> ignore every request-scoped event for that chat.

Assert the adoption history row has `phase == "adoption"`, inherited source hash/stage, and provenance fields, with no `result` event for the adoption operation. Fold the event with `resolve_card_copies` and assert adoption preserves the explicit inherited `delivery`; extend that fold to read the event's validated `delivery` for phase `adoption` instead of mapping every unfamiliar phase to unknown.

- [ ] **Step 2: Write failing audience and per-chat tests**

Cover:

- Generation 0 with no lifecycle evidence -> every configured chat outcome is `blocked`; no audience or send is created.
- Generation 1 with no lifecycle evidence -> current configured audience is pinned and each chat gets an initial intent-first send.
- Generation 0 with evidence in chat 100 and newly configured chat 200 -> only chat 100 is pinned/updated; chat 200 is not sent.
- Current request unknown in chat 100 and no evidence in chat 200 for generation 1 -> chat 100 remains unknown while chat 200 receives its safe initial send.
- Current confirmed copy in chat 100 and current failed copy in chat 200 -> adopt/edit 100 and retry 200 independently.
- A workflow-scoped state in chat 100 prevents legacy reclaim only in chat 100; chat 200 still follows its own current-request-first precedence.

- [ ] **Step 3: Run the red delivery suite**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_card_delivery.py -q
```

Expected: FAIL on import because adoption event and delivery coordinator do not exist.

- [ ] **Step 4: Implement the provenance-bearing adoption event**

Create an event helper that writes directly through the existing `record_card_event` contract. Preserve the source stage, render hash, delivery classification, and message ID. Teach `resolve_card_copies` to accept `confirmed`, `failed`, or `unknown` from an adoption event's explicit `delivery` field. Use the deterministic duplicate key above; because `record_card_event` writes history and projection atomically, a crash cannot leave adoption history without the workflow-scoped projection or vice versa.

Do not alter the SQLite schema.

- [ ] **Step 5: Implement per-chat precedence and safe synchronization**

For each chat, apply this exact order:

1. Existing target workflow copy wins.
2. Inspect the current request's request-scoped copy in that chat.
3. Adopt current `confirmed`, `unknown`, or `failed` evidence unchanged.
4. Only when current evidence is absent, scan `model.lineage[1:]` for the nearest confirmed predecessor in that chat. Ignore predecessor unknown/failed copies as adoption candidates.
5. If there is still no target copy, generation 1 permits initial send; generation 0 returns `blocked`.

Pin audience once:

- Generation 1 with no recorded target audience -> the lifecycle's current configured chats.
- Generation 0 -> only chats where a target copy was adopted or already existed, intersected with current allowed chats.

Call lifecycle `refresh` once with the safe chat subset and `edit_replacement_policy="replace_on_target_absence"`. Convert lifecycle grouped output into one outcome per chat. Unknown in one chat must never remove another chat from the safe subset.

- [ ] **Step 6: Run focused and adjacent delivery tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_card_delivery.py tests/test_telegram_card_lifecycle.py tests/test_telegram_card_state.py tests/test_health_cli.py -q
```

Expected: PASS. Existing health reporting continues to show unknown legacy and workflow-scoped copies independently.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/ui/card_state.py src/maestro/integrations/telegram/ui/funding_workflow_delivery.py tests/test_funding_workflow_card_delivery.py
git commit -m "feat(3b): adopt legacy funding request cards"
```

### Task 7: Prepare the Migration-Fenced Workflow-Card Refresh/Sweep Service

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Create: `tests/test_telegram_monthly_funding_workflow.py`

**Interfaces:**
- Consumes: `StateStore.list_funding_workflow_heads`, `project_funding_workflow_card`, `FundingWorkflowCardDelivery.sync`, and `_migration_block_reason()`.
- Produces:

```python
def TelegramOperatorCommandRouter._refresh_funding_workflow_card(
    self,
    workflow_id: str,
) -> FundingWorkflowCardSyncResult: ...

def TelegramOperatorCommandRouter._sweep_funding_workflow_cards(self) -> None: ...
```

- Shared migration fence: `_refresh_funding_workflow_card` is the only Phase 3b projection/delivery mutation entry and checks `_migration_block_reason()` before calling `project_funding_workflow_card` or `FundingWorkflowCardDelivery.sync`. When blocked, it returns `FundingWorkflowCardSyncResult(card_key=funding_workflow_card_key(workflow_id), outcomes={})`; `outcome_for(chat_id)` therefore returns `"blocked"` without creating an audience, adoption, intent, result, or failure event.

- Construction: `TelegramOperatorCommandRouter.__init__` creates one `FundingWorkflowCardDelivery(self.store, self._card_manager)`.
- Runtime ownership boundary: these helpers are callable only from focused tests in this task. Do not add `_sweep_funding_workflow_cards` to `poll_once`, and do not call either helper from callbacks, commands, Resume, or child handoff. Constructing the coordinator is inert without invocation; Task 9 activates every production runtime caller while removing the old sender.

- [ ] **Step 1: Write the failing generation and restart sweep tests**

In the new integration file, invoke `_sweep_funding_workflow_cards()` directly with the existing fake Telegram client/router pattern and assert:

- Version 0 head + no request-scoped/workflow-scoped lifecycle evidence -> sweep sends nothing.
- Version 0 head + current request-scoped `failed` evidence -> a **direct test invocation** may adopt and retry under the workflow key. Assert `poll_once` still does not invoke the helper. This capability is why generation 0 is not an ownership fence and why runtime activation waits for Task 9.
- A test fixture that explicitly persists a version 1 head + no lifecycle evidence -> sweep sends exactly one workflow-scoped actionable card per configured chat. Tasks 1-8 do not make production builders create this fixture state; this proves Operator capability before the atomic Task 9 activation.
- Constructing a new router over the same SQLite database and sweeping again does not send a duplicate; it skips or edits the confirmed physical message according to the render hash.
- The card key in projection state is exactly `funding-workflow:<canonical workflow id>`.

- [ ] **Step 2: Write the failing shared-boundary migration and side-effect tests**

Parameterize durable migration markers for `MIGRATING` and `INVALID`. For each state, call `_refresh_funding_workflow_card(workflow_id)` **directly**, without going through the sweep, and assert `outcome_for(configured_chat_id) == "blocked"`, no send/edit occurs, and card audience, adoption, card-event, request, head, claim, completion, cash-flow, child, and migration event counts do not change. Repeat through `_sweep_funding_workflow_cards()` to prove both entry paths stand down.

Add the deterministic race-shaped test: first persist a completed funding transition and its already-published budget successor while migration is normal; then persist the existing marker sequence that makes `load_migration_state` return `MIGRATING`; finally call `_refresh_funding_workflow_card(workflow_id)` directly. Assert the durable financial completion/head remain unchanged, but no edit exposes B's budget buttons and no workflow-card state/audience/adoption event is written. Add the `INVALID` direct-refresh twin. No real threads are needed because the safety boundary is the durable state observed immediately before UI refresh.

The assertion is one-way: migration state does not roll back already durable financial truth; it only prevents Phase 3b UI mutation and new action exposure while migration authority says to stand down.

For a normal migration state, monkeypatch these symbols to raise if called: `claim_workflow_attempt`, `_run_child_signal`, `complete_workflow`, cash-flow record helpers, and `converge_workflow_invariants`. The card sweep must still render/send.

- [ ] **Step 3: Write the failing malformed-isolation test**

Create one valid workflow and one head whose request is missing or phase contradicts its payload. Directly invoke the helper and assert the valid card is still processed and one failure is audited/logged. Add a `poll_once` spy asserting `_sweep_funding_workflow_cards` is **not** called in this preparatory binary; the production poll-order assertion moves to Task 9.

- [ ] **Step 4: Run the red operator sweep tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_monthly_funding_workflow.py -q
```

Expected: FAIL because the router has no monthly workflow-card service or callable helper.

- [ ] **Step 5: Implement the helpers without production runtime wiring**

Implement `_refresh_funding_workflow_card` as the only handler entry that projects and synchronizes a workflow. Implement `_sweep_funding_workflow_cards` as:

1. In `_refresh_funding_workflow_card`, check `_migration_block_reason()` first and return the blocked result above before projection, audience resolution, adoption, or lifecycle delivery.
2. In `_sweep_funding_workflow_cards`, retain an early return for the same migration reason as a scan optimization; this is not the safety boundary.
3. Iterate a stable sort of current heads by `workflow_id`.
4. Call `_refresh_funding_workflow_card` inside a per-workflow `try`.
5. Route exceptions through `_log_card_failure` and continue.

Every Phase 3b workflow-card mutation path activated by Task 9 must cross this shared migration-aware method. Callers must not project or call `FundingWorkflowCardDelivery.sync` directly.

Do not insert the sweep into `poll_once`. Do not add any production runtime invocation in this task. Direct helper tests intentionally prove future capability, including generation-1 delivery and legacy-failed retry behavior, without creating a second deployed network owner.

- [ ] **Step 6: Run operator, migration, and lifecycle regressions**

Run:

```bash
.venv/bin/pytest tests/test_telegram_monthly_funding_workflow.py tests/test_migration_runtime_gates.py tests/test_telegram_operator_ui.py tests/test_telegram_approval_card.py tests/test_funding_workflow_resume.py -q
```

Expected: PASS. Direct helper calls share one stand-down fence, `poll_once` remains unchanged, and existing recovery/convergence plus request-scoped delivery ownership remain intact.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_telegram_monthly_funding_workflow.py
git commit -m "feat(3b): prepare monthly workflow card synchronization"
```

### Task 8: Prepare the Immediate-Refresh Integration Seam

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Modify: `tests/test_telegram_monthly_funding_workflow.py`

**Interfaces:**
- Consumes: Task 7's migration-aware `_refresh_funding_workflow_card(workflow_id)` and Task 2's predecessor admission guard.
- Produces:

```python
def TelegramOperatorCommandRouter._refresh_request_workflow_card(
    self,
    request: Mapping[str, Any],
) -> FundingWorkflowCardSyncResult:
    return self._refresh_funding_workflow_card(workflow_id_from_request(request))
```

- Safety boundary: this helper never calls the projector or delivery coordinator directly, so the immediate callback/Resume refreshes activated in Task 9 cannot bypass Task 7's `MIGRATING` / `INVALID` fence.
- Deployability boundary: no production callback, command, Resume, `finally`, child handoff, or poll path calls this helper yet. CLI and the request-scoped child lifecycle remain the sole active actionable delivery paths through this task. This prohibition covers existing confirmed/unknown/failed evidence as well as absent evidence; generation 0 does not authorize concurrent workflow-card retry.

- [ ] **Step 1: Write the failing request-to-workflow seam tests**

Parameterize valid funding and budget request payloads. Call `_refresh_request_workflow_card(request)` directly, spy on `_refresh_funding_workflow_card`, and assert it is called exactly once with `workflow_id_from_request(request)` and returns the same `FundingWorkflowCardSyncResult`. Assert no production transition callback, `/budget`, Resume, child handoff, or `poll_once` spy observes a call to `_refresh_request_workflow_card` in this preparatory binary.

Task 9 owns the live call-site matrix: funding complete/cancel, budget select/cancel, `/budget <request_id> <amount>`, funding/budget Resume, transition `finally`, and child handoff.

- [ ] **Step 2: Write the failing direct-seam migration tests**

Persist the financial completion/head state, then write the existing markers for `MIGRATING` and call `_refresh_request_workflow_card(request)` directly. Assert it crosses `_refresh_funding_workflow_card`, returns blocked, preserves durable financial truth, and creates no audience/adoption/card event or send/edit. Repeat with the existing contradictory markers that produce `INVALID`.

This verifies the seam without activating it. Task 9 repeats the same ordering through a real callback `finally` path after runtime ownership switches.

- [ ] **Step 3: Write failing ambiguity/crash-boundary integration tests**

Invoke the helper directly for current budget B unknown plus confirmed predecessor A, generic edit rejection, and edit timeout. It must use the same current-request-first adoption/no-replay rules as the direct sweep helper. If financial completion is already durable when an edit times out, it remains durable and a directly invoked subsequent sweep sends/edits no replacement. Approval handoff remains on approval cards.

- [ ] **Step 4: Run the red immediate-refresh tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_monthly_funding_workflow.py -k "workflow_card or request_refresh_seam or migration or predecessor" -q
```

Expected: FAIL because the request-to-workflow helper does not exist.

- [ ] **Step 5: Implement only the request-to-workflow helper**

Implement the exact helper in **Interfaces**. It derives only the canonical workflow ID and delegates to `_refresh_funding_workflow_card`, which performs the migration check at call time.

Do not call this helper from any production runtime path. Do not change callback/command/Resume `finally` blocks, `_deliver_child_signal_outcome`, `_send_funding_request`, `_send_budget_request`, `_send_request_card`, or `poll_once`; their atomic ownership handoff belongs to Task 9.

- [ ] **Step 6: Run focused and full funding workflow regressions**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_transitions.py tests/test_funding_workflow_head.py tests/test_telegram_monthly_funding_workflow.py tests/test_telegram_card_lifecycle.py tests/test_migration_runtime_gates.py -q
```

Expected: PASS. The seam shares migration/adoption/no-replay semantics when directly invoked, while no runtime path activates workflow-card delivery before cutover.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_telegram_monthly_funding_workflow.py
git commit -m "feat(3b): prepare monthly card immediate refresh seam"
```

### Task 9: Atomically Cut Over Actionable Card Delivery Ownership

**Files:**
- Modify: `src/maestro/execution/funding_requests.py`
- Modify: `src/maestro/execution/budget_requests.py`
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Modify: `src/maestro/cli.py`
- Modify: `tests/test_contribution_funding_requests.py`
- Modify: `tests/test_contribution_budget_requests.py`
- Modify: `tests/test_funding_workflow_resume.py`
- Modify: `tests/test_telegram_monthly_funding_workflow.py`
- Modify: `tests/test_cli_no_action_notice.py`
- Modify: `tests/test_signal_approval_handoff.py`
- Modify: `tests/test_multi_account_contributions.py`

**Interfaces:**
- Consumes: Task 1's default-0 model fields, Task 7's Operator sweep, Task 8's shared refresh helper, `FundingWorkflowCardSyncResult.outcome_for(chat_id)`, `_require_card_delivered`, and `StateStore.load_signal_package(signal_run_id)`.
- Activates in this one commit:
  - both builders explicitly pass `card_delivery_version=1`;
  - `poll_once` schedules `_sweep_funding_workflow_cards` immediately after `_sweep_lifecycle_cards` without reordering existing sweeps;
  - funding complete/cancel, budget select/cancel, `/budget`, funding/budget Resume, and transition `finally` paths call `_refresh_request_workflow_card`;
  - `_deliver_child_signal_outcome` synchronizes the workflow-scoped card and admits parent completion only through `_require_card_delivered`.
- Removes: CLI `RequestNotification`, `_NOTHING_REQUESTED`, `_send_signal_request_notifications`, `_send_signal_funding_request_notifications`, `_send_signal_budget_request_notifications`; Operator request-scoped `_send_funding_request`, `_send_budget_request`, and `_send_request_card` after their final child-handoff call sites disappear.
- Produces:

```python
def _signal_request_presence(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> tuple[bool, bool]:
    """Return (funding_requests_exist, budget_requests_exist)."""
```

- Ownership invariant: this is the **delivery ownership cutover commit**. The same commit makes every newly built funding/budget request generation 1, removes CLI actionable network delivery, removes request-scoped child delivery/retry, and activates workflow-scoped poll, immediate-refresh, and child-handoff delivery. There is no binary where request-scoped actionable retry and workflow-scoped actionable retry are both live for one funding/budget request. It must never be split into separately deployable commits.
- Reporting contract: emit `funding_required` and/or `budget_required` from durable request existence; emit neither `request_delivery_failed` nor actionable-card delivery claims. Daily summary and no-action informational notifications remain CLI-owned.
- Poll order after this cutover:

```text
_sweep_pending_approvals
_sweep_recovery_notifications
_sweep_lifecycle_cards
_sweep_funding_workflow_cards
_sweep_incomplete_workflows
_converge_workflow_invariants
```

- [ ] **Step 1: Write the failing atomic ownership proof**

In `tests/test_signal_approval_handoff.py`, use one shared temporary StateStore/config and the existing daily-signal fixture to produce a real funding or budget request. Patch only the informational summary sender; patch the actionable `TelegramBotAPIClient.send_message` boundary to fail the test if `_run_daily_signal_approval` invokes it. Then assert all three facts in one test:

```python
request = persisted_signal["funding_requests"][0]
assert request["card_delivery_version"] == 1
assert actionable_cli_calls == []

client = FakeTelegramClient()
router = TelegramOperatorCommandRouter(
    config=signal_maestro_config,
    store=store,
    audit=AuditLogger(signal_maestro_config.audit.jsonl_path),
    client=client,
)
router.poll_once()
assert client.sent_messages[0]["reply_markup"] is not None
assert load_card_delivery_state(store, funding_workflow_card_key(workflow_id))
```

Parameterize the test for `funding` and `budget`. Assert `poll_once` schedules the new sweep after `_sweep_lifecycle_cards` and before `_sweep_incomplete_workflows`, and the first actionable network intent/send belongs to `funding-workflow:<workflow_id>`, not a request-scoped CLI call. Retain the Task 7 version-0/missing-field direct-helper regression: absent lifecycle state is not permission to send.

- [ ] **Step 2: Write failing builder and child-handoff cutover tests**

Change Task 1's real-builder assertions from 0 to 1 and assert the persisted signal package contains explicit 1 for both funding and budget builders. Using the existing `_stub_child_signal` fixture, prove budget B is published as head while funding A is incomplete, `_deliver_child_signal_outcome` calls `_refresh_request_workflow_card(B)`, `_require_card_delivered` accepts only `sent`/`edited`/`skipped`, A completes only after confirmed delivery, and the same physical workflow message becomes actionable after A completion. Explicit rejection remains retryable; timeout remains unknown/no replay; either unconfirmed outcome keeps A incomplete.

- [ ] **Step 3: Write the failing legacy-failed single-owner regression**

In `tests/test_funding_workflow_resume.py`, publish budget B as funding A's legitimate successor and seed request-scoped legacy evidence with `card_intent_event` followed by `card_failure_event` under `budget-request:<B>`. Record the pre-cutover request-scoped intent count, then run the ownership-cutover router:

1. Invoke the activated workflow synchronization and assert it adopts the failed copy into `funding-workflow:<workflow_id>` with provenance, retries once, and records a confirmed workflow-scoped result.
2. Assert the fake Telegram client has exactly one successful `send_message` for B and that the workflow-rendered B card is buttonless while predecessor A is incomplete.
3. Continue the real parent Resume/transition path that previously re-entered `_send_budget_request`.
4. Assert `hasattr(router, "_send_budget_request")`, `hasattr(router, "_send_funding_request")`, and `hasattr(router, "_send_request_card")` are all false.
5. Assert the number of `budget-request:<B>` send-intent events is unchanged from the seeded count; no request-scoped retry was created.
6. Assert the Resume/continuation durably completes A and its activated `finally` refresh edits the same workflow-scoped physical message into the sole live actionable B representation with callbacks carrying B's exact `request_id`; there is still only one successful B `send_message`.

This test is distinct from Task 6's coordinator unit test: it exercises the post-cutover parent continuation and proves the removed owner cannot retry the same proven non-delivery under a second key. Preserve Task 6 coverage for confirmed, unknown, failed, current-unknown dominance, and per-chat independence.

- [ ] **Step 4: Write failing live immediate-refresh and migration tests**

Spy on `_refresh_request_workflow_card` through real funding complete/cancel callbacks, budget select/cancel callbacks, `/budget <request_id> <amount>`, funding/budget Resume, child handoff, and a transition exception after claim. Assert every live call delegates through `_refresh_funding_workflow_card`; no live caller invokes `project_funding_workflow_card` or `FundingWorkflowCardDelivery.sync` directly.

Add the deterministic migration race through a real callback `finally`: wrap `complete_workflow` so it persists financial completion, then writes the existing markers for `MIGRATING`, and returns. Assert the activated immediate refresh is blocked before projection/adoption/audience/intent/edit/send while financial completion/head truth remains durable. Repeat with the existing markers for `INVALID`.

- [ ] **Step 5: Write failing CLI ownership/reporting tests**

Replace notifier-result fixtures with persisted `funding_requests` / `budget_requests`. Assert funding prevents `no_action`, budget prevents `no_action`, both emit both status lines, no request preserves the existing informational `NO_ACTION_NOTICE`, daily summary remains unchanged, and no output contains `request_delivery_failed`, `telegram_funding_request=`, or `telegram_budget_request=`. Assert no actionable funding/budget formatter/markup/client path is called by daily approval.

- [ ] **Step 6: Run the red cutover tests**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py tests/test_cli_no_action_notice.py tests/test_funding_workflow_resume.py tests/test_telegram_monthly_funding_workflow.py -k "card_delivery_version or ownership or request or child_card or no_action or funding or budget or legacy_failed or immediate_refresh or migration or poll_order" -q
```

Expected: FAIL because builders still emit 0, the workflow sweep/live refresh are not scheduled, CLI still sends actionable cards, child handoff still uses request-scoped delivery, and the old request-scoped retry methods still exist.

- [ ] **Step 7: Activate generation 1 and every workflow-card runtime path**

Add `card_delivery_version=1` to both builder return statements. Insert `_sweep_funding_workflow_cards` immediately after `_sweep_lifecycle_cards` in `poll_once`; preserve every other sweep's relative order. A convergence repair at the end of one poll appears on the next poll, which remains safe because the workflow-card sweep writes no financial state and rejects malformed authority.

Once each request payload is validated, wire funding complete/cancel, budget select/cancel, `/budget`, and funding/budget Resume so their `finally` path calls `_refresh_request_workflow_card(request)` after success, claim refusal, or transition failure. Log/isolate UI refresh exceptions with `_log_card_failure`; a Telegram failure never reverses durable financial completion. The best-effort legacy callback edit touches only that callback's own chat/message before canonical refresh.

In `_deliver_child_signal_outcome`, replace each still-current funding/budget request send with:

```python
result = self._refresh_request_workflow_card(request)
self._require_card_delivered(result.outcome_for(chat_id), phase, request)
```

Remove `_send_funding_request`, `_send_budget_request`, and `_send_request_card` plus their now-unused raw formatter/markup imports. Keep `_request_still_needs_a_card`, `_require_card_delivered`, and the undelivered event/notice because they fence parent completion. `_require_card_delivered` accepts `sent`, `edited`, or `skipped`; `failed`, `unknown`, and `blocked` record `funding_request_card_undelivered` and keep the parent incomplete.

- [ ] **Step 8: Retire CLI actionable sends and classify persisted requests**

Delete the five CLI symbols listed in **Removes** and their CLI-only funding/budget formatter/markup imports. Keep `TelegramBotAPIClient` for daily summary, no-action, and failure informational messages. Implement `_signal_request_presence` by opening the same StateStore and returning:

```python
return (
    bool(signal.get("funding_requests")),
    bool(signal.get("budget_requests")),
)
```

Emit each applicable requirement line independently and call `_send_no_action_notice` only when both values are false. Apply Steps 7 and 8 before committing; there is no permitted intermediate commit between builder activation, runtime workflow activation, request-scoped sender removal, and CLI sender retirement.

- [ ] **Step 9: Run focused and adjacent cutover regressions**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py tests/test_cli_no_action_notice.py tests/test_funding_workflow_resume.py tests/test_telegram_monthly_funding_workflow.py tests/test_operator_deployment_wiring.py tests/test_telegram_card_lifecycle.py -q
```

Expected: PASS. New requests truthfully advertise generation 1 only in the binary where workflow poll/immediate/child delivery is live and both CLI raw delivery and request-scoped child retry are gone. Legacy failed evidence can be retried only under the workflow key.

- [ ] **Step 10: Audit and commit the indivisible cutover**

Before staging, run:

```bash
rg -n "card_delivery_version=1|_sweep_funding_workflow_cards|_refresh_request_workflow_card|_send_signal_request_notifications|_send_signal_funding_request_notifications|_send_signal_budget_request_notifications|def _send_funding_request|def _send_budget_request|def _send_request_card" src tests
```

Expected: explicit generation-1 writes appear only in the two builders; `poll_once` and the enumerated transition paths call only the shared helpers; removed CLI/request-scoped sender definitions and call sites are absent; tests contain only assertions/negative checks for removed names. Then commit every cutover file together:

```bash
git add src/maestro/execution/funding_requests.py src/maestro/execution/budget_requests.py src/maestro/integrations/telegram/handlers.py src/maestro/cli.py tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_funding_workflow_resume.py tests/test_telegram_monthly_funding_workflow.py tests/test_cli_no_action_notice.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py
git commit -m "feat(3b): cut over monthly card delivery ownership"
```

### Task 10: Synchronize Documentation and Verify the Complete Phase 3b Boundary

**Files:**
- Modify: `README.md`
- Modify: `docs/TRD.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/TASKS.md`
- Modify: `docs/operator_runbook.md`
- Modify: `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` (implementation-status sentence only)

**Interfaces:**
- Consumes: The completed code/tests from Tasks 1-9 and canonical B-1.1 through B-1.10.
- Produces: Accurate operator/developer documentation and a verified spec-coverage audit; no source/test behavior is introduced in this task.

- [ ] **Step 1: Update exact documentation claims**

Make these concrete edits:

- `README.md`: state that actionable funding/budget cards are workflow-scoped, lifecycle-managed, and sent only by Telegram Operator; daily CLI still owns informational summary/no-action messages.
- `docs/TRD.md`: add `ui/funding_workflow.py` and `ui/funding_workflow_delivery.py` to the Telegram component boundary; document authoritative event projection, request-scoped financial callbacks, predecessor admission, and existing card-state persistence reuse.
- `docs/ROADMAP.md`: move Phase 3a-3 through 3a-5 and Phase 3b into delivered GitHub engineering; retain Phase 4b and Phase 5 as remaining; explicitly state that this does not assert VPS migration/deployment completion.
- `docs/TASKS.md`: add a checked Phase 3b item naming generation cutover, workflow projection, adoption, strict unknown policy, operator sweep, successor admission, and CLI sender retirement.
- `docs/operator_runbook.md`: replace request-scoped examples with workflow-scoped keys where applicable; explain current-active-request unknown dominance, per-chat independence, no replay, health inspection, and that no clear/reset/force-send command exists. Do not instruct the operator to infer absence from missing evidence.
- Canonical spec: update only the top status/next-step sentence to say Phase 3b GitHub implementation is complete after all verification passes. Leave B-1 text unchanged.

- [ ] **Step 2: Run the focused Phase 3b suite**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_funding_workflow_card_projection.py tests/test_funding_workflow_card_delivery.py tests/test_telegram_ui_cards.py tests/test_telegram_card_lifecycle.py tests/test_telegram_monthly_funding_workflow.py tests/test_funding_workflow_resume.py tests/test_cli_no_action_notice.py tests/test_signal_approval_handoff.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader Telegram/workflow/migration regressions**

Run:

```bash
.venv/bin/pytest tests/test_telegram_card_state.py tests/test_telegram_card_state_migration.py tests/test_telegram_approval_card.py tests/test_telegram_approval_resume.py tests/test_telegram_dispatch_resume.py tests/test_telegram_operator_ui.py tests/test_funding_workflow_transitions.py tests/test_funding_workflow_head.py tests/test_authoritative_funding_state.py tests/test_migration_state.py tests/test_migration_runtime_gates.py tests/test_upgrade_backfill_heads.py tests/test_rollback_preflight.py tests/test_health_cli.py -q
```

Expected: PASS.

- [ ] **Step 4: Run full repository verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: all commands exit 0. Do not write an expected test count into release notes; use the count printed by this run as the current evidence.

- [ ] **Step 5: Perform the B-1.1 through B-1.10 coverage audit**

Record the audit in the commit message preparation notes and verify this mapping against code/tests:

| Spec requirement | Implemented/tested by |
|---|---|
| B-1.1 single network owner | Tasks 7-8 prepare uninvoked migration-fenced helpers; Task 9 atomically removes CLI/request-scoped child senders and activates poll, immediate, and child workflow delivery. |
| B-1.2 delivery generation | Task 1 adds default-0 compatibility; Tasks 6-7 prove v0/v1 absence semantics; Task 9 is the first and only builder activation to explicit 1. |
| B-1.3 legacy adoption | Task 6 precedence, provenance, lineage, audience, and per-chat tests. |
| B-1.4 logical/action identity | Tasks 3-4 key/callback tests; Tasks 2 and 9 stale/live callback coverage. |
| B-1.5 truthful stages/admission | Tasks 2-4 stage matrix, recovery-owned attention overlay, terminal copy, ordinary in-progress/no-warning cases, and pre-claim rejection tests. |
| B-1.6 read model/sweep boundary | Task 3 projects; Tasks 7-8 directly test uninvoked sweep/refresh seams; Task 9 schedules the sweep and activates every immediate/child caller through the same boundary. |
| B-1.7 delivery failures | Tasks 5-6 send/edit rejection, timeout, no-replay, and target-absence tests; Task 9 proves a legacy failed copy has only one post-cutover retry owner. |
| B-1.8 CLI retirement | Task 9 atomic cutover, durable request reporting, and informational notification preservation. |
| B-1.9 migration stand-down | Tasks 7-8 directly test MIGRATING/INVALID at the shared fence; Task 9 repeats the post-financial/pre-refresh race through activated callback wiring. |
| B-1.10 scope | Global Constraints, Tasks 1-9, and the no-Phase-3a-authority regression commands. |

Explicitly confirm during this audit:

- No Task changed head/CAS, claim fencing, child lineage, dual-write, rollback preflight, or recovery ownership.
- Telegram UI state contains no persisted active request or financial terminal truth.
- Current-request unknown cannot create a second actionable representation.
- Funding predecessor incomplete + budget head cannot enter a budget transition through a stale callback.
- One chat's ambiguity does not suppress an independent safe chat.
- Only Telegram Operator can send actionable funding/budget cards.
- No commit before Task 9 writes generation 1 or schedules/invokes workflow-card synchronization from a production runtime path.
- Task 9 removes every CLI/request-scoped sender in the same commit that activates both builders, poll scheduling, immediate refresh, and workflow-scoped child handoff.
- Generation 0 plus a legacy `failed` copy is acknowledged as retryable; the Task 9 single-owner regression proves parent continuation creates no second request-scoped intent/send after workflow retry succeeds.
- Every periodic or immediate workflow-card mutation crosses `_refresh_funding_workflow_card` and its migration fence before projection, adoption, audience pinning, send, or edit.
- Ordinary open claims have truthful in-progress stages without warning; `incomplete_transition` requires exact-attempt evidence already emitted by the existing recovery path.
- Production/VPS migration and deployment remain separate work.

- [ ] **Step 6: Inspect the final diff for surgical scope**

Run:

```bash
git status --short
git diff --stat
git diff -- src/maestro/state/funding_workflow.py src/maestro/integrations/telegram/handlers.py src/maestro/cli.py
```

Expected: every changed source line traces to B-1; no production config, systemd unit, test fixture database, generated dashboard asset, or unrelated cleanup appears.

- [ ] **Step 7: Commit documentation after verification**

```bash
git add README.md docs/TRD.md docs/ROADMAP.md docs/TASKS.md docs/operator_runbook.md docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md
git commit -m "docs(3b): document monthly funding workflow cards"
```

Do not deploy, touch the production VPS, run production migrations, remove legacy dual-write, or begin Phase 3a-6/4b/5 after this commit.
