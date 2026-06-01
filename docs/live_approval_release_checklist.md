# Live Approval Release Checklist

This checklist covers approval-gated live approval release and real-account
rehearsal. It is not a `live_auto` procedure.
Maestro still has no market orders, no direct buy/sell/cancel CLI, no dashboard
write controls, and no high-risk Telegram resume, clear-halt, live enablement,
direct trading, or risk-change controls.

The planned Symphony operator workflow will separate `symphony_readonly`,
`symphony_signal`, and `symphony_approval`. In that workflow, approval consumes
a persisted `signal_run_id` and does not re-run strategies. Until that workflow
is implemented, this checklist applies to the existing `live_approval run-once`
pipeline.

Maestro owns DataHub, execution, approval, broker adapters, state, audit, and
the read-only dashboard. Yahoo/yfinance, FRED, RSS feeds, KIS Open API, and
Telegram Bot API are external systems reached through Maestro adapters.
Virtuoso apps must request data through Maestro DataHub and must not call broker,
Telegram, or external data APIs directly.

## Promotion Path

Do not treat KIS network connectivity or a successful single submit as
production readiness. Promote one operator-local configuration through these
gates in order:

1. Run mock or CSV paper mode until the strategy and state path are stable.
2. Run real-data paper mode with the intended DataHub provider and symbols.
3. Run KIS multi-asset read-only sync and broker reconciliation against the real
   account.
4. Run a real Telegram approval rehearsal without broker submission.
5. Run `execution.order_posture=dry_run` through approval and lifecycle preflight.
6. Submit one minimum-size approval-gated limit order only after every prior
   gate passes.
7. Confirm broker status, fill reconciliation, broker reconciliation, audit
   events, and read-only dashboard state before repeated operation.

Normal automated tests remain fake-client and fixture based. Real KIS and
Telegram network checks must be explicit operator rehearsals, skipped by default
in test automation, and run only with operator-local config.

For the complete operator-local promotion runbook, see
`docs/live_account_promotion.md`.

## Preflight Checklist

- Review `configs/live_approval.yaml` and keep
  `execution.order_posture=disabled` until every item below passes.
- Confirm the strategy universe, DataHub provider, broker account, and effective
  allowed symbols refer to the same canonical symbols. If
  `portfolio.allowed_symbols` is omitted, Maestro derives it from
  `portfolio.currency_sleeves` or `universe.instruments`.
- Confirm `universe.instruments` maps each canonical symbol to the intended
  broker product, broker symbol, exchange code, currency, price tick, and
  quantity step.
- For the first production target, confirm US-listed stocks/ETFs use
  `broker_product=kis_overseas_stock`.
- Confirm `execution.allowed_order_type=limit`.
- Confirm `execution.require_reconciliation_pass=true`.
- Confirm per-order and daily live notional caps are small enough for the first
  operator-approved order.
- Confirm `execution.live_order_limits.max_daily_order_count` is small enough
  for the first operator-approved run.
- Enable `execution.market_session.required=true` only after the operator config
  has the intended venue timezone, open/close times, weekdays, and holiday list.
- Enable `execution.broker_validation.require_quote_validation=true` only after
  KIS read-only sync writes current prices for the intended order symbols.
- Enable `execution.broker_validation.require_risk_validation=true` only after
  the latest KIS read-only snapshot is reconciled and includes cash, buying
  power, current prices, positions, and unfilled orders.
- Confirm KIS overseas buy orders perform a pre-submit `/inquire-psamount`
  check with the exact limit price, and fail closed on insufficient buying power
  or max buy quantity.
- Set `execution.live_order_limits.fee_buffer_pct` to the operator's conservative
  commission/fee cushion before the first real order.
- Set `execution.live_order_limits.daily_loss_limit_by_currency` only after the
  operator has verified which normalized broker PnL field and currency each
  account snapshot provides.
- Set `monitoring.heartbeat_max_age_seconds` and
  `monitoring.scheduled_run_max_age_seconds` in operator deployments that run on a
  schedule.
- Confirm required DataHub price data is fresh before live approval.
- Confirm the dashboard remains read-only.
- Confirm no strategy plugin imports or calls KIS, Telegram, or other broker APIs
  directly.
- Confirm no strategy plugin calls Yahoo/yfinance, FRED, RSS, or other external
  research APIs directly; all research data must flow through Maestro DataHub.
- Confirm `var/` state, audit, and token cache paths are owner-controlled and not
  committed.

## Required Environment Variables

Set these in the operator environment, not in YAML and not in source control:

- `KIS_MOCK_APP_KEY`
- `KIS_MOCK_APP_SECRET`
- `KIS_ISA_ACCOUNT_ID`, `KIS_ISA_APP_KEY`, and `KIS_ISA_APP_SECRET` for ISA
  account routing when enabled.
- `KIS_BROKERAGE_ACCOUNT_ID`, `KIS_BROKERAGE_APP_KEY`, and
  `KIS_BROKERAGE_APP_SECRET` for brokerage account routing when enabled.
- `KIS_ACCESS_TOKEN` only when using a real externally issued token
- `KIS_APPROVAL_KEY` only when using a real externally issued WebSocket
  approval key
- `TELEGRAM_BOT_TOKEN`
- `MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS`
- `MAESTRO_TELEGRAM_WHITELISTED_USER_IDS`

If `KIS_ACCESS_TOKEN` is absent, the KIS adapter may use `kis.token_cache_path`
to cache an issued token. Keep operator secrets in `/etc/maestro/maestro.env`;
do not keep real or placeholder secret values in a repo-local `.env`. The token
cache must not be committed or copied into audit/state records.
If `KIS_APPROVAL_KEY` is absent, the KIS adapter can issue `/oauth2/Approval`
for future WebSocket sessions; treat that key as a secret with the same
no-audit/no-state rule.

## KIS Read-Only Sync

1. Start from a read-only config such as `configs/live_readonly.yaml` for the
   operator KIS skeleton, or a test fixture config for the
   deterministic mock KIS path.
2. Run `maestro kis-sync --config <readonly-config>`.
3. Run `maestro kis-account --config <readonly-config>`.
4. Confirm cash, positions, buying power, and account ID are expected.
5. If this is the first rehearsal against the account state, explicitly adopt
   the verified broker snapshot as Maestro's baseline:

```bash
maestro adopt-broker-snapshot --config <readonly-config> --reason "operator baseline accepted"
```

Do not proceed if the read-only account snapshot is missing, stale, points to
the wrong account, or does not use the intended broker product adapter.
Do not adopt the snapshot if it contains positions outside both
`portfolio.allowed_symbols` and approved `universe.instruments`, violates
`universe.policy`, or includes holdings the strategy is not meant to manage.

The KR+US read-only KIS fixture documents the intended KR+US
multi-product read-only shape. KIS read-only uses domestic and overseas account
endpoints for broker snapshots and reconciliation only; strategy market and
research data must still come through Maestro DataHub. Live approval uses
verified limit-order submit/status payloads behind the safety gates.
The domestic and overseas adapters share common REST plumbing, but endpoint
paths, request bodies, TR_IDs, cash currency, quote exchange-code mapping, and
response parsing remain product-specific. For a brokerage account that trades
both KRX and US-listed instruments, enable both `kis_domestic_stock` and
`kis_overseas_stock` and verify that each tradable instrument declares the
correct `broker_product`.

## Broker Reconciliation Pass

1. Run `maestro reconcile --config <readonly-config>`.
2. Confirm it exits successfully and writes a passing `broker_reconciliation`
   event.
3. Investigate every cash or position mismatch before live approval.

Live order submission is blocked when the latest reconciliation is missing,
failed, or older than `reconciliation.max_age_seconds`.

## Telegram Approval Test

1. Use a paper or fake-client path first.
2. Confirm the bot sends the approval request only to configured chat IDs.
3. Confirm only whitelisted users can approve or reject.
4. Confirm rejection and timeout skip execution.
5. Confirm lifecycle notifications are sent for submit, status, fill, halt, and
   failure transitions.

Telegram must not expose commands to bypass risk limits, enable live orders,
disable reconciliation, place market orders, or call cancel directly.

Operator UI safety checklist:

- Confirm Telegram command handling enforces the configured user whitelist.
- Confirm account IDs are masked in Telegram responses.
- Confirm read-only commands use Maestro state/read models or the latest stored
  broker snapshot and do not call broker network endpoints.
- Confirm `/pause` and `/kill_switch` require confirmation callbacks before
  changing safety state.
- Confirm recovery commands such as `resume`, `clear-halt`, broker sync,
  reconciliation triggers, live enablement, dry-run disablement, and risk
  changes remain CLI/runbook only.

## Dry-Run / Fake-Client Test Path

Normal tests are fake-client and fixture based. Before a real broker submit,
run:

```bash
ruff check .
ruff format --check .
pytest -q
maestro run-once --config configs/paper.yaml
maestro status --config configs/paper.yaml
```

For live approval behavior, use injected fake KIS and Telegram clients in tests.
Do not add normal tests that call KIS or Telegram network endpoints.

Before enabling live orders, run:

```bash
maestro health --config <live-approval-config>
```

Confirm `live_approval_preflight` is `ok`. Do not proceed when it reports
missing reconciliation requirements, non-Telegram approval, missing Telegram
chat/user allowlists, missing KIS overseas broker product config, unsupported
exchange codes, or missing live notional caps.

For the final rehearsal before broker submission, set
`execution.order_posture=dry_run` in the operator-local config and run:

```bash
maestro run-once --config <live-approval-config>
```

Confirm the approval path completes and `live_order_dry_run` events contain the
expected symbol, side, quantity, limit price, and notional. Then set
`execution.order_posture=armed` only after the dry-run payload matches the
intended first order.

Review the matching `live_proposal_data_snapshot` event before approval. It
records the DataHub requests, price basis, data quality issues, risk decision,
and proposed orders used to create the live approval proposal.

If real KIS responses are captured for parser fixtures, redact them using
`docs/kis_fixture_redaction.md` before storing them anywhere in the repository.

## Live Network Smoke Checks

Run these only from an operator environment with real credentials and an
operator-local config:

- KIS read-only account snapshot: cash, positions, buying power, fills, unfilled
  orders, and account ID must match the broker UI.
- Broker reconciliation: the latest `broker_reconciliation` event must pass and
  be younger than `reconciliation.max_age_seconds`.
- Telegram approval: only allowed chat IDs receive proposals, only whitelisted
  users can approve, and rejection/timeout skip execution.
- Telegram operator UI: command whitelist is enforced, account IDs are masked,
  read-only commands avoid broker network calls, `/pause` and `/kill_switch`
  require confirmation, and recovery commands remain CLI/runbook only.
- Live dry-run: `live_order_dry_run` must show the exact symbol, side, quantity,
  limit price, notional, approval ID, and broker product expected for the first
  order.
- First live order: use the smallest practical notional and order count caps,
  then verify `live_order_result`, `live_order_status`,
  `fill_reconciliation`, `broker_reconciliation`, and
  `live_order_lifecycle` events.

The KIS read-only smoke gate is available as:

```bash
maestro live-smoke --config <operator-readonly-config> --check kis-readonly
```

For normal tests this same gate is covered with a mock provider by passing
`--allow-mock`. The real-network pytest smoke is skipped unless
`MAESTRO_RUN_KIS_LIVE_SMOKE=1` and `MAESTRO_KIS_LIVE_CONFIG` are set.

The Telegram approval smoke gate sends a non-approval smoke message to the
configured Telegram chats:

```bash
maestro live-smoke --config <operator-live-approval-config> --check telegram-approval
```

For normal tests this gate validates a Telegram-configured approval path with
`--allow-mock`. The real-network pytest smoke is skipped unless
`MAESTRO_RUN_TELEGRAM_LIVE_SMOKE=1` and `MAESTRO_TELEGRAM_LIVE_CONFIG` are set.

The live approval dry-run smoke gate runs `run_once`, requires
`execution.order_posture=dry_run`, and verifies that `live_order_dry_run`
events were written without broker submission:

```bash
maestro live-smoke --config <operator-live-approval-config> --check live-dry-run
```

For normal tests this gate can run with console/mock approval by passing
`--allow-mock`. The operator pytest smoke is skipped unless
`MAESTRO_RUN_LIVE_DRY_RUN_SMOKE=1` and `MAESTRO_LIVE_DRY_RUN_CONFIG` are set.

## Live Approval Submit Path

1. Copy `configs/live_approval.yaml` to an operator-local config.
2. Replace placeholder account ID, chat IDs, symbols, strategy configuration, and
   state/audit paths.
3. Keep `execution.order_posture=disabled` and run config validation/tests.
4. Run KIS read-only sync, inspect the account, and adopt the verified broker
   snapshot as the live baseline.
5. Run broker reconciliation against the adopted baseline.
6. Confirm Telegram approval works with the intended operator account.
7. Set small live notional caps.
8. Set `execution.order_posture=dry_run` and run one approved dry-run.
9. Only after all checks pass, set `execution.order_posture=armed` in the
   operator-local config.
10. Run `maestro run-once --config <live-approval-config>`.
11. Approve only if the proposal, limit price, notional, and symbol are expected.

`portfolio.initial_cash` is intentionally absent from live configs. `run-once`
must use the adopted KIS broker snapshot as its cash/position baseline.

There is no direct or unguarded buy/sell command. The only live submit path is
approval-gated `run_once` through `LiveOrderSafetyService` and the bounded
`LiveOrderLifecycleService`.

## Status Polling Expectations

- The lifecycle polls until a terminal status or
  `execution.order_status_max_polls`.
- Terminal statuses are filled, rejected, canceled, halted, and failed.
- Unknown broker status is converted to halted and must not continue to fills.
- Reaching max polls is non-terminal and does not auto-cancel.
- Status snapshots are persisted as `live_order_status` system and audit events.

## Fill Reconciliation Checks

- Confirm `fill_reconciliation` events are written after fill-bearing status
  snapshots.
- Confirm only new cumulative fill deltas are applied.
- Confirm cash and positions match expected fill quantity and average price.
- Confirm rejected, canceled, halted, and unknown states do not update the
  portfolio.
- Run broker reconciliation again after fills update the portfolio.

## Halt / Failure Handling

- Halt on unknown submit result or unknown broker status.
- Treat broker submit timeouts and ambiguous transport failures as unresolved
  until broker state is checked through read-only KIS queries. Maestro records
  `live_order_recovery_required` for these paths.
- If the process dies after broker submit or before lifecycle persistence
  completes, the next live approval proposal is blocked by
  `live_order_recovery_halt` until broker order/fill truth is reconstructed.
- Halt on stale required DataHub data before approval/lifecycle execution.
- Halt on missing, stale, or failed broker reconciliation when reconciliation is
  required.
- Halt when daily live notional or order count caps would be exceeded.
- Halt when a proposed live order violates `universe.instruments` precision,
  minimum, currency, or broker product constraints.
- Halt when broker risk validation detects insufficient buying power, negative
  post-order cash, pending broker orders, or unreconciled broker snapshot/manual
  broker activity.
- Halt when `execution.live_order_limits.daily_loss_limit_by_currency` is exceeded or broker
  PnL is unavailable while the limit is configured.
- Halt or fail when broker reconciliation fails after a fill update.
- Do not retry blindly after any halt/failure.
- Preserve state and audit files for review.
- Do not clear a halt after manual broker intervention until KIS read-only sync,
  broker reconciliation, and fill reconciliation have been rerun.
- Run `maestro ops-alerts --config <live-approval-config>` from the operator
  alerting path so halt/failure/stale/reconciliation/heartbeat health failures
  reach the configured Telegram approval chats.
- Inspect `safety_state`, `safety_execution_blocked`, `stale_data_halt`,
  `broker_reconciliation_halt`, `live_order_limit_halt`,
  `instrument_validation_halt`, `broker_risk_halt`, `live_order_result`,
  `live_order_status`, `fill_reconciliation`, `live_order_lifecycle`,
  `live_order_recovery_required`, `live_order_recovery_halt`, and
  `live_order_recovery_completed` events before the next run.

## Halt Recovery

1. Keep `execution.order_posture=disabled` until the root cause is resolved.
2. Review the halt event, audit log, broker account state, latest reconciliation,
   and affected canonical symbols.
3. Run read-only sync and broker reconciliation again.
4. Run fill reconciliation:

```bash
maestro reconcile-fills --config <live-approval-config>
```

5. Record live order recovery completion:

```bash
maestro recover-live-order --config <live-approval-config> --reason "<broker truth reconciled>"
```

6. Confirm DataHub freshness, universe mappings, precision rules, daily limits,
   and operator approval path.
7. Clear only a `halted` state with:

```bash
maestro clear-halt --config <live-approval-config> --reason "<root cause fixed>"
```

`resume` does not clear halted state. `clear-halt` and `resume` do not clear a
killed state.

## Monitoring / Audit Checks

Run these from the operator scheduler or deployment monitor:

```bash
maestro heartbeat --config <live-approval-config>
maestro health --config <live-approval-config>
maestro ops-alerts --config <live-approval-config>
maestro beta-preflight --config <live-approval-config>
```

Health verifies heartbeat age, scheduled `run-once` age, broker snapshot age,
reconciliation status, recent halt/failure events, and audit hash-chain
integrity. `ops-alerts` sends warn/fail health checks to Telegram; use
`--allow-mock` only for local rehearsal. `beta-preflight` is the final
private-beta readiness gate for an operator-local live approval config. It fails
if the config still uses mock DataHub or KIS mock-investment
`kis.paper_trading=true`.

## Rollback / Stop Procedure

1. Set `execution.order_posture=disabled` in the operator-local config.
2. Stop scheduled invocations of `maestro run-once`.
3. Leave the dashboard read-only.
4. Use broker-native tools for emergency broker-side action.
5. Run `maestro kis-sync`, `maestro kis-account`, `maestro reconcile`, and
   `maestro reconcile-fills` after any manual broker action.
6. Use Maestro live cancel only through `LiveOrderCancellationService` after
   Telegram approval, latest open or partial-fill status, remaining quantity,
   and a passing broker reconciliation are confirmed. There is still no direct
   cancel CLI.
