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

Use Telegram `/recovery` or the recovery button in the automatic halt alert.
Maestro first queries broker order history, reconciles any recovered fills,
forces fresh snapshots, and requires account reconciliation to pass. A Toss
order without a returned broker order ID is matched only when account, symbol,
side, quantity, limit price, and submit time (within five minutes) identify one
unique OPEN/CLOSED order. If no unique match exists, the final Telegram button
is an explicit attestation that the operator verified no acceptance or fill in
the broker app.

The CLI remains available as a fallback:

```bash
maestro recover-live-order --config <config> --reason "broker truth reconciled"
```

If an order was excluded before approval, review the
`live_order_capacity_blocked` event and Telegram's planned/available quantity.
Tap `재주문 검토` in the failure alert or `/orders`, then choose the original
quantity, the freshly calculated maximum quantity, or `직접 수량 입력`. Direct
input must be sent as a reply to Maestro's quantity prompt within 10 minutes.
The original-quantity choice can still be rejected if current capacity is lower.
The typed fallback is:

```text
/retry_order <blocked_order_id> <quantity> [price]
```

This creates a new order ID and approval and re-runs capacity, market-session,
quote, reconciliation, and order-limit gates. Contribution orders can be
recovered through their contribution month; ordinary rebalances remain
same-trading-day only. Pre-broker failures and expired approvals appear in
`/orders` under `Recoverable orders`. Do not treat the blocked order itself as a
completed contribution.

KIS application errors with a returned response code, such as `APBK1497`, are
recorded as definitive `rejected` orders. Network timeouts and malformed
responses remain `halted` and require operator recovery. When an operator
approves `/retry_order`, the source contribution order is superseded; the new
order's result becomes the monthly contribution idempotency source of truth.
The latest quote used by recovery review is rounded down to the instrument's
configured `price_tick` before quantity, notional, buying power, and live gates
are recalculated.

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

Telegram `/clear_halt` and the Recovery Center's `Safety halt 해제` button run
the same health preflight as the CLI and refuse to clear the halt while any
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

Toss order submission errors use the following fail-closed classification:

| Toss outcome | Maestro result | Batch behavior |
| --- | --- | --- |
| HTTP `400`, `401`, `403`, `404`, `422`, or `429` | definitive `rejected`; preserve status, Toss error code, request ID, and error data when returned | isolate that symbol and continue sequential submission |
| HTTP `409` or `5xx` | ambiguous `halted` with `live_order_recovery_required` | stop later submissions |
| Network timeout/disconnect or response parse failure | ambiguous `halted` with `live_order_recovery_required` | stop later submissions |

For example, `422 prerequisite-required` means Toss did not accept that order.
Only the affected symbol becomes a recovery-review candidate; it does not halt
the remaining batch. Do not infer the same for `409`, server failures, or a
missing/unreadable response, because broker acceptance cannot be ruled out.

## Cash flow accounting

Every `account_cash_flow` carries a `flow_class` saying what the money was.
This decides whether performance treats it as investor money crossing a
boundary or as the portfolio acting on its own cash:

| `flow_class` | Removed from account/portfolio return | Removed from a currency sleeve return |
| --- | --- | --- |
| `external_transfer` | yes | yes |
| `internal_transfer` | account only | yes |
| `fx_conversion` | no | yes |
| `investment_income` | no | no |
| `cost` | no | no |

Classify a dividend, interest payment, tax or fee as such, never as a deposit
or withdrawal. A dividend recorded as a deposit is removed from the return, so
the account silently reports having earned less than it did; a fee recorded as
a withdrawal reports more.

A conversion crosses no account and no portfolio boundary — the total does not
move — but it is exactly what moves one currency sleeve into another, so only a
sleeve return removes it.

Flows recorded before `flow_class` existed are read as `external_transfer`,
which is what they were recorded as.

Set the class with `--flow-class` on `cash-flow record`; it defaults to
`external_transfer`. A dividend the broker paid in cash:

```bash
maestro cash-flow record --config <approval-config> \
  --account-id kis_brokerage --amount 12500 --currency KRW \
  --flow-type deposit --flow-class investment_income \
  --reason "005930 dividend"
```

### Linked flows

`internal_transfer` and `fx_conversion` only mean anything as a pair. Both legs
must share one `--transfer-id`, and each leg is keyed separately, so the two
sides of a move between accounts take two `cash-flow record` calls:

```bash
maestro cash-flow record --config <approval-config> \
  --account-id kis_brokerage --amount 250000 --currency KRW \
  --flow-type withdrawal --flow-class internal_transfer \
  --transfer-id move-2026-08-02 \
  --reason "move to Toss for US ETF purchase"
maestro cash-flow record --config <approval-config> \
  --account-id toss_brokerage --amount 250000 --currency KRW \
  --flow-type deposit --flow-class internal_transfer \
  --transfer-id move-2026-08-02 \
  --reason "move from KIS for US ETF purchase"
```

A linked class without `--transfer-id` is refused: a leg with no id can never
be paired, so it could only ever be reported as incomplete. A leg whose
counterpart is missing is not counted either, and the read models report
`unpaired_linked_cash_flow` in `cash_flow_quality` rather than publishing a
figure built on half an event. Check that field after recording a transfer.

`cash-flow record` will not take `fx_conversion`; a conversion is two legs
whose amounts have to agree, so use `cash-flow convert` below.

### Currency conversion

Record a conversion with `cash-flow convert`. `--to-amount` is what actually
arrived and `--fee` is the spread or commission that did not; they are booked
apart so the cost stays visible after the sleeve removes the conversion itself:

```bash
maestro cash-flow convert --config <approval-config> \
  --account-id toss_brokerage \
  --from-currency KRW --from-amount 1400000 \
  --to-currency USD --to-amount 995 --fee 5 \
  --transfer-id fx-2026-08-02 \
  --reason "convert KRW for US ETF purchase"
```

`--transfer-id` is required: it is what makes a repeated run a no-op instead of
converting twice. Pass `--rate` (target-currency units per source-currency
unit) to have the amounts cross-checked before anything reaches the ledger.

The candidate detector deliberately offers nothing when two currencies move in
opposite directions over the same window. That is a conversion, and confirming
either side alone would record a deposit or withdrawal for an account whose
money never left. Record it with `cash-flow convert` instead.

### Candidate detection

Every broker's cash changes go through one detector, so the evidence required
is the same whether the figure is Toss buying power or a broker-reported
deposit balance. A change is offered for confirmation only when:

- the new level holds across three consecutive snapshots
- positions, open orders and fills are unchanged across the window
- no order lifecycle event falls inside the window
- no observed fill is within three days of the latest snapshot, since Korean
  equities settle T+2 and US equities T+1, and settlement is the account's own
  trading catching up rather than money arriving
- the change is at least KRW 1,000 or USD 1
- no second currency moved the opposite way

What still differs by broker is what the confirmation means, not what evidence
is required. Toss cash is a proxy the operator checks in the app; a
broker-reported figure is the broker's own number. KIS cannot do better than
this: it publishes no endpoint that says why a stock account's cash moved. See
`docs/kis_cash_transaction_api_survey.md`.

## Toss cash ledger operations

Toss `cashBuyingPower` is used only for current order capacity. It is not
adopted as settled cash on every refresh. Establish the one-time account ledger
after reviewing the broker snapshot:

```bash
maestro ledger open-baseline \
  --config <approval-config> \
  --account-id toss_brokerage \
  --reason "operator verified opening cash and positions"
```

Inspect subsequent buying-power drift without changing state:

```bash
maestro cash-drift report --config <approval-config> \
  --account-id toss_brokerage --days 7
```

The Telegram operator's `/cash_drift` command shows the same suspense rows.
Classifying an observation never writes a cash flow or alters the ledger; it
records what the operator believes caused the difference. The available
classifications and the flow class each implies:

| Classification | Implies |
| --- | --- |
| `settlement_candidate` | nothing — a timing difference, not a flow |
| `unexplained` | nothing — blocks `--include-cash` adoption |
| `transfer_candidate` | `external_transfer` |
| `dividend`, `interest` | `investment_income` |
| `tax`, `fee` | `cost` |
| `fx_conversion` | `fx_conversion` |

Classify by cause, not by convenience. `settlement_candidate` is for a broker
that has not caught up with a fill Maestro already booked — the open
`toss_brokerage` USD suspense from the 2026-08-02 QQQ disposal is one — and it
resolves itself once settlement lands.

Normal Toss refreshes backfill OPEN/CLOSED orders automatically and fail closed
before reconciliation when history cannot be verified. Use this command only
for an explicit historical repair; baseline/adoption cutoffs prevent replay of
already represented principal, positions, and costs:

```bash
maestro ledger backfill-orders --config <approval-config> \
  --account-id toss_brokerage --from-date YYYY-MM-DD
```

Record an operator-verified deposit or withdrawal through `cash-flow record`,
setting `--flow-class` per "Cash flow accounting" above. It advances the
account ledger and emits the audited flow the return is built from.

`adopt-broker-snapshot` preserves ledger cash by default and adopts positions;
use `--include-cash` only after a non-unexplained classification for the latest
broker snapshot. Adopting cash moves the ledger without writing a cash flow, so
the change is never removed from the return — right for a dividend the broker
reports and Maestro has not booked, wrong for anything that is really an
external transfer. The adoption event records `ledger_effect`,
`performance_effect` and the classification it relied on, so a later reader can
tell an earned amount from a bookkeeping correction.
Before a live run, confirm the latest reconciliation and that current Toss
buying power covers each order; buying-power drift alone is observational and
must not be described as investment return.

The Telegram operator offers a Toss cash-flow candidate only when the evidence
in "Candidate detection" above holds. Confirm the one-click amount only after
checking the Toss app. Use
`/cash_flow <proposal_id> <actual_amount>` when it differs, or reject the
candidate to leave the ledger unchanged. Confirmed candidates are recorded as
`operator_verified`; Toss still has no broker-verified cash endpoint.

That confirmation is evidence about the moment it was made, not a standing
guarantee. A later snapshot reads `operator_verified` only while the buying
power has not moved since; once it moves the snapshot reads `checkpoint_stale`
until the next confirmation. Staleness follows account activity, not elapsed
time, so a quiet account does not go stale on its own.

Portfolio-level `broker_cash_verification` reports the weakest account rather
than "mixed": one account whose cash cannot be verified means the total cannot
be either. `broker_cash_verification_counts` carries the per-account breakdown.

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
