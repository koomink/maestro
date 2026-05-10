# Live Approval Release Checklist

This checklist closes v0.6 `live_approval`. It is not a `live_auto` procedure.
Maestro still has no market orders, no direct buy/sell/cancel CLI, no dashboard
write controls, and no high-risk Telegram admin controls.

Maestro owns DataHub, execution, approval, broker adapters, state, audit, and
the read-only dashboard. Yahoo/yfinance, FRED, RSS feeds, KIS Open API, and
Telegram Bot API are external systems reached through Maestro adapters.
Virtuoso apps must request data through Maestro DataHub and must not call broker,
Telegram, or external data APIs directly.

## Preflight Checklist

- Review `configs/live_approval.example.yaml` and keep
  `execution.live_order_enabled=false` until every item below passes.
- Confirm the strategy universe, DataHub provider, broker account, and
  `portfolio.allowed_symbols` refer to the same canonical symbols.
- Confirm `universe.instruments` maps each canonical symbol to the intended
  broker product, broker symbol, exchange code, currency, price tick, and
  quantity step.
- For the first production target, confirm US-listed stocks/ETFs use
  `broker_product=kis_overseas_stock`.
- Confirm `execution.allowed_order_type=limit`.
- Confirm `execution.require_reconciliation_pass=true`.
- Confirm per-order and daily live notional caps are small enough for the first
  operator-approved order.
- Confirm `execution.max_daily_live_order_count` is small enough for the first
  operator-approved run.
- Leave `execution.daily_loss_limit` unset until broker PnL normalization is
  implemented for the configured overseas stock/ETF broker product.
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

- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- `KIS_ACCESS_TOKEN` when using an externally issued token
- `TELEGRAM_BOT_TOKEN`

If `KIS_ACCESS_TOKEN` is absent, the KIS adapter may use `kis.token_cache_path`
to cache an issued token. The token cache must not be committed or copied into
audit/state records.

## KIS Read-Only Sync

1. Start from a read-only config such as `configs/live_readonly.yaml` for the
   deterministic mock KIS path.
2. Run `maestro kis-sync --config <readonly-config>`.
3. Run `maestro kis-account --config <readonly-config>`.
4. Confirm cash, positions, buying power, and account ID are expected.

Do not proceed if the read-only account snapshot is missing, stale, points to
the wrong account, or does not use the intended broker product adapter.

`configs/kis_live_readonly.example.yaml` documents the intended
`kis_overseas_stock` shape, but real KIS overseas read-only remains fail-closed
until endpoint paths, TR_IDs, exchange codes, pagination, and response fields are
verified and implemented.

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

## Live Approval Submit Path

1. Copy `configs/live_approval.example.yaml` to an operator-local config.
2. Replace placeholder account ID, chat IDs, symbols, strategy configuration, and
   state/audit paths.
3. Keep `execution.live_order_enabled=false` and run config validation/tests.
4. Run KIS read-only sync and broker reconciliation.
5. Confirm Telegram approval works with the intended operator account.
6. Set small live notional caps.
7. Only after all checks pass, set `execution.live_order_enabled=true` in the
   operator-local config.
8. Run `maestro run-once --config <live-approval-config>`.
9. Approve only if the proposal, limit price, notional, and symbol are expected.

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
- Halt on stale required DataHub data before approval/lifecycle execution.
- Halt on missing, stale, or failed broker reconciliation when reconciliation is
  required.
- Halt when daily live notional or order count caps would be exceeded.
- Halt when a proposed live order violates `universe.instruments` precision,
  minimum, currency, or broker product constraints.
- Halt when `execution.daily_loss_limit` is configured before broker PnL
  normalization exists.
- Halt or fail when broker reconciliation fails after a fill update.
- Do not retry blindly after any halt/failure.
- Preserve state and audit files for review.
- Inspect `safety_state`, `safety_execution_blocked`, `stale_data_halt`,
  `broker_reconciliation_halt`, `live_order_limit_halt`,
  `instrument_validation_halt`, `live_order_result`, `live_order_status`,
  `fill_reconciliation`, and `live_order_lifecycle` events before the next run.

## Halt Recovery

1. Keep `execution.live_order_enabled=false` until the root cause is resolved.
2. Review the halt event, audit log, broker account state, latest reconciliation,
   and affected canonical symbols.
3. Run read-only sync and broker reconciliation again.
4. Confirm DataHub freshness, universe mappings, precision rules, daily limits,
   and operator approval path.
5. Clear only a `halted` state with:

```bash
maestro clear-halt --config <live-approval-config> --reason "<root cause fixed>"
```

`resume` does not clear halted state. `clear-halt` and `resume` do not clear a
killed state.

## Rollback / Stop Procedure

1. Set `execution.live_order_enabled=false` in the operator-local config.
2. Stop scheduled invocations of `maestro run-once`.
3. Leave the dashboard read-only.
4. Use broker-native tools for emergency broker-side action.
5. Run `maestro kis-sync`, `maestro kis-account`, `maestro reconcile`, and
   `maestro reconcile-fills` after any manual broker action.
6. Do not use Maestro for live cancel until the KIS cancel endpoint path, TR_IDs,
   and request fields are verified and implemented behind
   `LiveOrderCancellationService`.
