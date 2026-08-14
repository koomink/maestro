# Symphony User Migration Design

**Date:** 2026-08-14

**Status:** Approved for implementation planning

## Goal

Move the Symphony source trees and Maestro operator data out of `/root` and
run application workloads as the existing `symphony` user, while preserving a
tested rollback path and keeping secrets outside the source trees.

The final layout is:

```text
/home/symphony/
├── AGENTS.md
├── maestro/
├── maestro-operator/
└── virtuoso/
    ├── virtuoso-tranquillo/
    ├── virtuoso-crescendo/
    └── virtuoso-fugue/
```

## Current-State Findings

- The `symphony` account exists as UID/GID 1001, but `/home/symphony` is owned
  by `nobody:nogroup` and is not currently usable by that account.
- Maestro, the three active Virtuoso repositories, and their build artifacts
  are owned by `root` under `/root/projects/Symphony`.
- Live operator configuration, SQLite state, audit output, token caches, and
  backups are under `/root/maestro-operator` and owned by `root`.
- `/etc/maestro/maestro.env` is `root:root` mode `0600`. Its values remain
  secret; only its key names were inspected during design.
- Deployed Maestro systemd units contain absolute `/root` paths, and the
  application services run as `root`.
- The Maestro virtual environment points at a Python interpreter under
  `/root/.local/share/uv` and contains local-package metadata with absolute
  `/root/projects/Symphony` paths. It cannot be moved intact.
- Node and npm are supplied by root's NVM installation and are not a runtime
  dependency that the `symphony` user can inherit safely.
- Maestro, virtuoso-tranquillo, virtuoso-crescendo, and virtuoso-fugue have
  clean `main` worktrees at the time of design.
- `maestro-fx-refresh.service` is already failed. On 2026-08-14 it opened the
  shared state database and raised `OperationalError: duplicate column name:
  approval_id` during `StateStore._init_db()`.
- `hermes-agent-router.service` is independently in a restart loop because its
  working directory does not exist. That service is outside this migration and
  must not be bundled into the Symphony change.

## Scope

This work includes:

1. Making `StateStore` schema initialization safe when several Maestro
   processes open one database concurrently.
2. Preparing the approved `/home/symphony` directory layout and permissions.
3. Copying the four Git repositories without transplanting virtual
   environments, dependency directories, or caches.
4. Rebuilding the Python and frontend environments as `symphony`.
5. Moving the live operator configuration and runtime state during a planned
   maintenance window.
6. Updating repository-owned deployment files, deployed systemd units,
   environment path values, live YAML path values, and deployment docs.
7. Verifying the new runtime and retaining the old paths for rollback.

This work does not include:

- deleting `/root/projects/Symphony` or `/root/maestro-operator` during the
  initial cutover;
- moving secrets out of `/etc/maestro/maestro.env`;
- repairing `hermes-agent-router.service`;
- adding symlink compatibility paths under `/root`;
- manually running signal or rebalance workflows that could create approvals
  or live-order side effects;
- redesigning Maestro configuration composition or state storage.

## Directory and Permission Model

| Path | Owner | Mode | Purpose |
|---|---|---:|---|
| `/home/symphony` | `symphony:symphony` | `0750` | Account home and migration root |
| `/home/symphony/maestro` | `symphony:symphony` | repository defaults | Maestro Git repository |
| `/home/symphony/virtuoso` | `symphony:symphony` | `0750` | Virtuoso repository parent |
| `/home/symphony/maestro-operator` | `symphony:symphony` | `0700` | Live config and state root |
| operator YAML and runtime secrets | `symphony:symphony` | `0600` | Non-public operator material |
| operator `var` directory | `symphony:symphony` | `0700` | DB, audit, token cache, locks |
| `/etc/maestro` | `root:symphony` | `0750` | Secret environment directory |
| `/etc/maestro/maestro.env` | `root:symphony` | `0640` | Root-managed secrets readable by services |
| `/etc/systemd/system/maestro-*` | `root:root` | `0644` | Root-managed service definitions |

`maestro-operator` remains outside every Git repository. The environment file
remains outside the home directory. The old `/root` trees stay unchanged until
post-cutover acceptance is complete.

## StateStore Initialization Fix

### Problem

`StateStore.__init__()` currently calls `_init_db()` without the existing
cross-process writer lock. Schema migrations use a check-then-alter sequence:
they read `PRAGMA table_info` or `table_xinfo`, then conditionally execute
`ALTER TABLE`. Two constructors can therefore observe the same missing column
before either change is visible and race on the same `ALTER TABLE`.

The production log proves the duplicate-column failure. It does not identify
the competing process conclusively, so the implementation must address the
unprotected initialization boundary rather than special-case one service.

### Design

- Acquire the existing state writer lock before entering `_init_db()`.
- Hold it across schema inspection, migrations, index creation, backfills, and
  commit.
- Let later constructors wait, then inspect the committed schema themselves.
- Preserve the current lock timeout and lock-holder diagnostics.
- Do not catch and ignore generic `duplicate column` errors.
- Do not introduce a second migration-lock implementation when the state
  writer lock already provides the required process boundary.

### Regression Coverage

- Retain the existing reopen/idempotency migration tests.
- Add a deterministic test proving a constructor waits while another process
  owns the state writer lock.
- Add a multiprocessing migration test in which several constructors open one
  legacy-schema database, all processes exit successfully, both generated
  columns and indexes exist, and `PRAGMA integrity_check` returns `ok`.
- Run the complete Maestro test suite and Ruff before deployment.
- Deploy this fix before changing any paths, restart affected application
  services serially, and verify `maestro-fx-refresh.service` succeeds. A green
  FX refresh is a prerequisite for beginning the directory cutover.

## Runtime Environment Rebuild

The source copy excludes `.venv`, `node_modules`, Python bytecode, and tool
caches. Git metadata and tracked build inputs are retained.

The `symphony` account receives its own Python 3.11 runtime managed by a uv
binary outside `/root`. Maestro is synchronized from `uv.lock`, then the three
active Virtuoso packages are installed from their new paths:

```text
/home/symphony/virtuoso/virtuoso-tranquillo
/home/symphony/virtuoso/virtuoso-crescendo
/home/symphony/virtuoso/virtuoso-fugue
```

Stale direct references to removed historical repositories are not copied into
the new environment. A Node installation available to `symphony` is used for
`npm ci`, TypeScript checking, and the dashboard build; root's NVM tree is not
reused.

The production `PYTHONPATH` becomes, in order:

```text
/home/symphony/virtuoso/virtuoso-tranquillo/src
/home/symphony/virtuoso/virtuoso-crescendo/src
/home/symphony/virtuoso/virtuoso-fugue/src
/home/symphony/maestro/src
```

## Configuration and systemd Design

The path-valued entries in `/etc/maestro/maestro.env` are edited without
printing or rewriting unrelated secret values:

- `MAESTRO_CONFIG`
- `MAESTRO_READONLY_CONFIG`
- `MAESTRO_SIGNAL_CONFIG`
- `MAESTRO_APPROVAL_CONFIG`
- `PYTHONPATH`

Live YAML files are updated so SQLite, audit, token-cache, backtest-data, and
related runtime paths resolve under `/home/symphony/maestro-operator`. Relative
fragment and strategy-account references remain relative where they are
already valid.

Application units use:

```ini
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro ...
```

This applies to dashboard, Telegram operator, FX refresh, heartbeat,
KIS/Toss read-only refreshes, signal and rebalance workflows, outstanding-order
tracking, and book-performance generation.

The following control-plane helpers remain root-run because they invoke
`systemctl restart`; only their watched or executed paths change:

- `maestro-dashboard-reload.service`
- `maestro-dashboard-health.service`
- `maestro-dashboard-src-watch.service`

Timer and path units remain system units. The dashboard path unit and backend
watch script use the new source and operator paths.

Repository-owned systemd templates, `deploy/scripts/watch_dashboard_backend.sh`,
`README.md`, and `docs/vps_systemd.md` are changed in the same code revision as
the deployed path model. Deployed files under `/etc/systemd/system` are updated
only during cutover.

## Migration Strategy

The migration uses copy-and-cutover, not a direct move and not compatibility
symlinks.

### Phase 1: Repair and Establish a Green Baseline

1. Implement and test the serialized `StateStore` initialization.
2. Deploy the fix to the current root-based runtime.
3. Restart long-running application services one at a time.
4. Run and verify FX refresh, dashboard health, Telegram polling, and database
   integrity.
5. Record the enabled and active Maestro unit set for later comparison.

### Phase 2: Pre-stage Without Downtime

1. Repair `/home/symphony` ownership and create the approved directory tree.
2. Copy source repositories while excluding non-portable dependency artifacts.
3. Rebuild Python and Node environments as `symphony`.
4. Run Python tests, Ruff, TypeScript checks, and dashboard build at the new
   source path.
5. Prepare new live YAML, environment-file path edits, and systemd units in
   staged copies.
6. Run `systemd-analyze verify` on the staged unit definitions.
7. Keep the current root-based services running throughout this phase.

### Phase 3: Maintenance Window and State Copy

Stop in this order:

1. dashboard health timer;
2. all Maestro timers;
3. dashboard path unit;
4. dashboard source watcher;
5. Telegram operator;
6. dashboard;
7. any active Maestro one-shot service.

Confirm no Maestro process remains. Then checkpoint the SQLite WAL and require
`PRAGMA integrity_check` to return `ok`. Back up the live DB, audit log, token
caches, operator configuration, environment file, and deployed units. Perform
the final operator-directory synchronization and apply the approved ownership
and modes.

Update the live YAML paths and environment-file path values. Before starting
multiple services, open the copied database once as `symphony` through the new
runtime so schema initialization is serialized, then repeat the integrity
check.

### Phase 4: Activate in Layers

1. Install the new units and run `systemctl daemon-reload`.
2. Start dashboard and verify `/api/health`.
3. Start Telegram operator and verify healthy polling without sending a test
   trading action.
4. Start the dashboard watcher and path unit.
5. Run FX refresh, heartbeat, and read-only refresh individually and inspect
   their exit status and logs.
6. Start the corresponding timers only after their service checks pass.
7. Restore the remaining timers in the previously recorded enabled state.
8. Do not manually invoke signal or rebalance workflows; observe their next
   scheduled runs in the appropriate market windows.

## Verification and Acceptance

Pre-cutover verification requires:

- complete Maestro tests and Ruff;
- applicable Virtuoso test suites;
- `npm ci`, TypeScript check, and dashboard build;
- all four repositories recognized as clean by Git when run as `symphony`;
- `symphony` can read the environment file and read/write the operator `var`
  directory;
- staged units pass `systemd-analyze verify`;
- current FX refresh no longer fails during state initialization.

Post-cutover acceptance requires:

- application processes run as `symphony`;
- no active command or deployed unit refers to `/root/projects/Symphony`;
- no active configuration refers to `/root/maestro-operator`;
- both source and copied databases pass `PRAGMA integrity_check`;
- dashboard health and Telegram polling are healthy;
- FX refresh, heartbeat, and read-only refresh complete successfully;
- timers and the dashboard path unit are active and waiting;
- logs contain no new permission, working-directory, import, lock, or migration
  errors;
- at least one KR and one US scheduled cycle complete successfully.

Only after these conditions are reviewed may deletion of the old root paths be
proposed as a separate, explicitly approved operation.

## Rollback

The old source and operator directories remain untouched during initial
cutover. Unit files and `/etc/maestro/maestro.env` receive timestamped backups.

If activation fails before the new runtime performs an operational write:

1. stop all new timers, path units, and services;
2. restore the prior units and environment file;
3. run `systemctl daemon-reload`;
4. start the root-based dashboard and Telegram operator;
5. restore the previously enabled timers and path unit;
6. verify the old database and service health.

If the new runtime has written operational state, rollback is not automatic.
Both databases and audit streams must first be compared so approvals, broker
observations, token changes, or lifecycle events are not silently lost. The
decision is then either to preserve the new state while fixing the runtime or
to reconcile the delta before returning to the old database.

## Documentation and Operational Record

The implementation updates the repository deployment templates and the
operator documentation in the same change. The final handoff records:

- old and new path mappings;
- copied-data backup locations without secret values;
- before/after unit activation state;
- verification command results;
- the first successful KR and US scheduled cycles;
- remaining rollback artifacts and the later deletion decision.
