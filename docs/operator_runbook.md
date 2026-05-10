# Operator Runbook

This runbook covers halt recovery and KIS overseas read-only reconciliation.
It does not authorize live auto-trading, market orders, direct broker CLI
actions, dashboard write controls, or Telegram admin controls.

## Halt Recovery

1. Stop scheduled jobs that could submit or approve work.
2. Check current safety state:

```bash
maestro safety-status --config <config>
maestro health --config <config>
```

3. Review recent system and audit events for `safety_state`,
   `safety_execution_blocked`, `stale_data_halt`, `broker_reconciliation_halt`,
   `live_order_limit_halt`, `live_order_halt`, `live_order_status`,
   `fill_reconciliation`, `live_order_lifecycle`,
   `live_order_recovery_required`, `live_order_recovery_halt`, and
   `live_order_recovery_completed`.
4. Check broker account state in the broker UI.
5. Run read-only sync and reconciliation:

```bash
maestro kis-sync --config <config>
maestro reconcile --config <config>
maestro reconcile-fills --config <config>
```

6. If live order recovery was required, record recovery completion:

```bash
maestro recover-live-order --config <config> --reason "broker truth reconciled"
```

7. If the halt cause is understood and resolved, clear only a halted state with
   an explicit reason:

```bash
maestro clear-halt --config <config> --reason "operator reviewed broker state and reconciliation passed"
```

`resume` must not clear `killed`. `clear-halt` must not clear `killed`. A kill
switch requires a separately defined safe recovery procedure.

## Monitoring

For scheduled deployments, configure `execution.heartbeat_max_age_seconds` and
`execution.scheduled_run_max_age_seconds`, then run:

```bash
maestro heartbeat --config <config>
maestro health --config <config>
maestro ops-alerts --config <config>
```

`health` fails on missed heartbeat, missed scheduled `run-once`, broken audit
hash chain, failed reconciliation, stale broker state, or active safety halt.
`ops-alerts` sends warn/fail health checks to configured Telegram approval
chats.

## KIS Overseas Reconciliation

1. Use a `kis_overseas_stock` read-only config.
2. Confirm KIS env vars are present with:

```bash
maestro health --config <config>
```

3. Fetch and store the broker snapshot:

```bash
maestro kis-sync --config <config>
maestro kis-account --config <config>
```

4. Reconcile Maestro state against the latest broker snapshot:

```bash
maestro reconcile --config <config>
```

5. If reconciliation fails, do not proceed to live approval. Review the reported
   cash and position differences, manual broker activity, fills, and the latest
   Maestro portfolio state.

KIS current price data is broker reference data for checks and reconciliation.
Strategy research data must still come through Maestro DataHub.
