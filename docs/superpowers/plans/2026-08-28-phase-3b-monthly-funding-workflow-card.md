# Phase 3b Monthly Funding Workflow Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Telegram Operator the sole actionable funding/budget card sender and project each durable monthly funding workflow into one truthful, lifecycle-managed, workflow-scoped card without changing Phase 3a financial authority.

**Architecture:** Durable `funding_workflow_head`, request, supersession, claim, completion, and migration events remain the only financial authority. A new ephemeral `FundingWorkflowCardModel` is rebuilt from those events, rendered by the existing pure Telegram UI layer, and synchronized per chat through the existing intent-first lifecycle projection; legacy request-card copies are adopted through a provenance-bearing card event, never represented as a new send. Both the periodic sweep and immediate transition refresh call the same projection/synchronization service, while a separate pre-claim admission helper prevents a budget successor from acting before its funding predecessor is durably complete.

**Tech Stack:** Python >=3.11, Pydantic >=2.0, SQLite-backed `StateStore`, Telegram Bot API client, pytest >=7.0, Ruff >=0.8.0.

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md`, canonical section `B-1. Phase 3b: 월간 워크플로우 카드 아키텍처`, including revision 16 corrections.

## Global Constraints

- Plan and implement against repository baseline `c298861f6cce2981b699cc9668f576875534f8cd` or the actual newer `main` after inspecting every intervening commit. Never reset or discard unrelated work.
- This is GitHub/development-repository work only. Production VPS deployment, production migration state, and production live-order incident remediation remain separate and must not be inferred from GitHub completion.
- Telegram Operator is the only network owner for actionable funding/budget cards. Do not preserve dual CLI/operator sending and do not add a cross-process Telegram delivery lease or CAS.
- `card_delivery_version` is Telegram delivery provenance only: missing/default `0` is legacy/raw-send generation and explicit `1` is lifecycle-owned generation. It never enters `funding_workflow_head.version`, head CAS, or financial claim logic.
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
- `list_incomplete_workflows(store)` reports claimed-but-uncompleted transitions for the attention overlay and the existing recovery UI. The monthly sweep must never invoke the recovery action.
- `StateStore.record_card_event`, `load_card_delivery_state`, `record_card_audience`, and `load_card_audience` already provide atomic event/projection persistence and per-card audience pinning. Adoption fits this event model; no new table is justified.
- `CardLifecycleManager.refresh` already preserves send ambiguity per chat, skips confirmed equal renders, retries proven failed sends, and escalates unknown/repeated failures. Phase 3b needs a strict edit-replacement policy without changing the approval-card default.
- `TelegramOperatorCommandRouter.poll_once` currently runs pending approvals, recovery notifications, approval lifecycle cards, incomplete workflow notices, and workflow convergence in that order. The monthly-card sweep can run immediately after the existing lifecycle-card sweep and tolerate one poll of convergence lag because it is read-only, validates each head/request, and isolates malformed workflows.
- `_confirm_budget_request` and `_cancel_budget_request` are the smallest common pre-claim budget transition entries: button callbacks, `/budget`, and workflow Resume all reuse them.
- `_deliver_child_signal_outcome` is the existing pre-completion handoff boundary. Phase 3b must replace its request-scoped send with workflow-card synchronization while preserving `_require_card_delivered`.
- `_run_daily_signal_approval` can classify funding/budget requirements from `StateStore.load_signal_package(signal_run_id)`; it no longer needs `RequestNotification` or any request-card network sender.

## Implementation File Map

| File | Responsibility in Phase 3b |
|---|---|
| `src/maestro/execution/funding_requests.py` | Backward-compatible funding delivery generation field; new builder stamps generation 1. |
| `src/maestro/execution/budget_requests.py` | Backward-compatible budget delivery generation field; new builder stamps generation 1. |
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

---

### Task 1: Cut Over Request Delivery Generation

**Files:**
- Create: `tests/test_contribution_funding_requests.py`
- Modify: `src/maestro/execution/funding_requests.py`
- Modify: `src/maestro/execution/budget_requests.py`
- Modify: `tests/test_contribution_budget_requests.py`
- Modify: `tests/test_signal_approval_handoff.py`
- Modify: `tests/test_multi_account_contributions.py`

**Interfaces:**
- Consumes: Existing `ContributionFundingRequest`, `ContributionBudgetRequest`, `build_contribution_funding_request(...)`, and `build_contribution_budget_request(...)` APIs.
- Produces: `ContributionFundingRequest.card_delivery_version: int = 0` and `ContributionBudgetRequest.card_delivery_version: int = 0`; both builders return objects with `card_delivery_version == 1`.
- Invariant: `model_validate()` of a persisted payload missing the field yields `0`; `model_dump(mode="json")` of every newly built request persists `1`.

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

- [ ] **Step 2: Write failing new-request generation tests**

Extend the existing persisted request assertions:

```python
assert signal["funding_requests"][0]["card_delivery_version"] == 1
assert events[0]["payload"]["card_delivery_version"] == 1
```

Add the budget assertion to the existing multi-account test that produces `signal["budget_requests"]`.

- [ ] **Step 3: Run the red tests**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py::test_run_signal_persists_funding_request_when_buy_only_cash_is_below_minimum tests/test_multi_account_contributions.py -q
```

Expected: FAIL because both models lack `card_delivery_version`, and newly persisted request payloads have no generation-1 field.

- [ ] **Step 4: Add the backward-compatible fields and explicit builder stamps**

Add this field to each request model immediately before `status`:

```python
card_delivery_version: int = 0
```

Add this explicit constructor argument in both builder return statements:

```python
card_delivery_version=1,
```

Do not add the field in `plan_contribution_request`, head payloads, claims, completions, or CAS keys.

- [ ] **Step 5: Run focused and adjacent tests**

Run:

```bash
.venv/bin/pytest tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py -q
```

Expected: PASS. Existing direct model constructors continue to default to generation 0; orchestrator-built funding and budget requests persist generation 1.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/execution/funding_requests.py src/maestro/execution/budget_requests.py tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py tests/test_signal_approval_handoff.py tests/test_multi_account_contributions.py
git commit -m "feat(3b): version funding card delivery ownership"
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

**Interfaces:**
- Consumes: `StateStore.load_funding_workflow_head`, `load_request_payload`, `list_incomplete_workflows`, `funding_workflow_superseded`, `funding_workflow_claim`, `funding_workflow_completed`, and existing card-state read APIs.
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
- An incomplete current funding or budget claim has `attention == "incomplete_transition"` while preserving its truthful in-progress stage.
- The lineage tuple is current-first and follows `superseded_by` links, never event timestamps or event IDs.
- A funding completion with an approval child but no budget successor remains `funding_completed`, not `budget_completed`.

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
- `project_funding_workflow_card(...)` validates the head phase/request/scope, derives the active claim/completion, applies the stage matrix, and overlays attention without writing it.

Do not consult `contribution_funding_request_ack` or `contribution_budget_request_decision` for terminal truth.

- [ ] **Step 6: Run focused and authority regression tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_card_projection.py tests/test_authoritative_funding_state.py tests/test_funding_workflow_head.py tests/test_funding_workflow_transitions.py -q
```

Expected: PASS. No Phase 3a authority test changes.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/ui/funding_workflow.py tests/test_funding_workflow_card_projection.py
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
- Modify: `src/maestro/integrations/telegram/ui/lifecycle.py`
- Modify: `tests/test_telegram_card_lifecycle.py`
- Modify: `tests/test_telegram_approval.py`

**Interfaces:**
- Consumes: Existing `TelegramApiRejected`, `CardLifecycleManager.refresh`, and operation-id intent/result/failure events.
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

- [ ] **Step 3: Run the red lifecycle tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_approval.py tests/test_telegram_card_lifecycle.py -q
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

Expected: PASS. Strict replacement is opt-in; approval-card network behavior is unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/bot.py src/maestro/integrations/telegram/ui/lifecycle.py tests/test_telegram_card_lifecycle.py tests/test_telegram_approval.py
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

### Task 7: Add the Read-Only Operator Workflow-Card Sweep

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

- Construction: `TelegramOperatorCommandRouter.__init__` creates one `FundingWorkflowCardDelivery(self.store, self._card_manager)`.
- Poll order after this task:

```text
_sweep_pending_approvals
_sweep_recovery_notifications
_sweep_lifecycle_cards
_sweep_funding_workflow_cards       # new minimal insertion
_sweep_incomplete_workflows
_converge_workflow_invariants
```

- [ ] **Step 1: Write the failing generation and restart sweep tests**

In the new integration file, use the existing fake Telegram client/router pattern and assert:

- Version 0 head + no request-scoped/workflow-scoped lifecycle evidence -> sweep sends nothing.
- Version 1 head + no lifecycle evidence -> sweep sends exactly one workflow-scoped actionable card per configured chat.
- Constructing a new router over the same SQLite database and sweeping again does not send a duplicate; it skips or edits the confirmed physical message according to the render hash.
- The card key in projection state is exactly `funding-workflow:<canonical workflow id>`.

- [ ] **Step 2: Write the failing migration and side-effect tests**

Parameterize durable migration markers for `MIGRATING` and `INVALID`, then assert the sweep performs no send/edit and does not change card, request, head, claim, completion, cash-flow, child, or migration event counts.

For a normal migration state, monkeypatch these symbols to raise if called: `claim_workflow_attempt`, `_run_child_signal`, `complete_workflow`, cash-flow record helpers, and `converge_workflow_invariants`. The card sweep must still render/send.

- [ ] **Step 3: Write the failing malformed-isolation and ordering tests**

Create one valid workflow and one head whose request is missing or phase contradicts its payload. Assert the valid card is still processed and one failure is audited/logged. Spy on all poll sweeps and assert the exact order listed above.

- [ ] **Step 4: Run the red operator sweep tests**

Run:

```bash
.venv/bin/pytest tests/test_telegram_monthly_funding_workflow.py -q
```

Expected: FAIL because the router has no monthly workflow-card service or sweep.

- [ ] **Step 5: Implement the minimal sweep insertion**

Implement `_refresh_funding_workflow_card` as the only handler entry that projects and synchronizes a workflow. Implement `_sweep_funding_workflow_cards` as:

1. Return immediately for `_migration_block_reason()`.
2. Iterate a stable sort of current heads by `workflow_id`.
3. Call `_refresh_funding_workflow_card` inside a per-workflow `try`.
4. Route exceptions through `_log_card_failure` and continue.

Insert the sweep after `_sweep_lifecycle_cards`. Do not reorder any existing sweep. A convergence repair that occurs at the end of this poll appears on the next poll; this one-poll lag is safe because the card sweep writes no financial state and refuses malformed/missing authority rather than inventing it.

- [ ] **Step 6: Run operator, migration, and lifecycle regressions**

Run:

```bash
.venv/bin/pytest tests/test_telegram_monthly_funding_workflow.py tests/test_migration_runtime_gates.py tests/test_telegram_operator_ui.py tests/test_telegram_approval_card.py tests/test_funding_workflow_resume.py -q
```

Expected: PASS. Existing recovery and convergence ownership remains intact.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_telegram_monthly_funding_workflow.py
git commit -m "feat(3b): sweep monthly funding workflow cards"
```

### Task 8: Route Child Handoff and Immediate Refresh through the Same Projection

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Modify: `tests/test_funding_workflow_resume.py`
- Modify: `tests/test_telegram_monthly_funding_workflow.py`

**Interfaces:**
- Consumes: `_refresh_funding_workflow_card(workflow_id)`, `FundingWorkflowCardSyncResult.outcome_for(chat_id)`, `_require_card_delivered`, and Task 2 admission.
- Produces:

```python
def TelegramOperatorCommandRouter._refresh_request_workflow_card(
    self,
    request: Mapping[str, Any],
) -> FundingWorkflowCardSyncResult:
    return self._refresh_funding_workflow_card(workflow_id_from_request(request))
```

- Handoff contract: `_require_card_delivered` accepts `sent`, `edited`, or `skipped` as confirmed physical delivery for the acting chat; `failed`, `unknown`, and `blocked` record `funding_request_card_undelivered` and keep the parent transition incomplete.

- [ ] **Step 1: Write the failing pre-completion child handoff test**

Use the existing `_stub_child_signal` fixture with funding A producing budget B in the same workflow. Assert the sequence:

1. B becomes head while A is claimed but incomplete.
2. `_deliver_child_signal_outcome` synchronizes a buttonless B card with the predecessor-incomplete attention copy.
3. `_require_card_delivered` observes the acting chat's confirmed outcome.
4. A completion is then persisted.
5. Immediate refresh edits the same message into actionable budget B with callbacks carrying B's `request_id`.

Assert there is one physical message, not one A message plus a newly sent B message.

- [ ] **Step 2: Write failing immediate-refresh tests for every entry path**

Spy on `_refresh_request_workflow_card` and cover:

- Funding complete and cancel callbacks.
- Budget select and cancel callbacks.
- `/budget <request_id> <amount>`.
- Funding and budget Resume paths.
- A transition exception after claim: refresh still runs and projects the truthful in-progress/attention state.

The best-effort `_edit_callback_message` may remain for the clicked raw legacy message, but it must run before the canonical refresh and must never derive canonical stage or route a stale request to the head.

- [ ] **Step 3: Write failing ambiguity/crash-boundary integration tests**

Cover:

- Confirmed predecessor A + active budget B unknown + A incomplete: immediate/sweep refresh sends no B duplicate and does not promote A; the B callback is rejected before any B claim.
- After A completion, the possibly existing original B callback can pass predecessor admission; B remains request-scoped. If B completes while its workflow card edit times out, `funding_workflow_completed` remains durable and the next sweep sends/edits no replacement.
- Explicit Telegram rejection of a child card remains retryable and keeps the parent incomplete until a confirmed delivery.
- Child-card send timeout remains unknown/no replay and keeps the parent incomplete.
- Approval child handoff still uses approval cards; the monthly renderer contains no approval action.

- [ ] **Step 4: Run the red immediate-refresh tests**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_resume.py tests/test_telegram_monthly_funding_workflow.py -k "monthly or workflow_card or predecessor or child_card" -q
```

Expected: FAIL because child outcomes still send request-scoped cards and callback/command paths do not invoke the monthly projection.

- [ ] **Step 5: Replace request-scoped child sends with workflow synchronization**

In `_deliver_child_signal_outcome`, for every still-current funding/budget request:

```python
result = self._refresh_request_workflow_card(request)
self._require_card_delivered(
    result.outcome_for(chat_id),
    phase,
    request,
)
```

Remove `_send_funding_request`, `_send_budget_request`, and `_send_request_card` after their last call site disappears. Remove their raw formatter/markup imports from `handlers.py`. Keep `_request_still_needs_a_card`, `_require_card_delivered`, and the undelivered event/notice because they protect parent completion.

- [ ] **Step 6: Add canonical immediate refresh in `finally` blocks**

Once a request payload has been validated and migration has passed, wrap each financial transition entry so `_refresh_request_workflow_card(request)` runs after success, claim refusal, or transition failure. Log/isolate UI refresh exceptions with `_log_card_failure`; never convert a completed financial transition back to incomplete because Telegram refresh failed.

For raw legacy callback-message cleanup, edit only the callback's own `chat_id/message_id`, remove buttons, and then let the canonical projector perform the final workflow-card update. Do not infer that the raw message was delivered in another chat.

- [ ] **Step 7: Run focused and full funding workflow regressions**

Run:

```bash
.venv/bin/pytest tests/test_funding_workflow_resume.py tests/test_funding_workflow_transitions.py tests/test_funding_workflow_head.py tests/test_telegram_monthly_funding_workflow.py tests/test_telegram_card_lifecycle.py -q
```

Expected: PASS. Parent handoff failure/unknown remains fail-closed; financial completion survives UI ambiguity.

- [ ] **Step 8: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_funding_workflow_resume.py tests/test_telegram_monthly_funding_workflow.py
git commit -m "feat(3b): refresh monthly cards across workflow transitions"
```

### Task 9: Retire CLI Actionable Request-Card Delivery and Report Durable Requests

**Files:**
- Modify: `src/maestro/cli.py`
- Modify: `tests/test_cli_no_action_notice.py`
- Modify: `tests/test_signal_approval_handoff.py`

**Interfaces:**
- Consumes: `StateStore.load_signal_package(signal_run_id)`.
- Removes: `RequestNotification`, `_NOTHING_REQUESTED`, `_send_signal_request_notifications`, `_send_signal_funding_request_notifications`, and `_send_signal_budget_request_notifications`.
- Produces:

```python
def _signal_request_presence(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> tuple[bool, bool]:
    """Return (funding_requests_exist, budget_requests_exist)."""
```

- Reporting contract: emit `funding_required` for durable funding requests and `budget_required` for durable budget requests; emit both when both exist. Emit neither `request_delivery_failed` nor actionable-card network delivery claims.

- [ ] **Step 1: Rewrite the no-action fixture to persist request truth**

Replace `funding_sent`/`budget_sent` notifier stubs with `funding_requests`/`budget_requests`. Have the fake `run_signal` persist a signal package containing those lists under `_Summary.signal_run_id` before returning. Keep `_send_signal_summary_notification` stubbed so the tests isolate no-action/actionable ownership.

- [ ] **Step 2: Write failing CLI ownership/reporting tests**

Assert:

- A durable funding request prevents `no_action`, emits `funding_required`, and makes no actionable Telegram client call.
- A durable budget request prevents `no_action`, emits `budget_required`, and makes no actionable Telegram client call.
- Funding and budget in the same package emit both status lines and do not return after the first.
- No request package preserves the informational `NO_ACTION_NOTICE` behavior and its per-chat at-most-once tests.
- No output contains `request_delivery_failed`, `telegram_funding_request=`, or `telegram_budget_request=`.
- The daily summary notification remains outside this ownership change.

- [ ] **Step 3: Run the red CLI tests**

Run:

```bash
.venv/bin/pytest tests/test_cli_no_action_notice.py tests/test_signal_approval_handoff.py -k "request or no_action or funding or budget" -q
```

Expected: FAIL because `_run_daily_signal_approval` still calls both actionable network senders and reports delivery outcomes.

- [ ] **Step 4: Implement persisted-request classification**

Instantiate `StateStore` with the same state path/cash arguments used by the retired helper, load the signal package, and return:

```python
return (
    bool(signal.get("funding_requests")),
    bool(signal.get("budget_requests")),
)
```

In `_run_daily_signal_approval`, emit each applicable requirement line independently. Only call `_send_no_action_notice` when both booleans are false.

- [ ] **Step 5: Remove actionable sender code and imports**

Delete the five removed symbols and the CLI-only imports of funding/budget raw formatters/markup helpers. Do not remove `TelegramBotAPIClient`; daily summary, no-action, and failure informational notifications still use it.

- [ ] **Step 6: Run CLI and signal-package regressions**

Run:

```bash
.venv/bin/pytest tests/test_cli_no_action_notice.py tests/test_signal_approval_handoff.py tests/test_operator_deployment_wiring.py tests/test_multi_account_contributions.py -q
```

Expected: PASS. Durable request existence, not Telegram delivery, determines the daily classification.

- [ ] **Step 7: Commit**

```bash
git add src/maestro/cli.py tests/test_cli_no_action_notice.py tests/test_signal_approval_handoff.py
git commit -m "refactor(3b): retire cli request card delivery"
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
| B-1.1 single network owner | Tasks 7-9; CLI has no actionable sender and Operator sweep/child handoff owns delivery. |
| B-1.2 delivery generation | Task 1 model/builder tests; Tasks 6-7 v0/v1 absent-state tests. |
| B-1.3 legacy adoption | Task 6 precedence, provenance, lineage, audience, and per-chat tests. |
| B-1.4 logical/action identity | Tasks 3-4 key/callback tests; Task 8 stale callback coverage. |
| B-1.5 truthful stages/admission | Tasks 2-4 stage matrix, attention, terminal copy, and pre-claim rejection tests. |
| B-1.6 read model/sweep boundary | Tasks 3, 7, and 8 restart, no-side-effect, malformed isolation, and shared refresh tests. |
| B-1.7 delivery failures | Tasks 5-6 send/edit rejection, timeout, no-replay, and target-absence tests. |
| B-1.8 CLI retirement | Task 9 durable request reporting and informational notification preservation. |
| B-1.9 migration stand-down | Task 7 MIGRATING/INVALID tests plus unchanged callback migration regression suite. |
| B-1.10 scope | Global Constraints, Tasks 1-9, and the no-Phase-3a-authority regression commands. |

Explicitly confirm during this audit:

- No Task changed head/CAS, claim fencing, child lineage, dual-write, rollback preflight, or recovery ownership.
- Telegram UI state contains no persisted active request or financial terminal truth.
- Current-request unknown cannot create a second actionable representation.
- Funding predecessor incomplete + budget head cannot enter a budget transition through a stale callback.
- One chat's ambiguity does not suppress an independent safe chat.
- Only Telegram Operator can send actionable funding/budget cards.
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
