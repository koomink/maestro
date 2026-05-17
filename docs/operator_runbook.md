# Operator Runbook

This runbook covers halt recovery, KIS read-only reconciliation, and links to
the Ataraxia KIS mock-investment broker-submit pilot. It does not authorize live
auto-trading, market orders, direct broker CLI actions, dashboard write
controls, or Telegram admin controls.

## Operator Config Discipline

Use one operator-local config for every process in a deployment. `run-once`,
`kis-sync`, `reconcile`, `health`, `ops-alerts`, `telegram-operator`, dashboard,
and systemd timers should all point at the same YAML file, state DB path, and
audit log path. Do not run the Telegram operator from a separate Telegram-only
config when it is expected to represent the live operator state.
Set `MAESTRO_CONFIG` in the operator environment or systemd environment file so
routine commands can omit `--config`; use an explicit `--config` only for
isolated tests or manual overrides.

This is necessary because Maestro is currently a hybrid operator architecture:
one-shot CLI jobs and long-running Telegram/dashboard services coordinate
through SQLite and JSONL, not through a single in-memory daemon. SQLite has
timeout, `busy_timeout`, WAL settings, a StateStore writer lock, and a config
identity drift check. If a command reports a state DB config identity mismatch,
stop scheduled services and verify every unit points at the intended operator
config before continuing.

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

For scheduled deployments, configure `monitoring.heartbeat_max_age_seconds` and
`monitoring.scheduled_run_max_age_seconds`, then run:

```bash
maestro heartbeat --config <config>
maestro health --config <config>
maestro ops-alerts --config <config>
```

`health` fails on missed heartbeat, missed scheduled `run-once`, broken audit
hash chain, failed reconciliation, stale broker state, or active safety halt.
`ops-alerts` sends warn/fail health checks to configured Telegram approval
chats.

## KIS Read-only Reconciliation

1. Use a KIS read-only or live-approval config for the intended broker product,
   such as `kis_overseas_stock` for US-listed instruments or
   `kis_domestic_stock` for KRX instruments.
2. Confirm KIS env vars are present with:

```bash
maestro health --config <config>
```

3. Fetch and store the broker snapshot:

```bash
maestro kis-sync --config <config>
maestro kis-account --config <config>
```

4. For the first rehearsal on a verified account baseline, adopt the latest
   broker snapshot into Maestro state:

```bash
maestro adopt-broker-snapshot --config <config> --reason "operator baseline accepted"
```

This is a state-only action. It does not call a broker order endpoint and
refuses broker positions that are neither in `portfolio.allowed_symbols` nor
known `universe.instruments` allowed by `universe.policy`.

5. Reconcile Maestro state against the latest broker snapshot:

```bash
maestro reconcile --config <config>
```

6. If reconciliation fails, do not proceed to live approval. Review the reported
   cash and position differences, manual broker activity, fills, and the latest
   Maestro portfolio state.

KIS current price data is broker reference data for checks and reconciliation.
Strategy research data must still come through Maestro DataHub.

## Ataraxia KIS Paper Pilot

Use `docs/ataraxia_kis_paper_pilot.md` for the Ataraxia domestic ETF promotion
from Maestro paper runs to the KIS mock-investment approval-gated broker-submit
path. The pilot scope is `kis_domestic_stock`, `kis.paper_trading=true`,
Telegram manual approval, limit orders only, and four scheduled trading-day
cycles. A real cash account and `live_auto` remain out of scope.
