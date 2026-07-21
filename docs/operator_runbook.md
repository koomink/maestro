# Operator Runbook

This runbook covers halt recovery, KIS read-only reconciliation, and links to
the Tranquillo KIS mock-investment broker-submit pilot. It does not authorize live
auto-trading, market orders, direct broker CLI actions, dashboard write
controls, or Telegram admin controls.

## Operator Config Discipline

Use one operator-local config for single-profile deployments. For the Symphony
three-phase workflow, use one operator-local config set:
`symphony_readonly.yaml`, `symphony_signal.yaml`, and
`symphony_approval.yaml`. These files may differ in mode, strategy execution,
approval settings, and order posture, but must point at the same state DB path,
`state.identity_group`, and audit log path.

Do not run the Telegram operator from a separate Telegram-only config when it is
expected to represent the live operator state. Set `MAESTRO_CONFIG` for
single-profile deployments, or set `MAESTRO_READONLY_CONFIG`,
`MAESTRO_SIGNAL_CONFIG`, and `MAESTRO_APPROVAL_CONFIG` for the Symphony config
set. Use explicit `--config` only for isolated tests or manual overrides.

This is necessary because Maestro is currently a hybrid operator architecture:
one-shot CLI jobs and long-running Telegram/dashboard services coordinate
through SQLite and JSONL, not through a single in-memory daemon. SQLite has
timeout, `busy_timeout`, WAL settings, a StateStore writer lock, and a config
identity drift check. If a command reports a state DB config identity mismatch,
stop scheduled services and verify every unit points at the intended operator
config set before continuing.

## Time Display

Maestro stores SQLite `created_at` values and audit timestamps as UTC so age
checks remain stable across servers. Operator-facing surfaces should display
those times in the operator timezone from `execution.market_session.timezone`
such as `Asia/Seoul`, with the timezone suffix included. For example,
`2026-05-23 06:43:23` in SQLite is shown as
`2026-05-23 15:43:23 KST` when the operator timezone is `Asia/Seoul`.

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
5. Run reconciliation and fill reconciliation. `reconcile` refreshes the KIS
   read-only broker snapshot before comparing it with Maestro state, so the
   result is based on current broker truth rather than the last dashboard view.
   In `live_approval`, `run-once` also refreshes and adopts the KIS broker
   snapshot before strategy work and, when
   `execution.require_reconciliation_pass=true`, records a
   `broker_reconciliation` event under the same run id before approval and order
   gates.

```bash
maestro reconcile --config <config>
maestro reconcile-fills --config <config>
```

6. If live order recovery was required, record recovery completion:

```bash
maestro recover-live-order --config <config> --reason "broker truth reconciled"
```

If an order was excluded before approval, review the
`live_order_capacity_blocked` event and Telegram's planned/available quantity.
Submit a smaller standalone proposal only on the same trading date:

```text
/retry_order <blocked_order_id> <quantity> [price]
```

This creates a new approval and re-runs capacity, market-session, quote,
reconciliation, and order-limit gates. Do not treat the blocked order itself as
a completed contribution.

For a broker-accepted order that remains `open` or `partially_filled`, use
`/orders` to refresh its status and copy the displayed command:

```text
/modify <broker_order_id> <price> [quantity]
```

The modification requires another Telegram approval. KIS domestic verifies the
current modifiable quantity before revision; an omitted quantity revises the
entire remainder. If account routing cannot be recovered uniquely, modification
is blocked and broker truth must be reconciled manually.

7. If the halt cause is understood and resolved, clear only a halted state with
   an explicit reason:

```bash
maestro clear-halt --config <config> --reason "operator reviewed broker state and reconciliation passed"
```

The command re-runs health checks and refuses to clear the halt while any
non-safety check is failing.

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

## Symphony Workflow

The multi-account operator workflow uses three configs:

1. `symphony_readonly`
   - Run on observation timers and explicit Dashboard/Telegram refresh paths.
   - `/account` reads stored state immediately; `/account_refresh [account_id]`
     performs an explicit broker call.
   - Refresh and reconcile accounts independently and display broker truth.
   - Run no Virtuoso strategies and create no approvals.
2. `symphony_signal`
   - Run on a schedule, initially daily at 09:10 KST.
   - Derive required accounts from selected strategies. Reuse snapshots up to
     15 minutes old; otherwise refresh only those accounts and fail closed only
     for a required-account failure.
   - Persist a `signal_run_id` and expose `action_required`; skip approval when
     `action_required=false`.
   - Submit no broker orders.
3. `symphony_approval`
   - Accept the `signal_run_id`.
   - Load the saved signal package and do not re-run strategies.
   - Refresh required snapshots older than 5 minutes, reject materially changed
     signal baselines, and re-check reconciliation, safety state, limits, and approval
     before any broker submit.

Dashboard and Telegram interactions should mirror the same separation. Global
Dashboard `Refresh` may run the read-only account refresh path and classify
latest signal freshness, but it must not run Virtuoso apps. Per-app Dashboard
`Generate Signal` and Telegram `/signal_<strategy>` commands use the signal
config for one selected app only, persist Dashboard-visible signal packages, and
do not create approval requests or submit broker orders.

The systemd wiring for this workflow is:

- `maestro-symphony-readonly.timer`: hourly all-account observation.
- `maestro-symphony-readonly-kr.timer` and `-us.timer`: market-session
  account-scoped 15-minute observation.
- `maestro-symphony-signal.timer`: weekday 09:10 Asia/Seoul and 09:40
  America/New_York orchestration using `maestro daily-signal-approval`.
- The command sends a daily signal summary, creates approval requests only when
  `action_required=true`, and temporarily stops
  `maestro-telegram-operator.service` during approval polling. If the daily
orchestration fails before the normal summary, it sends a best-effort Telegram
failure briefing and still exits non-zero for systemd.

Do not treat `live_readonly` as a strategy execution mode; signal generation is
a separate operator phase. `run-once` remains a legacy single-pipeline entrypoint
for paper and older workflows.

Broker account definitions are centralized in `broker_accounts_path`, normally
`/root/maestro-operator/broker_accounts.yaml` in deployment. Add or disable KIS
logical accounts there, and keep secret values in `/etc/maestro/maestro.env`.

Strategy-to-account routing for `symphony_signal` and `symphony_approval` is
centralized in `strategy_account_map_path`, normally
`/root/maestro-operator/strategy_accounts.yaml` in deployment. Change `enabled`, account mappings, and phase controls there, then validate the phase configs before the next scheduled signal run.
The mapping file participates in config fingerprint checks, so approval rejects
a signal package if the mapping changes between signal generation and approval.
Development-only strategies may remain outside this mapping. If the operator
wants them visible in Symphony, add them as explicit candidates with conservative
phase controls instead of routing them to a real account. Use `dev_sandbox` for
signal/approval UX rehearsal without broker API access, use `kis_mock` only when
the rehearsal intentionally targets the KIS mock-investment account, and map to
a real account only after evidence review.

Use this promotion ladder for strategy phase controls:

1. `enabled: false`, `readonly: true`, `signal: false`, `order_posture: disabled`: tracked as an operator candidate but not enabled.
2. `enabled: true`, `readonly: true`, `signal: false`, `order_posture: disabled`: visible in
   operator views only.
3. `enabled: true`, `readonly: true`, `signal: true`, `order_posture: disabled`: signal and
   order preview can be audited, but no Telegram approval request is created.
4. `enabled: true`, `readonly: true`, `signal: true`, `order_posture: dry_run`: Telegram approval
   UX is exercised, and approved orders are dry-run events only.
5. `enabled: true`, `readonly: true`, `signal: true`, `order_posture: armed`: approved orders can
   reach broker submit only when the global config is also armed.

The current shared mapping routes `tranquillo` through
`multi_account_contributions.tranquillo` for `kis_ps` and `kis_isa`, and routes
`crescendo_us -> toss_brokerage / crescendo_us` with manual bucket capacity
reserved in `account_strategy_targets`. `fugue -> dev_sandbox` is
`enabled: false`, `readonly: true`, and `signal: false`, so it can appear in
operator views without being imported or executed by `symphony_signal`.

Before the first Toss order, refresh the broker snapshot and inspect the
automatic attribution baseline:

```bash
maestro adopt-account-attribution \
  --config <approval-config> \
  --account-id toss_brokerage \
  --reason "operator verified initial Toss holdings"
```

The same adoption is available through `/attribution toss_brokerage` in the
Telegram operator. Do not approve a baseline with incorrectly assigned
strategy or manual quantities.

For the first armed Toss rollout, keep automated Toss instruments at integer
`quantity_step` and `min_order_quantity`. Fractional Toss orders are blocked by
live gates before approval or broker submission.

If a broker status poll returns an unknown order state, Maestro records
`live_order_recovery_required`; the next live approval run remains blocked until
operator recovery is completed.

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
defaults to refusing broker positions that are neither in
`portfolio.allowed_symbols` nor known `universe.instruments` allowed by
`universe.policy`. If `portfolio.unknown_broker_position_policy` is
`include_readonly`, already-held unknown broker positions are included for
read-only display/adoption, but they remain ineligible for target allocations
and live order execution until onboarded into the universe.

5. Reconcile Maestro state against the latest broker snapshot:

```bash
maestro reconcile --config <config>
```

For configs with multiple enabled KIS accounts, reconciliation compares each
account-scoped Maestro portfolio snapshot with that account's latest broker
snapshot, then reports the aggregate cash and position differences. It does not
compare a global Maestro portfolio against whichever broker snapshot happened
to be stored last.

6. If reconciliation fails, do not proceed to live approval. Review the reported
   cash and position differences, manual broker activity, fills, and the latest
   Maestro portfolio state.

For scheduled `live_approval run-once`, the KIS broker snapshot is refreshed and
adopted at the start of the run. If
`execution.require_reconciliation_pass=true`, the run immediately reconciles
that adopted Maestro state against the same saved broker snapshot without a
second KIS fetch. A mismatch records `broker_reconciliation` with
`passed=false`; the existing live approval gate then halts before Telegram
approval or order submission.

KIS current price data is broker reference data for checks and reconciliation.
Strategy research data must still come through Maestro DataHub.

## Tranquillo KIS Paper Pilot

Use `docs/tranquillo_kis_paper_pilot.md` for the Tranquillo domestic ETF promotion
from Maestro paper runs to the KIS mock-investment approval-gated broker-submit
path. The pilot scope is `kis_domestic_stock`, `kis.paper_trading=true`,
Telegram manual approval, limit orders only, and four scheduled trading-day
cycles. A real cash account and `live_auto` remain out of scope.
