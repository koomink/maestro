# Daily Signal Approval Design

## Goal

Run the Symphony operator each morning as a three-phase flow: refresh read-only
broker truth, generate dashboard-visible strategy signals, then create Telegram
approval requests only when the saved signal package contains actionable orders.

## Architecture

Add one Maestro CLI orchestration command:

```bash
maestro daily-signal-approval \
  --readonly-config ${MAESTRO_READONLY_CONFIG} \
  --signal-config ${MAESTRO_SIGNAL_CONFIG} \
  --approval-config ${MAESTRO_APPROVAL_CONFIG}
```

The command owns the daily handoff so systemd does not have to parse CLI output
or chain multiple units. It runs read-only broker sync and reconciliation first,
runs `run_signal()` with the signal config, sends a Telegram daily signal
summary, and calls `approve_signal()` with the approval config only when the
signal summary reports `action_required=true`.

## Data Flow

1. Load read-only, signal, and approval configs.
2. Refresh KIS broker snapshots and reconciliation evidence with the read-only
   config.
3. Run all signal-enabled strategies with the signal config.
4. Persist an immutable `signal_package` under `signal_run_id`.
5. Send a Telegram summary that includes the signal id, loaded strategies,
   action requirement, and order preview count.
6. If no action is required, exit successfully.
7. If action is required, approve the saved signal package with the approval
   config. Approval does not re-run strategies.

## Approval Grouping

When a signal package contains orders from multiple strategy source groups,
`approve_signal()` creates one approval request per strategy/source group. Orders
without source metadata fall back to the package-level `source_strategy_ids`.
The package is marked consumed only after all approval groups have been handled.

## Failure Handling

The command fails closed. Read-only refresh, reconciliation, signal generation,
signal summary parsing, and approval validation errors all produce a non-zero
exit. Telegram summary delivery warnings do not turn an otherwise successful
signal run into a failed run. Approval polling stops the long-running Telegram
operator first when requested, then restarts it on exit.

## Systemd

The daily timer should call `maestro daily-signal-approval` directly. The legacy
shell wrapper remains a compatibility path, but the preferred path is the CLI
command because locking, status output, and signal handoff stay inside Maestro.
