# Dashboard Refresh and Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Dashboard global account refresh plus signal freshness checks, and add Virtuoso sub-tabs with per-app proposal-only signal generation.

**Architecture:** Keep the Dashboard safety boundary explicit. The global refresh endpoint uses the read-only Dashboard config to perform KIS account snapshot sync and classify persisted signal freshness. Per-app signal generation uses a separate signal config and a filtered orchestrator path so a single app button runs exactly one selected strategy and never approval or execution.

**Tech Stack:** Python 3.11, FastAPI, Typer, SQLite `StateStore`, Maestro orchestrator, React 19, Vite, TypeScript.

---

## File Structure

- Modify `src/maestro/dashboard/server.py`: add optional `signal_config_path`, `POST /api/dashboard/refresh`, and `POST /api/dashboard/virtuoso/{strategy_id}/generate-signal`.
- Create `src/maestro/dashboard/actions.py`: focused Dashboard action helpers for account sync, signal freshness, and per-app signal generation.
- Modify `src/maestro/orchestration/orchestrator.py`: add filtered `run_signal(strategy_ids=...)` support without changing all-strategy default behavior.
- Modify `src/maestro/cli.py`: add `--signal-config` to `maestro dashboard` and pass it into the server.
- Modify `src/maestro/dashboard/snapshot.py` and `src/maestro/dashboard/read_models.py`: expose per-strategy signal freshness in the existing Dashboard snapshot.
- Modify `dashboard_frontend_next/src/api/snapshot.ts`: add API helpers for refresh and per-app generate signal.
- Modify `dashboard_frontend_next/src/App.tsx`: add refresh action state and Virtuoso sub-tab state.
- Modify `dashboard_frontend_next/src/components/layout/TopBar.tsx`: wire heavier refresh copy/status.
- Modify `dashboard_frontend_next/src/components/tabs/VirtuosoReport.tsx`: add Overview/app sub-tabs and per-app Generate Signal button.
- Modify `dashboard_frontend_next/src/types.ts`: add action response and signal freshness fields.
- Test `tests/test_dashboard_server.py`: API behavior, no approval/order execution, missing signal config.
- Test `tests/test_signal_approval_handoff.py`: filtered `run_signal(strategy_ids=...)` runs only requested app.
- Test `tests/test_dashboard_app_helpers.py`: freshness helper classification if implemented as exported Python helper.

---

### Task 1: Add Filtered Orchestrator Signal Runs

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py`
- Test: `tests/test_signal_approval_handoff.py`

- [ ] **Step 1: Add failing test for one-strategy signal filtering**

Append this test near `test_signal_false_strategy_is_not_loaded` in `tests/test_signal_approval_handoff.py`:

```python
def test_run_signal_can_filter_to_one_strategy(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    second_strategy = config.strategies[0].model_copy(
        update={
            "id": "second_static",
            "weight": 1.0,
            "config": {"allocations": {"MOCK_ETF_B": 1.0}},
        }
    )
    config.strategies.append(second_strategy)

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["second_static"])

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    strategy_runs = store.list_strategy_runs(limit=10)
    assert summary.loaded_strategies == ["second_static"]
    assert signal["loaded_strategies"] == ["second_static"]
    assert [row["strategy_id"] for row in strategy_runs] == ["second_static"]
    assert signal["portfolio_target"]["source_strategy_ids"] == ["second_static"]
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_signal_approval_handoff.py::test_run_signal_can_filter_to_one_strategy -q
```

Expected: FAIL with `TypeError` because `run_signal()` does not accept `strategy_ids`.

- [ ] **Step 3: Implement minimal filtering**

In `src/maestro/orchestration/orchestrator.py`, change the public and locked signatures:

```python
def run_signal(self, strategy_ids: list[str] | None = None) -> SignalRunSummary:
    with self.state_store.writer_lock("run_signal"):
        return self._run_signal_locked(strategy_ids=strategy_ids)

def _run_signal_locked(self, *, strategy_ids: list[str] | None = None) -> SignalRunSummary:
```

At the start of `_run_signal_locked`, after `signal_run_id` and `current_state` are created, add:

```python
selected_strategy_ids = set(strategy_ids or [])
if selected_strategy_ids:
    unknown = selected_strategy_ids - self.registry.strategy_ids
    if unknown:
        raise ValueError("Unknown or disabled signal strategy id(s): " + ", ".join(sorted(unknown)))
```

Replace the existing call:

```python
valid_results, data_requests_by_strategy, data_quality_issues, prices = (
    self._collect_strategy_results(signal_run_id, current_state)
)
```

with:

```python
valid_results, data_requests_by_strategy, data_quality_issues, prices = (
    self._collect_strategy_results(
        signal_run_id,
        current_state,
        strategy_ids=selected_strategy_ids or None,
    )
)
```

Change `_collect_strategy_results` signature:

```python
def _collect_strategy_results(
    self,
    run_id: str,
    current_state: PortfolioState,
    *,
    strategy_ids: set[str] | None = None,
) -> tuple[
```

Inside `_collect_strategy_results`, before the loop, replace:

```python
strategy_ids=self.registry.strategy_ids,
```

with:

```python
validator_strategy_ids = strategy_ids or self.registry.strategy_ids
```

and pass:

```python
strategy_ids=validator_strategy_ids,
```

Finally, change the loop:

```python
for loaded in self.registry.strategies:
```

to:

```python
for loaded in self.registry.strategies:
    if strategy_ids is not None and loaded.config.id not in strategy_ids:
        continue
```

- [ ] **Step 4: Run focused signal tests**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_signal_approval_handoff.py::test_run_signal_can_filter_to_one_strategy tests/test_signal_approval_handoff.py::test_signal_false_strategy_is_not_loaded -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_signal_approval_handoff.py
git commit -m "feat(dashboard): support filtered signal runs"
```

---

### Task 2: Add Dashboard Action Helpers

**Files:**
- Create: `src/maestro/dashboard/actions.py`
- Test: `tests/test_dashboard_server.py`

- [ ] **Step 1: Add failing server tests for refresh and missing signal config**

Append to `tests/test_dashboard_server.py`:

```python
def test_dashboard_refresh_syncs_accounts_without_running_strategies(monkeypatch, tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    calls = []

    class FakeSnapshot:
        class Account:
            account_id = "acct_fake"
            cash = 12.0
            buying_power = 12.0
            positions = []
            total_value = 12.0

        account = Account()

    def fake_fetch(self, symbols):
        calls.append(list(symbols))
        return FakeSnapshot()

    monkeypatch.setattr(
        "maestro.execution.brokers.kis.service.KISReadOnlyService.fetch_and_store_snapshot",
        fake_fetch,
    )
    raw["mode"] = "live_readonly"
    raw["state"]["sqlite_path"] = str(tmp_path / "readonly_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "readonly_audit.jsonl")
    raw["portfolio"].pop("initial_cash", None)
    raw["accounts"] = [
        {
            "id": "kis_paper",
            "broker": "kis",
            "environment": "paper_trading",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK",
        }
    ]
    raw["strategies"] = []
    readonly_path = tmp_path / "readonly.yaml"
    readonly_path.write_text(yaml.safe_dump(raw))
    client = TestClient(create_app(readonly_path))

    response = client.post("/api/dashboard/refresh")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["accounts_synced"] == 1
    assert payload["signal_freshness"]["overall"] in {"missing", "fresh", "stale", "failed"}
    assert calls


def test_generate_signal_requires_signal_config(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.post("/api/dashboard/virtuoso/sample_static_allocation/generate-signal")

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "missing_signal_config"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py::test_dashboard_refresh_syncs_accounts_without_running_strategies tests/test_dashboard_server.py::test_generate_signal_requires_signal_config -q
```

Expected: FAIL because endpoints do not exist.

- [ ] **Step 3: Create `dashboard/actions.py`**

Create `src/maestro/dashboard/actions.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from maestro.config.loader import load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


@dataclass(frozen=True)
class DashboardRefreshResult:
    status: str
    accounts_synced: int
    signal_freshness: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accounts_synced": self.accounts_synced,
            "signal_freshness": self.signal_freshness,
        }


def refresh_dashboard_state(config_path: str | Path) -> DashboardRefreshResult:
    config, identity = load_config_with_identity(config_path)
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )
    audit = AuditLogger(config.audit.jsonl_path)
    accounts_synced = 0
    if config.mode in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        for logical_account_id, kis_config in _kis_accounts(config):
            service = KISReadOnlyService(
                kis_config,
                store,
                audit,
                instruments=config.universe.instruments,
                logical_account_id=logical_account_id,
            )
            service.fetch_and_store_snapshot(config.portfolio.allowed_symbols)
            accounts_synced += 1
    return DashboardRefreshResult(
        status="ok",
        accounts_synced=accounts_synced,
        signal_freshness=build_signal_freshness(store, max_age_seconds=config.approval.signal_max_age_seconds),
    )


def generate_strategy_signal(config_path: str | Path, strategy_id: str) -> dict[str, Any]:
    config, identity = load_config_with_identity(config_path)
    matching = [strategy for strategy in config.strategies if strategy.id == strategy_id]
    if not matching:
        raise ValueError(f"Unknown strategy_id: {strategy_id}")
    strategy = matching[0]
    if not strategy.enabled or not strategy.signal_enabled:
        raise ValueError(f"Strategy is not signal-enabled: {strategy_id}")
    summary = MaestroOrchestrator(config, config_identity=identity).run_signal(
        strategy_ids=[strategy_id],
    )
    return {
        "status": "ok",
        "strategy_id": strategy_id,
        "signal_run_id": summary.signal_run_id,
        "loaded_strategies": summary.loaded_strategies,
        "action_required": summary.action_required,
        "orders_preview_count": summary.orders_preview_count,
    }


def build_signal_freshness(store: StateStore, *, max_age_seconds: int) -> dict[str, Any]:
    rows = store.list_system_events_by_type("signal_package", limit=20)
    if not rows:
        return {"overall": "missing", "strategies": []}
    now = utc_now()
    strategies = []
    for row in rows:
        payload = row.get("payload") or {}
        created_at = row.get("created_at")
        age_seconds = _age_seconds(created_at, now)
        status = "fresh" if age_seconds is not None and age_seconds <= max_age_seconds else "stale"
        if payload.get("status") == "failed":
            status = "failed"
        for strategy_id in payload.get("loaded_strategies") or []:
            if any(item["strategy_id"] == strategy_id for item in strategies):
                continue
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "status": status,
                    "latest_signal_run_id": payload.get("signal_run_id") or row.get("run_id"),
                    "latest_signal_at": created_at,
                    "age_seconds": age_seconds,
                    "max_age_seconds": max_age_seconds,
                }
            )
    overall = _overall_signal_status([item["status"] for item in strategies])
    return {"overall": overall, "strategies": strategies}


def _kis_accounts(config) -> list[tuple[str | None, Any]]:
    if config.accounts:
        return [
            (account.id, account.to_kis_config())
            for account in config.accounts
            if account.enabled and account.broker == "kis"
        ]
    if config.kis.enabled:
        return [(None, config.kis)]
    return []


def _age_seconds(created_at: Any, now) -> int | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)
    return max(0, int((now - created).total_seconds()))


def _overall_signal_status(statuses: list[str]) -> str:
    if not statuses:
        return "missing"
    for status in ("failed", "stale", "missing", "fresh"):
        if status in statuses:
            return status
    return "missing"
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py::test_dashboard_refresh_syncs_accounts_without_running_strategies tests/test_dashboard_server.py::test_generate_signal_requires_signal_config -q
```

Expected: still FAIL until server endpoints are wired in Task 3.

---

### Task 3: Wire Dashboard API and CLI Signal Config

**Files:**
- Modify: `src/maestro/dashboard/server.py`
- Modify: `src/maestro/cli.py`
- Test: `tests/test_dashboard_server.py`

- [ ] **Step 1: Extend `create_app` and server runner signatures**

In `src/maestro/dashboard/server.py`, update imports:

```python
from maestro.dashboard.actions import generate_strategy_signal, refresh_dashboard_state
```

Change:

```python
def create_app(config_path: str | Path, web_dir: str | Path | None = None):
```

to:

```python
def create_app(
    config_path: str | Path,
    web_dir: str | Path | None = None,
    signal_config_path: str | Path | None = None,
):
```

Inside `create_app`, after `resolved_config = Path(config_path)`, add:

```python
resolved_signal_config = Path(signal_config_path) if signal_config_path else None
```

- [ ] **Step 2: Add POST endpoints**

Add before the frontend catch-all routes:

```python
    @app.post("/api/dashboard/refresh")
    def refresh() -> dict[str, object]:
        try:
            return refresh_dashboard_state(resolved_config).as_payload()
        except ValueError as exc:
            raise config_state_mismatch_error(exc) from exc

    @app.post("/api/dashboard/virtuoso/{strategy_id}/generate-signal")
    def generate_signal(strategy_id: str) -> dict[str, object]:
        if resolved_signal_config is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "missing_signal_config",
                    "message": "Dashboard signal generation requires --signal-config.",
                },
            )
        try:
            return generate_strategy_signal(resolved_signal_config, strategy_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"status": "signal_generation_failed", "message": str(exc)},
            ) from exc
```

- [ ] **Step 3: Pass `signal_config_path` through runner and CLI**

In `src/maestro/dashboard/server.py`, change:

```python
def run_dashboard_server(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8503,
) -> None:
```

to:

```python
def run_dashboard_server(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8503,
    signal_config_path: str | Path | None = None,
) -> None:
```

and call:

```python
uvicorn.run(create_app(config_path, signal_config_path=signal_config_path), host=host, port=port)
```

In `main()`, add:

```python
parser.add_argument("--signal-config", default=None)
```

and pass `signal_config_path=args.signal_config`.

In `src/maestro/cli.py`, change dashboard command:

```python
signal_config: Path | None = typer.Option(None, help="Signal config for Virtuoso generate-signal actions."),
```

and call:

```python
run_dashboard_server(resolved_config, host=host, port=port, signal_config_path=signal_config)
```

- [ ] **Step 4: Run endpoint tests**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py::test_dashboard_refresh_syncs_accounts_without_running_strategies tests/test_dashboard_server.py::test_generate_signal_requires_signal_config -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/maestro/dashboard/actions.py src/maestro/dashboard/server.py src/maestro/cli.py tests/test_dashboard_server.py
git commit -m "feat(dashboard): add refresh and signal action APIs"
```

---

### Task 4: Expose Signal Freshness in Dashboard Snapshot

**Files:**
- Modify: `src/maestro/dashboard/snapshot.py`
- Modify: `src/maestro/dashboard/read_models.py`
- Test: `tests/test_dashboard_server.py`

- [ ] **Step 1: Add failing snapshot assertion**

In `test_dashboard_snapshot_includes_feature_parity_read_models`, add:

```python
    assert "signal_freshness" in payload["virtuoso_apps"]
    assert payload["virtuoso_apps"]["signal_freshness"]["overall"] in {
        "fresh",
        "stale",
        "missing",
        "failed",
    }
```

- [ ] **Step 2: Run failing test**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py::test_dashboard_snapshot_includes_feature_parity_read_models -q
```

Expected: FAIL because `signal_freshness` is absent.

- [ ] **Step 3: Add read-model wrapper**

In `src/maestro/dashboard/read_models.py`, import:

```python
from maestro.dashboard.actions import build_signal_freshness
```

Add:

```python
def build_signal_freshness_card(store: StateStore, *, max_age_seconds: int) -> dict[str, Any]:
    return build_signal_freshness(store, max_age_seconds=max_age_seconds)
```

- [ ] **Step 4: Include freshness in Virtuoso snapshot**

In `src/maestro/dashboard/snapshot.py`, import `build_signal_freshness_card`.

In the dict returned by the Virtuoso builder, add:

```python
"signal_freshness": build_signal_freshness_card(
    store,
    max_age_seconds=config.approval.signal_max_age_seconds,
),
```

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py tests/test_dashboard_read_models.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/dashboard/read_models.py src/maestro/dashboard/snapshot.py tests/test_dashboard_server.py
git commit -m "feat(dashboard): expose signal freshness read model"
```

---

### Task 5: Add Frontend Refresh Action and Virtuoso Sub-Tabs

**Files:**
- Modify: `dashboard_frontend_next/src/types.ts`
- Modify: `dashboard_frontend_next/src/api/snapshot.ts`
- Modify: `dashboard_frontend_next/src/App.tsx`
- Modify: `dashboard_frontend_next/src/components/layout/TopBar.tsx`
- Modify: `dashboard_frontend_next/src/components/tabs/VirtuosoReport.tsx`
- Modify: `dashboard_frontend_next/src/styles.css`
- Test: `npm run dashboard:check`

- [ ] **Step 1: Add frontend types**

In `dashboard_frontend_next/src/types.ts`, add:

```ts
export type SignalFreshness = {
  overall: "fresh" | "stale" | "missing" | "failed" | string;
  strategies: Array<{
    strategy_id: string;
    status: "fresh" | "stale" | "missing" | "failed" | string;
    latest_signal_run_id?: string | null;
    latest_signal_at?: string | null;
    age_seconds?: number | null;
    max_age_seconds?: number | null;
  }>;
};

export type DashboardRefreshResult = {
  status: string;
  accounts_synced: number;
  signal_freshness: SignalFreshness;
};

export type GenerateSignalResult = {
  status: string;
  strategy_id: string;
  signal_run_id: string;
  loaded_strategies: string[];
  action_required: boolean;
  orders_preview_count: number;
};
```

In `DashboardSnapshot["virtuoso_apps"]`, add:

```ts
signal_freshness: SignalFreshness;
```

- [ ] **Step 2: Add API helpers**

In `dashboard_frontend_next/src/api/snapshot.ts`, add:

```ts
import type { DashboardRefreshResult, GenerateSignalResult } from "../types";

export async function refreshDashboardState(): Promise<DashboardRefreshResult> {
  const response = await fetch("/api/dashboard/refresh", { method: "POST" });
  if (!response.ok) {
    throw new Error(await dashboardErrorMessage(response));
  }
  return (await response.json()) as DashboardRefreshResult;
}

export async function generateStrategySignal(strategyId: string): Promise<GenerateSignalResult> {
  const response = await fetch(
    `/api/dashboard/virtuoso/${encodeURIComponent(strategyId)}/generate-signal`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(await dashboardErrorMessage(response));
  }
  return (await response.json()) as GenerateSignalResult;
}
```

Export `dashboardErrorMessage` only if TypeScript needs shared access; otherwise keep it private.

- [ ] **Step 3: Wire global refresh in `App.tsx`**

Import:

```ts
import { generateStrategySignal, loadSnapshot, refreshDashboardState } from "./api/snapshot";
```

Add state:

```ts
const [refreshStatus, setRefreshStatus] = useState<string | null>(null);
const [actionError, setActionError] = useState<string | null>(null);
```

Add handler:

```ts
async function refreshAccountState() {
  setLoading(true);
  setActionError(null);
  setRefreshStatus("Refreshing account state...");
  try {
    const result = await refreshDashboardState();
    setRefreshStatus(
      `Updated ${result.accounts_synced} account${result.accounts_synced === 1 ? "" : "s"}; signal ${result.signal_freshness.overall}`,
    );
    await loadSnapshot(displayCurrency, setSnapshot, setLoading, setError);
  } catch (error) {
    setActionError(error instanceof Error ? error.message : "Unknown refresh error");
    setLoading(false);
  }
}
```

Pass `onRefresh={() => void refreshAccountState()}` to `TopBar`, and pass `refreshStatus` and `actionError` to the page or console status area.

- [ ] **Step 4: Update `TopBar` props**

In `TopBar.tsx`, add optional prop:

```ts
refreshStatus?: string | null;
```

Render below `.top-actions`:

```tsx
{refreshStatus ? <span className="top-status">{refreshStatus}</span> : null}
```

- [ ] **Step 5: Replace Virtuoso layout with sub-tabs**

In `VirtuosoReport.tsx`, add local type:

```ts
type VirtuosoSubTab = "Overview" | string;
```

Add props:

```ts
generateSignal: (strategyId: string) => Promise<void>;
generatingStrategyId: string | null;
actionError: string | null;
```

Render a sub-tab row:

```tsx
<div className="subtab-list" aria-label="Virtuoso app tabs">
  {["Overview", ...apps.strategies.map((strategy) => strategy.strategy_id)].map((tab) => (
    <button
      key={tab}
      className={tab === selectedSubTab ? "subtab active" : "subtab"}
      type="button"
      onClick={() => setSelectedSubTab(tab)}
    >
      {tab}
    </button>
  ))}
</div>
```

For `Overview`, render `ReadableTable rows={apps.overview}` and a panel for `apps.signal_freshness.strategies`.

For an app tab, render the current detail panels plus:

```tsx
<button
  className="button primary"
  type="button"
  disabled={!selected || generatingStrategyId === selected.strategy_id}
  onClick={() => selected && void generateSignal(selected.strategy_id)}
>
  {generatingStrategyId === selected?.strategy_id ? "Generating..." : "Generate Signal"}
</button>
```

- [ ] **Step 6: Add CSS for sub-tabs/status**

In `dashboard_frontend_next/src/styles.css`, add:

```css
.top-status {
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}

.subtab-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  border-bottom: 1px solid var(--hairline);
  padding-bottom: var(--space-sm);
}

.subtab {
  min-height: 34px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-pill);
  background: var(--canvas);
  color: var(--muted);
  cursor: pointer;
  padding: 7px 13px;
  font-size: 13px;
  font-weight: 600;
}

.subtab.active {
  background: var(--surface-dark);
  color: var(--on-dark);
}
```

- [ ] **Step 7: Run frontend checks**

Run:

```bash
npm run dashboard:check
npm run dashboard:build
```

Expected: both pass.

- [ ] **Step 8: Commit**

```bash
git add dashboard_frontend_next/src dashboard_frontend_next/index.html src/maestro/dashboard/web
git commit -m "feat(dashboard): add refresh actions and virtuoso subtabs"
```

---

### Task 6: Final Verification and Docs

**Files:**
- Modify: `README.md`
- Modify: `docs/operator_runbook.md`
- Modify: `docs/TRD.md`
- Modify: `docs/TASKS.md`
- Keep: `docs/superpowers/specs/2026-05-28-dashboard-refresh-signals-design.md`

- [ ] **Step 1: Ensure docs match final behavior**

Check that docs state:

```text
Refresh syncs read-only account snapshots and signal freshness.
Refresh does not run Virtuoso apps.
Virtuoso app tabs generate one selected app signal only.
Dashboard does not approve or execute orders.
```

- [ ] **Step 2: Run backend test suite slice**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/pytest tests/test_dashboard_server.py tests/test_dashboard_read_models.py tests/test_signal_approval_handoff.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run frontend verification**

Run:

```bash
npm run dashboard:check
npm run dashboard:build
```

Expected: both pass and `src/maestro/dashboard/web` contains the current built assets.

- [ ] **Step 4: Run formatting/lint checks**

Run:

```bash
/root/projects/Symphony/Maestro/.venv/bin/ruff check src/maestro/dashboard src/maestro/orchestration tests/test_dashboard_server.py tests/test_signal_approval_handoff.py
```

Expected: no ruff violations.

- [ ] **Step 5: Commit docs and verification updates**

```bash
git add README.md docs/operator_runbook.md docs/TRD.md docs/TASKS.md docs/superpowers/specs/2026-05-28-dashboard-refresh-signals-design.md
git commit -m "docs(dashboard): plan refresh and signal actions"
```

---

## Self-Review

Spec coverage:

- Global refresh account sync is covered by Tasks 2, 3, and 5.
- Signal freshness classification is covered by Tasks 2, 4, and 5.
- Virtuoso Overview and per-app sub-tabs are covered by Task 5.
- Per-app Generate Signal is covered by Tasks 1, 3, and 5.
- `Generate All Signals` remains absent by design in Task 5.
- Dashboard safety boundary is covered by Tasks 2, 3, 5, and 6.

Type consistency:

- Backend refresh payload uses `DashboardRefreshResult.as_payload()`.
- Frontend `DashboardRefreshResult` matches backend fields.
- Backend generate payload uses `GenerateSignalResult` frontend fields.
- `SignalFreshness` is nested under `snapshot.virtuoso_apps.signal_freshness`.

Verification commands:

- Backend: `pytest tests/test_dashboard_server.py tests/test_dashboard_read_models.py tests/test_signal_approval_handoff.py -q`
- Frontend: `npm run dashboard:check` and `npm run dashboard:build`
- Lint: `ruff check` on touched Python files.
