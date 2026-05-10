# Backup / Restore Guide

Back up Maestro before changing configs, clearing halts, rotating hosts, or
running broker reconciliation after manual broker activity.

## Files To Back Up

- SQLite state DB from `state.sqlite_path`
- SQLite WAL/SHM sidecar files if present
- JSONL audit log from `audit.jsonl_path`
- Local untracked config files
- Owner-only KIS token cache file, if configured

Do not include raw environment files in broad backups unless the backup target is
approved for secrets.

## Backup

Stop scheduled Maestro jobs or ensure no command is writing state. Then copy the
state and audit files:

```bash
mkdir -p backups/maestro-YYYYMMDD-HHMMSS
cp var/*.db var/*.db-wal var/*.db-shm var/*.jsonl backups/maestro-YYYYMMDD-HHMMSS/ 2>/dev/null || true
```

For SQLite databases in active use, prefer the SQLite online backup command from
a controlled shell:

```bash
sqlite3 var/kis_overseas_readonly_state.db ".backup 'backups/maestro-YYYYMMDD-HHMMSS/state.db'"
```

## Restore

1. Stop Maestro timers/services.
2. Move the current state and audit files aside.
3. Restore the selected backup files.
4. Run:

```bash
maestro health --config <config>
maestro status --config <config>
maestro kis-account --config <config>
```

Confirm the `audit_integrity` health check is `ok` or investigate the restored
audit JSONL before resuming live approval.

5. For broker-connected workflows, run read-only sync and reconciliation before
   allowing live approval:

```bash
maestro kis-sync --config <config>
maestro reconcile --config <config>
```

Never restore a stale state DB and continue approval-gated live operation without
broker reconciliation.
