# Symphony User Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the active Maestro and Virtuoso repositories plus Maestro operator state from `/root` into the approved `/home/symphony` layout, run application services as `symphony`, and retain a verified rollback path.

**Architecture:** First serialize `StateStore` initialization and restore a green root-based production baseline. Prepare layout-dependent changes on isolated branches, build and test the new user-owned runtime without disturbing production, then stop every writer for a copy-and-cutover of SQLite state, configuration, and systemd units. Activate services in layers and keep both old trees until KR and US scheduled cycles pass.

**Tech Stack:** Python 3.11, SQLite WAL, `fcntl.flock`, pytest, Ruff, uv, Node 24/npm, React/Vite/TypeScript, systemd services/timers/path units, rsync, Git.

## Global Constraints

- Final source paths are `/home/symphony/maestro` and `/home/symphony/virtuoso/{virtuoso-tranquillo,virtuoso-crescendo,virtuoso-fugue}`.
- Final operator path is `/home/symphony/maestro-operator`; it remains outside every Git repository.
- `/etc/maestro/maestro.env` remains the secret source. Never print or commit its values.
- Application units run as `User=symphony`, `Group=symphony`, `UMask=0077`; the three dashboard restart/watch helpers remain root-run.
- The root-run source watcher executes only `/usr/local/libexec/maestro/watch_dashboard_backend.sh`, installed `root:root` mode `0755`; it never executes a user-writable home script.
- Do not copy `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, or `.ruff_cache` into new source trees.
- Do not create `/root` compatibility symlinks or delete either old root tree in this plan.
- Do not manually invoke signal or rebalance workflows during migration verification.
- Stop all Maestro writers before the final state copy. SQLite integrity must be `ok` before and after it.
- Deploy and verify the DB initialization repair on the root runtime before path cutover.
- Do not merge layout branches until they pass at the new paths. Keep the root Maestro checkout pinned at the last root-compatible commit for rollback.
- Secret-bearing commands may edit exact path prefixes but may not emit environment-file contents.
- `hermes-agent-router.service` repair remains out of scope.

**Design reference:** `docs/superpowers/specs/2026-08-14-symphony-user-migration-design.md`

---

### Task 1: Serialize `StateStore` Initialization

**Files:**
- Modify: `src/maestro/state/store.py:42-60`
- Modify: `tests/test_state_store.py:1-35`
- Modify: `tests/test_state_store.py:212-228`
- Modify: `tests/test_telegram_card_state_migration.py:1-240`

**Interfaces:**
- Consumes: `StateStore.writer_lock(owner: str, timeout_seconds: float = 10.0)` and the existing `<sqlite_path>.lock` protocol.
- Produces: every `StateStore(...)` constructor serializes `_init_db()` under writer-lock owner `init_db`.

- [ ] **Step 1: Create an isolated Maestro worktree**

Use `superpowers:using-git-worktrees` to create branch `fix/state-store-init-lock` from current Maestro `main`. Verify:

```bash
git status --short --branch
git rev-parse HEAD
```

Expected: a clean worktree containing the approved design and this plan.

- [ ] **Step 2: Add a picklable child-process helper**

Add next to `_hold_writer_lock` in `tests/test_state_store.py`:

```python
def _open_state_store(db_path, ready, result):
    ready.set()
    try:
        StateStore(db_path, 0)
    except Exception as exc:  # pragma: no cover - asserted through child result
        result.put((type(exc).__name__, str(exc)))
    else:
        result.put(None)
```

- [ ] **Step 3: Write the deterministic constructor-lock test**

Add to `tests/test_state_store.py`:

```python
def test_constructor_waits_for_the_state_writer_lock(tmp_path):
    db = str(tmp_path / "state.db")
    StateStore(db, 0)
    lock_path = Path(f"{db}.lock")
    ready = multiprocessing.Event()
    result = multiprocessing.Queue()

    with lock_path.open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = multiprocessing.Process(target=_open_state_store, args=(db, ready, result))
        proc.start()
        assert ready.wait(timeout=10)
        time.sleep(0.3)
        assert proc.is_alive()
        fcntl.flock(held, fcntl.LOCK_UN)

    proc.join(timeout=10)
    assert proc.exitcode == 0
    assert result.get(timeout=2) is None
```

- [ ] **Step 4: Run it and verify the pre-fix failure**

```bash
.venv/bin/pytest -q tests/test_state_store.py::test_constructor_waits_for_the_state_writer_lock
```

Expected before implementation: FAIL because the constructor exits while the raw writer lock is held.

- [ ] **Step 5: Add a concurrent legacy migration test**

Add `import multiprocessing` and this top-level helper to `tests/test_telegram_card_state_migration.py`:

```python
def _open_legacy_store_together(path, start, results):
    start.wait(timeout=10)
    try:
        StateStore(path, 0.0)
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put((type(exc).__name__, str(exc)))
    else:
        results.put(None)
```

Add the test:

```python
def test_concurrent_constructors_migrate_one_legacy_database(tmp_path):
    path = _old_schema_db(tmp_path)
    start = multiprocessing.Event()
    results = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_open_legacy_store_together, args=(path, start, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    assert [results.get(timeout=2) for _ in processes] == [None, None, None, None]
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_xinfo(system_events)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(system_events)")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    assert {"approval_id", "signal_run_id"} <= columns
    assert {"idx_system_events_type_approval", "idx_system_events_type_signal_run"} <= indexes
    assert integrity == "ok"
```

- [ ] **Step 6: Implement the minimal initialization lock**

Change only the constructor boundary in `src/maestro/state/store.py`:

```python
self.initial_cash = float(initial_cash or 0.0)
self.initial_cash_by_currency = dict(initial_cash_by_currency or {})
with self.writer_lock("init_db"):
    self._init_db()
```

Do not add a second lock file and do not catch duplicate-column errors.

- [ ] **Step 7: Run focused verification**

```bash
.venv/bin/pytest -q \
  tests/test_state_store.py::test_constructor_waits_for_the_state_writer_lock \
  tests/test_state_store.py::test_writer_lock_is_exclusive_across_processes \
  tests/test_telegram_card_state_migration.py
```

Expected: all selected tests pass with no hung child process.

- [ ] **Step 8: Run complete Maestro verification**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: full suite passes, Ruff reports `All checks passed!`, and diff check is silent.

- [ ] **Step 9: Commit the repair**

```bash
git add src/maestro/state/store.py tests/test_state_store.py tests/test_telegram_card_state_migration.py
git commit -m "fix(state): serialize database initialization"
```

---

### Task 2: Deploy the DB Fix and Restore a Green Root Baseline

**Files:**
- No repository file changes.
- Read: `/etc/systemd/system/maestro-*.service`
- Read: `/root/maestro-operator/var/symphony_state.db`

**Interfaces:**
- Consumes: the Task 1 commit and existing root-based units.
- Produces: root-compatible `main`, healthy FX refresh, DB integrity evidence, and a captured unit baseline.

- [ ] **Step 1: Review and integrate Task 1 only**

Review `main..fix/state-store-init-lock`, rerun the full suite, and fast-forward the root Maestro checkout. Do not include `/home/symphony` layout changes.

```bash
git diff --check main..fix/state-store-init-lock
git switch main
git merge --ff-only fix/state-store-init-lock
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Expected: fast-forward merge and green full verification.

- [ ] **Step 2: Push root-compatible `main` and verify its remote SHA**

```bash
git push origin main
git rev-parse HEAD
git rev-parse origin/main
git ls-remote origin refs/heads/main
```

Expected: all three SHA readings match.

- [ ] **Step 3: Back up and verify the current production DB**

```bash
sudo install -d -o root -g root -m 0700 /root/maestro-migration-backup
sqlite3 -readonly /root/maestro-operator/var/symphony_state.db 'PRAGMA integrity_check;'
sqlite3 /root/maestro-operator/var/symphony_state.db ".backup '/root/maestro-migration-backup/pre-user-migration-state.db'"
sudo chmod 0600 /root/maestro-migration-backup/pre-user-migration-state.db
```

Expected: integrity output is `ok` and the backup exists mode `0600`.

- [ ] **Step 4: Restart root applications serially**

```bash
sudo systemctl restart maestro-dashboard.service
sudo systemctl is-active maestro-dashboard.service
sudo systemctl restart maestro-telegram-operator.service
sudo systemctl is-active maestro-telegram-operator.service
```

Expected: both are active with no initialization exception.

- [ ] **Step 5: Re-run the previously failed FX service**

```bash
sudo systemctl reset-failed maestro-fx-refresh.service
sudo systemctl start maestro-fx-refresh.service
sudo systemctl status maestro-fx-refresh.service --no-pager
sudo journalctl -u maestro-fx-refresh.service -n 100 --no-pager
```

Expected: successful oneshot and no duplicate-column, timeout, or integrity error. Stop and investigate if it is not green.

- [ ] **Step 6: Capture the root runtime baseline without secrets**

```bash
systemctl list-unit-files 'maestro-*' --no-legend --no-pager > /root/maestro-migration-backup/unit-files.before.txt
systemctl list-units --all --type=service --type=timer --type=path 'maestro-*' --no-legend --no-pager > /root/maestro-migration-backup/units.before.txt
sqlite3 -readonly /root/maestro-operator/var/symphony_state.db 'PRAGMA integrity_check;' > /root/maestro-migration-backup/integrity.before.txt
```

Expected: no environment values are captured; integrity file contains `ok`.

---

### Task 3: Make Maestro Repository Assets Layout-Aware

**Files:**
- Modify: `pyproject.toml:39-45`
- Modify: `configs/operator/symphony_readonly.yaml:1-5`
- Modify: `configs/operator/symphony_signal.yaml:1-5`
- Modify: `configs/operator/symphony_approval.yaml:1-5`
- Modify: `deploy/systemd/*.service`
- Modify: `deploy/systemd/*.timer`
- Modify: `deploy/systemd/maestro-dashboard.path`
- Create: `deploy/systemd/maestro-book-performance.service`
- Create: `deploy/systemd/maestro-symphony-rebalance-kr.service`
- Create: `deploy/systemd/maestro-symphony-rebalance-us.service`
- Modify: `deploy/maestro-dashboard.service`
- Modify: `deploy/scripts/watch_dashboard_backend.sh`
- Modify: `scripts/operator/symphony_signal_then_approval.sh:4`
- Modify: `tests/test_operator_deployment_wiring.py:1-170`
- Modify: `README.md`
- Modify: `docs/vps_systemd.md`
- Modify: `docs/deployment.md`
- Modify: `docs/operator_runbook.md`
- Modify: `docs/multi_account_broker_routing.md`
- Modify: `docs/TRD.md`, `docs/ROADMAP.md`, `docs/TASKS.md`
- Inspect and leave unchanged with a recorded rationale: `docs/PRD.md`

**Interfaces:**
- Consumes: approved target paths and Task 1's root-compatible `main`.
- Produces: branch `feat/symphony-user-migration` with no active root path and lowercase `../virtuoso` references.

- [ ] **Step 1: Create an isolated layout worktree**

Use `superpowers:using-git-worktrees` to create `feat/symphony-user-migration` from root-compatible Maestro `main`. Do not switch the live root checkout away from `main`.

- [ ] **Step 2: Write deployment path and service-user tests first**

Extend `tests/test_operator_deployment_wiring.py`:

```python
APP_SERVICE_NAMES = {
    "maestro-book-performance.service",
    "maestro-dashboard.service",
    "maestro-fx-refresh.service",
    "maestro-heartbeat.service",
    "maestro-resume-order-tracking.service",
    "maestro-run-once.service",
    "maestro-symphony-readonly.service",
    "maestro-symphony-readonly-kr.service",
    "maestro-symphony-readonly-us.service",
    "maestro-symphony-rebalance-kr.service",
    "maestro-symphony-rebalance-us.service",
    "maestro-symphony-signal.service",
    "maestro-symphony-signal-kr.service",
    "maestro-symphony-signal-us.service",
    "maestro-telegram-operator.service",
}


def test_application_units_run_as_symphony_from_the_home_layout():
    for name in APP_SERVICE_NAMES:
        content = (SYSTEMD_DIR / name).read_text()
        assert "User=symphony" in content
        assert "Group=symphony" in content
        assert "UMask=0077" in content
        assert "/root/projects/Symphony" not in content
        assert "/root/maestro-operator" not in content


def test_deployment_assets_have_no_active_root_runtime_paths():
    paths = [
        *SYSTEMD_DIR.glob("maestro-*"),
        REPO_ROOT / "deploy/maestro-dashboard.service",
        REPO_ROOT / "deploy/scripts/watch_dashboard_backend.sh",
        REPO_ROOT / "scripts/operator/symphony_signal_then_approval.sh",
    ]
    for path in paths:
        content = path.read_text()
        assert "/root/projects/Symphony" not in content
        assert "/root/maestro-operator" not in content


def test_root_watcher_executes_the_root_owned_installed_script():
    content = (SYSTEMD_DIR / "maestro-dashboard-src-watch.service").read_text()
    assert "ExecStart=/bin/bash /usr/local/libexec/maestro/watch_dashboard_backend.sh" in content
    assert "/home/symphony/maestro/deploy/scripts" not in content
```

- [ ] **Step 3: Run the deployment tests and verify they fail**

```bash
.venv/bin/pytest -q tests/test_operator_deployment_wiring.py
```

Expected: FAIL on missing templates, `User=root`, and old absolute paths.

- [ ] **Step 4: Update Maestro-relative app paths**

Change `pyproject.toml` pytest paths to:

```toml
"../virtuoso/virtuoso-tranquillo/src",
"../virtuoso/virtuoso-crescendo/src",
```

Change the three `configs/operator/symphony_*.yaml` fragment prefixes from `../../../Virtuoso/` to `../../../virtuoso/`.

- [ ] **Step 5: Update application systemd templates**

For every name in `APP_SERVICE_NAMES`, preserve its command, timeout, and network ordering, but set:

```ini
Documentation=file:/home/symphony/maestro/docs/vps_systemd.md
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
```

Use `/home/symphony/maestro/.venv/bin/maestro` for Maestro commands. The book-performance service instead uses:

```ini
WorkingDirectory=/home/symphony/virtuoso/virtuoso-crescendo
ExecStart=/home/symphony/maestro/.venv/bin/python backtests/generate_book_performance.py --signal-config /home/symphony/maestro-operator/symphony_signal.yaml --data-dir /home/symphony/maestro-operator/var/backtest_data --output /home/symphony/maestro-operator/var/book_performance.json
```

Create the three missing repository templates from current deployed semantics; change only paths, user/group, and umask.

- [ ] **Step 6: Keep control-plane helpers root-run**

Do not add `User=symphony` to:

```text
maestro-dashboard-health.service
maestro-dashboard-reload.service
maestro-dashboard-src-watch.service
```

Update their documentation paths. Make the source watcher execute `/usr/local/libexec/maestro/watch_dashboard_backend.sh`; that repository script still watches `/home/symphony/maestro/src/maestro`. Update timer documentation and make `maestro-dashboard.path` watch:

```ini
PathModified=/home/symphony/maestro-operator/symphony_readonly.yaml
PathModified=/home/symphony/maestro-operator/symphony_signal.yaml
PathChanged=/home/symphony/maestro/src/maestro/dashboard/web/index.html
```

- [ ] **Step 7: Update scripts and operator documentation**

Set the watch target to `/home/symphony/maestro/src/maestro` and the signal wrapper default to `/home/symphony/maestro/.venv/bin/maestro`. Replace active deployment examples in README, `docs/vps_systemd.md`, `docs/deployment.md`, `docs/operator_runbook.md`, and `docs/multi_account_broker_routing.md`.

Do not rewrite dated historical design documents. Add the service-user and home-layout boundary to TRD, add the infrastructure migration item to ROADMAP/TASKS, and leave PRD unchanged because product requirements and user-visible behavior do not change.

- [ ] **Step 8: Run deployment and config verification**

```bash
.venv/bin/pytest -q tests/test_operator_deployment_wiring.py tests/test_config_validation.py
.venv/bin/ruff check tests/test_operator_deployment_wiring.py
rg -n '/root/projects/Symphony|/root/maestro-operator|User=root|\.\./\.\./\.\./Virtuoso' \
  deploy scripts/operator configs/operator pyproject.toml README.md \
  docs/vps_systemd.md docs/deployment.md docs/operator_runbook.md docs/multi_account_broker_routing.md
git diff --check
```

Expected: tests pass; `rg` returns no active reference; diff check is silent.

- [ ] **Step 9: Commit and push the Maestro layout branch**

```bash
git add pyproject.toml configs/operator deploy scripts/operator tests/test_operator_deployment_wiring.py README.md docs
git commit -m "chore(deploy): target the symphony user layout"
git push -u origin feat/symphony-user-migration
```

Do not merge this branch into Maestro `main` yet.

---

### Task 4: Make the Three Virtuoso Repositories Layout-Aware

**Files:**
- Modify in virtuoso-tranquillo: `pyproject.toml`, `README.md`
- Modify in virtuoso-crescendo: `pyproject.toml`, `backtests/generate_book_performance.py`
- Modify in virtuoso-fugue: `pyproject.toml`, `README.md`

**Interfaces:**
- Consumes: Maestro's new sibling path `/home/symphony/maestro`.
- Produces: branch `chore/symphony-home-layout` in each repository with `../../maestro/src` test/import paths.

- [ ] **Step 1: Create isolated Virtuoso worktrees**

Use `superpowers:using-git-worktrees` in each repository and create branch `chore/symphony-home-layout`. Keep live root checkouts on `main`.

- [ ] **Step 2: Update Tranquillo**

Change `../../Maestro/src` to `../../maestro/src`. Replace active root setup examples with `/home/symphony/maestro` and `/home/symphony/virtuoso/virtuoso-tranquillo`.

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git add pyproject.toml README.md
git commit -m "docs: target the symphony home layout"
git push -u origin chore/symphony-home-layout
```

- [ ] **Step 3: Update Crescendo**

Change `../../Maestro/src` to `../../maestro/src`. In `backtests/generate_book_performance.py`, change only hard-coded command examples from `/root/maestro-operator` to `/home/symphony/maestro-operator`.

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git add pyproject.toml backtests/generate_book_performance.py
git commit -m "docs: target the symphony home layout"
git push -u origin chore/symphony-home-layout
```

- [ ] **Step 4: Update Fugue**

Change `../../Maestro/src` to `../../maestro/src`. Replace active root commands with `/home/symphony/maestro` and `/home/symphony/virtuoso/virtuoso-fugue`.

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git add pyproject.toml README.md
git commit -m "docs: target the symphony home layout"
git push -u origin chore/symphony-home-layout
```

- [ ] **Step 5: Confirm all layout branches are remote-visible**

```bash
git ls-remote --heads https://github.com/koomink/maestro.git feat/symphony-user-migration
git ls-remote --heads https://github.com/koomink/virtuoso-tranquillo.git chore/symphony-home-layout
git ls-remote --heads https://github.com/koomink/virtuoso-crescendo.git chore/symphony-home-layout
git ls-remote --heads https://github.com/koomink/virtuoso-fugue.git chore/symphony-home-layout
```

Expected: one non-empty SHA line per repository.

---

### Task 5: Pre-stage the `symphony` Home Runtime

**Files:**
- Create outside Git: `/home/symphony/{AGENTS.md,maestro,maestro-operator,virtuoso}`
- Create outside Git: `/home/symphony/.local/node-v24.19.0`
- Create outside Git: new Python environments and `node_modules`

**Interfaces:**
- Consumes: four pushed layout branches and current operator directory.
- Produces: a user-owned, tested runtime not yet referenced by production systemd.

- [ ] **Step 1: Repair the account home and create parents**

```bash
sudo chown symphony:symphony /home/symphony
sudo chmod 0750 /home/symphony
sudo install -d -o symphony -g symphony -m 0750 /home/symphony/virtuoso
sudo install -d -o symphony -g symphony -m 0700 /home/symphony/maestro-operator
sudo install -d -o symphony -g symphony -m 0750 /home/symphony/.local
sudo install -m 0644 -o symphony -g symphony /root/projects/Symphony/AGENTS.md /home/symphony/AGENTS.md
```

Verify owners and modes with `stat -c '%A %U:%G %n'`.

- [ ] **Step 2: Install user-accessible uv and Node**

```bash
sudo install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
sudo rsync -aHAX --chown=symphony:symphony \
  /root/.nvm/versions/node/v24.19.0/ \
  /home/symphony/.local/node-v24.19.0/
sudo -u symphony -H /usr/local/bin/uv python install 3.11
sudo -u symphony -H /home/symphony/.local/node-v24.19.0/bin/node --version
sudo -u symphony -H /home/symphony/.local/node-v24.19.0/bin/npm --version
```

Expected: Python installs under `/home/symphony`; Node/npm run without opening `/root` files.

- [ ] **Step 3: Clone layout branches into final paths**

```bash
sudo -u symphony -H git clone --branch feat/symphony-user-migration \
  https://github.com/koomink/maestro.git /home/symphony/maestro
sudo -u symphony -H git clone --branch chore/symphony-home-layout \
  https://github.com/koomink/virtuoso-tranquillo.git \
  /home/symphony/virtuoso/virtuoso-tranquillo
sudo -u symphony -H git clone --branch chore/symphony-home-layout \
  https://github.com/koomink/virtuoso-crescendo.git \
  /home/symphony/virtuoso/virtuoso-crescendo
sudo -u symphony -H git clone --branch chore/symphony-home-layout \
  https://github.com/koomink/virtuoso-fugue.git \
  /home/symphony/virtuoso/virtuoso-fugue
```

Expected: every `git status --short --branch` is clean and names its layout branch.

- [ ] **Step 4: Build the Maestro Python environment**

```bash
sudo -u symphony -H sh -c 'cd /home/symphony/maestro && /usr/local/bin/uv sync --frozen --all-extras'
sudo -u symphony -H /usr/local/bin/uv pip install \
  --python /home/symphony/maestro/.venv/bin/python \
  /home/symphony/virtuoso/virtuoso-tranquillo \
  /home/symphony/virtuoso/virtuoso-crescendo \
  /home/symphony/virtuoso/virtuoso-fugue
```

Verify no direct URL or executable path points at root:

```bash
rg -n '/root/' /home/symphony/maestro/.venv/bin \
  /home/symphony/maestro/.venv/lib/python3.11/site-packages/*/direct_url.json
```

Expected: no matches.

- [ ] **Step 5: Build the frontend as `symphony`**

```bash
sudo -u symphony -H env PATH=/home/symphony/.local/node-v24.19.0/bin:/usr/bin:/bin \
  /home/symphony/.local/node-v24.19.0/bin/npm --prefix /home/symphony/maestro ci
sudo -u symphony -H env PATH=/home/symphony/.local/node-v24.19.0/bin:/usr/bin:/bin \
  /home/symphony/.local/node-v24.19.0/bin/npm --prefix /home/symphony/maestro run dashboard:check
sudo -u symphony -H env PATH=/home/symphony/.local/node-v24.19.0/bin:/usr/bin:/bin \
  /home/symphony/.local/node-v24.19.0/bin/npm --prefix /home/symphony/maestro run dashboard:build
```

Expected: clean install, TypeScript check, and Vite build succeed.

- [ ] **Step 6: Run complete new-path Maestro verification**

```bash
sudo -u symphony -H sh -c 'cd /home/symphony/maestro && .venv/bin/pytest -q'
sudo -u symphony -H sh -c 'cd /home/symphony/maestro && .venv/bin/ruff check .'
```

Expected: reviewed pass/skip counts with no permission or import error.

- [ ] **Step 7: Run applicable Virtuoso suites**

```bash
sudo -u symphony -H sh -c 'cd /home/symphony/virtuoso/virtuoso-tranquillo && /home/symphony/maestro/.venv/bin/pytest -q'
sudo -u symphony -H sh -c 'cd /home/symphony/virtuoso/virtuoso-crescendo && /home/symphony/maestro/.venv/bin/pytest -q'
sudo -u symphony -H sh -c 'cd /home/symphony/virtuoso/virtuoso-fugue && /home/symphony/maestro/.venv/bin/pytest -q'
```

Expected: each suite passes or only documented opt-in integration tests skip.

- [ ] **Step 8: Make a non-authoritative operator copy**

```bash
sudo rsync -aHAX --chown=symphony:symphony \
  /root/maestro-operator/ /home/symphony/maestro-operator/
sudo chmod 0700 /home/symphony/maestro-operator /home/symphony/maestro-operator/var
```

This is staging only. Do not run a command that writes its SQLite database.

- [ ] **Step 9: Verify staged unit syntax and repository config composition**

```bash
sudo systemd-analyze verify /home/symphony/maestro/deploy/systemd/maestro-*.service \
  /home/symphony/maestro/deploy/systemd/maestro-*.timer \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard.path
sudo -u symphony -H /home/symphony/maestro/.venv/bin/python -c \
  "from maestro.config.loader import load_config; [load_config(p) for p in ('/home/symphony/maestro/configs/operator/symphony_readonly.yaml','/home/symphony/maestro/configs/operator/symphony_signal.yaml','/home/symphony/maestro/configs/operator/symphony_approval.yaml')]"
```

Expected: syntax and repository config composition pass without loading staged live state.

---

### Task 6: Review and Promote the Four Layout Branches

**Files:**
- No new edits; integration-only task.

**Interfaces:**
- Consumes: passing Task 3-5 branches at final paths.
- Produces: remote `main` in each repository contains the layout while old root checkouts remain pinned for rollback; no Git write credentials are copied into `/home/symphony`.

- [ ] **Step 1: Review each branch against its main**

Confirm every line traces to approved paths, permissions, deployment, tests, or docs. Require all Task 3-5 checks to be green.

- [ ] **Step 2: Fast-forward each Virtuoso remote main from its root-owned worktree**

In each root-owned isolated worktree for `chore/symphony-home-layout`:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
```

Expected: ancestry check and push succeed. If either rejects, stop and review remote divergence; never force-push.

- [ ] **Step 3: Fast-forward Maestro remote main from its root-owned worktree**

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
```

Expected: remote `main` advances to the verified layout branch. Do not pull it into `/root/projects/Symphony/Maestro`.

- [ ] **Step 4: Move new-home clones onto the promoted mains and reverify**

In each new-home clone:

```bash
git switch main
git pull --ff-only origin main
git status --short --branch
```

Then run:

```bash
sudo -u symphony -H sh -c 'cd /home/symphony/maestro && .venv/bin/pytest -q && .venv/bin/ruff check .'
```

Expected: all new-home clones are clean on `main`; Maestro verification passes. Do not copy root Git credentials into the home.

---

### Task 7: Perform the Maintenance-Window Cutover

**Files:**
- Modify outside Git: `/home/symphony/maestro-operator/*.yaml`
- Modify outside Git: `/etc/maestro/maestro.env`
- Replace deployed units: `/etc/systemd/system/maestro-*`
- Create backups: `/root/maestro-migration-backup/cutover-*`

**Interfaces:**
- Consumes: green root baseline, green new-home main branches, staged runtime, and current operator state.
- Produces: production services running from `/home/symphony` with a complete rollback bundle.

- [ ] **Step 1: Create the cutover rollback directory**

```bash
MIGRATION_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o root -g root -m 0700 "/root/maestro-migration-backup/cutover-$MIGRATION_STAMP"
```

Keep `MIGRATION_STAMP` in the maintenance shell.

- [ ] **Step 2: Back up root-managed files without printing secrets**

```bash
sudo cp -a /etc/maestro/maestro.env "/root/maestro-migration-backup/cutover-$MIGRATION_STAMP/maestro.env"
sudo cp -a /etc/systemd/system/maestro-* "/root/maestro-migration-backup/cutover-$MIGRATION_STAMP/"
```

Expected: backup directory remains mode `0700`; never display `maestro.env`.

- [ ] **Step 3: Stop timers and restart triggers first**

```bash
sudo systemctl stop maestro-dashboard-health.timer
sudo systemctl stop maestro-book-performance.timer maestro-fx-refresh.timer maestro-heartbeat.timer
sudo systemctl stop maestro-resume-order-tracking.timer
sudo systemctl stop maestro-symphony-readonly.timer maestro-symphony-readonly-kr.timer maestro-symphony-readonly-us.timer
sudo systemctl stop maestro-symphony-signal.timer maestro-symphony-signal-kr.timer maestro-symphony-signal-us.timer
sudo systemctl stop maestro-dashboard.path maestro-dashboard-src-watch.service
```

- [ ] **Step 4: Stop applications and active one-shot work**

```bash
sudo systemctl stop maestro-telegram-operator.service maestro-dashboard.service
systemctl list-units --state=running --type=service 'maestro-*' --no-legend --no-pager
pgrep -af '/root/projects/Symphony/Maestro/.venv/bin/maestro'
```

Expected: both checks show no active Maestro workload. Stop an exact remaining unit and recheck if necessary.

- [ ] **Step 5: Checkpoint, verify, and back up authoritative state**

```bash
sudo sqlite3 /root/maestro-operator/var/symphony_state.db 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;'
sudo sqlite3 /root/maestro-operator/var/symphony_state.db ".backup '/root/maestro-migration-backup/cutover-$MIGRATION_STAMP/symphony_state.db'"
sudo cp -a /root/maestro-operator/var/symphony_audit.jsonl "/root/maestro-migration-backup/cutover-$MIGRATION_STAMP/"
```

Expected: checkpoint completes and integrity is `ok`. Abort and restart root services otherwise.

- [ ] **Step 6: Perform the final operator synchronization**

```bash
sudo rsync -aHAX --chown=symphony:symphony \
  /root/maestro-operator/ /home/symphony/maestro-operator/
sudo chmod 0700 /home/symphony/maestro-operator /home/symphony/maestro-operator/var
sudo find /home/symphony/maestro-operator -type f -exec chmod 0600 {} +
```

Do not use `--delete` during the first cutover.

- [ ] **Step 7: Rewrite only approved prefixes in live YAML**

```bash
sudo -u symphony perl -0pi -e 's#/root/maestro-operator#/home/symphony/maestro-operator#g' \
  /home/symphony/maestro-operator/broker_accounts.yaml \
  /home/symphony/maestro-operator/symphony_readonly.yaml \
  /home/symphony/maestro-operator/symphony_signal.yaml \
  /home/symphony/maestro-operator/symphony_approval.yaml
rg -l '/root/maestro-operator|/root/projects/Symphony' \
  /home/symphony/maestro-operator/broker_accounts.yaml \
  /home/symphony/maestro-operator/symphony_readonly.yaml \
  /home/symphony/maestro-operator/symphony_signal.yaml \
  /home/symphony/maestro-operator/symphony_approval.yaml
```

Expected: `rg` has no output.

- [ ] **Step 8: Update environment path values without printing them**

```bash
sudo perl -0pi -e 's#/root/maestro-operator#/home/symphony/maestro-operator#g; s#/root/projects/Symphony/Maestro#/home/symphony/maestro#g; s#/root/projects/Symphony/Virtuoso/virtuoso-tranquillo#/home/symphony/virtuoso/virtuoso-tranquillo#g; s#/root/projects/Symphony/Virtuoso/virtuoso-crescendo#/home/symphony/virtuoso/virtuoso-crescendo#g; s#/root/projects/Symphony/Virtuoso/virtuoso-fugue#/home/symphony/virtuoso/virtuoso-fugue#g' /etc/maestro/maestro.env
sudo chown root:symphony /etc/maestro /etc/maestro/maestro.env
sudo chmod 0750 /etc/maestro
sudo chmod 0640 /etc/maestro/maestro.env
if sudo rg -q '/root/maestro-operator|/root/projects/Symphony' /etc/maestro/maestro.env; then exit 1; fi
sudo -u symphony test -r /etc/maestro/maestro.env
```

- [ ] **Step 9: Initialize and verify copied DB once as `symphony`**

```bash
sudo -u symphony -H /home/symphony/maestro/.venv/bin/python -c \
  "from maestro.state.store import StateStore; StateStore('/home/symphony/maestro-operator/var/symphony_state.db')"
sudo -u symphony sqlite3 -readonly /home/symphony/maestro-operator/var/symphony_state.db 'PRAGMA integrity_check;'
```

Expected: constructor exits zero and integrity is `ok`.

- [ ] **Step 10: Install only the previously deployed unit set**

Install this exact previously deployed set. Do not install `maestro-run-once.service`, `maestro-run-once.timer`, or another source-only unit absent from the before inventory.

Install the privileged watcher script first:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/maestro
sudo install -o root -g root -m 0755 \
  /home/symphony/maestro/deploy/scripts/watch_dashboard_backend.sh \
  /usr/local/libexec/maestro/watch_dashboard_backend.sh
```

```bash
sudo install -m 0644 \
  /home/symphony/maestro/deploy/systemd/maestro-book-performance.service \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard-health.service \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard-reload.service \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard-src-watch.service \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard.service \
  /home/symphony/maestro/deploy/systemd/maestro-fx-refresh.service \
  /home/symphony/maestro/deploy/systemd/maestro-heartbeat.service \
  /home/symphony/maestro/deploy/systemd/maestro-resume-order-tracking.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly-kr.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly-us.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-rebalance-kr.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-rebalance-us.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal-kr.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal-us.service \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal.service \
  /home/symphony/maestro/deploy/systemd/maestro-telegram-operator.service \
  /etc/systemd/system/
sudo install -m 0644 \
  /home/symphony/maestro/deploy/systemd/maestro-book-performance.timer \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard-health.timer \
  /home/symphony/maestro/deploy/systemd/maestro-fx-refresh.timer \
  /home/symphony/maestro/deploy/systemd/maestro-heartbeat.timer \
  /home/symphony/maestro/deploy/systemd/maestro-resume-order-tracking.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly-kr.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly-us.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-readonly.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal-kr.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal-us.timer \
  /home/symphony/maestro/deploy/systemd/maestro-symphony-signal.timer \
  /home/symphony/maestro/deploy/systemd/maestro-dashboard.path \
  /etc/systemd/system/
```

Compare the filenames to `unit-files.before.txt` before continuing; stop if the deployed set differs from the captured set.

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/maestro-*.service \
  /etc/systemd/system/maestro-*.timer /etc/systemd/system/maestro-dashboard.path
```

Expected: verification exits zero.

- [ ] **Step 11: Start dashboard and Telegram first**

```bash
sudo systemctl start maestro-dashboard.service
curl -fsS http://127.0.0.1:8503/api/health
sudo systemctl start maestro-telegram-operator.service
sudo systemctl is-active maestro-dashboard.service maestro-telegram-operator.service
sudo journalctl -u maestro-dashboard.service -u maestro-telegram-operator.service -n 100 --no-pager
```

Expected: HTTP succeeds, units are active, and logs have no permission, import, path, lock, or migration error.

- [ ] **Step 12: Start control-plane watchers**

```bash
sudo systemctl start maestro-dashboard-src-watch.service maestro-dashboard.path
sudo systemctl is-active maestro-dashboard-src-watch.service maestro-dashboard.path
```

Expected: watcher is active and path unit is active/waiting.

- [ ] **Step 13: Verify safe one-shot services individually**

```bash
sudo systemctl reset-failed maestro-fx-refresh.service
sudo systemctl start maestro-fx-refresh.service
sudo systemctl start maestro-heartbeat.service
sudo systemctl start maestro-symphony-readonly.service
sudo systemctl status maestro-fx-refresh.service maestro-heartbeat.service maestro-symphony-readonly.service --no-pager
```

Expected: all complete successfully. Do not start signal or rebalance services manually.

- [ ] **Step 14: Restore recorded timer/path enablement**

Use `unit-files.before.txt` as authority. For the captured 2026-08-14 baseline, start this exact active timer set after service checks; keep the generic signal timer disabled:

```bash
sudo systemctl start \
  maestro-book-performance.timer \
  maestro-dashboard-health.timer \
  maestro-fx-refresh.timer \
  maestro-heartbeat.timer \
  maestro-resume-order-tracking.timer \
  maestro-symphony-readonly-kr.timer \
  maestro-symphony-readonly-us.timer \
  maestro-symphony-readonly.timer \
  maestro-symphony-signal-kr.timer \
  maestro-symphony-signal-us.timer
sudo systemctl disable --now maestro-symphony-signal.timer
```

Then verify:

```bash
systemctl list-units --all --type=timer --type=path 'maestro-*' --no-legend --no-pager
```

Expected: after set matches before set; `maestro-symphony-signal.timer` remains disabled if previously disabled.

---

### Task 8: Verify Production and Hold Rollback Assets

**Files:**
- Modify: `docs/TASKS.md`
- Modify if architectural wording changed: `docs/TRD.md`, `docs/ROADMAP.md`
- Modify: this plan with non-secret evidence

**Interfaces:**
- Consumes: running new-home services and before-state files.
- Produces: evidence-backed acceptance and an explicit no-delete rollback hold.

- [ ] **Step 1: Verify runtime identity and active paths**

```bash
systemctl show -p User -p Group -p WorkingDirectory \
  maestro-dashboard.service maestro-telegram-operator.service maestro-fx-refresh.service
ps -eo user:16,pid,cmd | rg '/home/symphony/maestro|maestro-operator'
if sudo rg -q '/root/projects/Symphony|/root/maestro-operator' /etc/systemd/system/maestro-*; then exit 1; fi
if sudo rg -q '/root/projects/Symphony|/root/maestro-operator' /etc/maestro/maestro.env; then exit 1; fi
```

Expected: users are `symphony`, active commands use home paths, and old-path checks pass silently.

- [ ] **Step 2: Verify permissions and integrity**

```bash
stat -c '%A %U:%G %n' /home/symphony /home/symphony/maestro /home/symphony/maestro-operator /home/symphony/maestro-operator/var /etc/maestro /etc/maestro/maestro.env
sudo -u symphony test -w /home/symphony/maestro-operator/var
sudo -u symphony sqlite3 -readonly /home/symphony/maestro-operator/var/symphony_state.db 'PRAGMA integrity_check;'
```

Expected: design modes, writable runtime directory, and `ok` integrity.

- [ ] **Step 3: Verify units and logs**

```bash
systemctl list-units --all --type=service --type=timer --type=path 'maestro-*' --no-legend --no-pager
sudo journalctl -u maestro-dashboard.service -u maestro-telegram-operator.service \
  -u maestro-fx-refresh.service -u maestro-heartbeat.service \
  -u maestro-symphony-readonly.service --since '1 hour ago' --no-pager
```

Expected: no new permission, CHDIR, import, DB lock, or migration error.

- [ ] **Step 4: Re-run production-tree verification**

```bash
sudo -u symphony -H sh -c 'cd /home/symphony/maestro && .venv/bin/pytest -q && .venv/bin/ruff check .'
sudo -u symphony -H env PATH=/home/symphony/.local/node-v24.19.0/bin:/usr/bin:/bin \
  /home/symphony/.local/node-v24.19.0/bin/npm --prefix /home/symphony/maestro run dashboard:check
```

Expected: tests, Ruff, and TypeScript check pass on production source.

- [ ] **Step 5: Record completion without secrets**

Mark migration complete in `docs/TASKS.md`, update TRD/ROADMAP only if their runtime statements would otherwise be false, and append checked evidence to this plan: commit SHAs, pass counts, unit comparison, integrity results, and backup directory name. Never include environment values, account identifiers, or token contents.

- [ ] **Step 6: Commit and push the record**

Stage only files that actually changed:

```bash
git add docs/TASKS.md docs/TRD.md docs/ROADMAP.md docs/superpowers/plans/2026-08-14-symphony-user-migration.md
git commit -m "docs: record the symphony user cutover"
git push origin main
```

- [ ] **Step 7: Observe scheduled-cycle acceptance**

Wait for at least one KR and one US scheduled cycle. Verify expected read-only and signal timers through unit status and journals; do not manually trigger signal/rebalance services. Record successful timestamps without payloads or account details.

- [ ] **Step 8: Preserve rollback assets and stop**

Confirm these remain:

```text
/root/projects/Symphony
/root/maestro-operator
/root/maestro-migration-backup
```

Do not delete or repurpose them. Cleanup is a separate approved task after scheduled-cycle acceptance and state/audit comparison.

## Rollback Procedure

If cutover fails before the new DB receives an operational write:

1. Stop every new timer, path unit, and Maestro service.
2. Restore `/etc/maestro/maestro.env` and deployed units from `cutover-$MIGRATION_STAMP`.
3. Run `systemctl daemon-reload`.
4. Start root-based dashboard and Telegram services.
5. Restore only the timer/path set captured in `unit-files.before.txt` and `units.before.txt`.
6. Confirm root DB integrity and service health.

If the new DB has received an operational write, stop and compare old/new DB and audit streams before rollback. Do not overwrite either database or discard approvals, broker observations, token changes, or lifecycle events.
