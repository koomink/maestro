# Personal Operator MVP

This guide turns Maestro into a single-user operator toolkit. It keeps live
orders approval-gated and does not add live auto-trading, market orders, direct
broker trading CLI commands, dashboard write controls, or Telegram admin
controls.

## Create Local Config

Create an operator-local config outside source control:

```bash
maestro init-personal --output ~/maestro-operator/maestro_personal.yaml
```

The generated config is safe by default:

- `mode=live_approval`
- `execution.live_order_enabled=false`
- `execution.live_order_dry_run=true`
- `execution.allowed_order_type=limit`
- small per-order and daily notional caps
- KIS overseas stock/ETF broker product
- Telegram approval required
- state, audit, and token cache paths under the config directory

Edit the generated config before any rehearsal:

- Set real Telegram chat IDs and whitelisted user IDs.
- Confirm the allowed symbols and `universe.instruments` match the intended
  account and strategy.
- Keep secrets in environment variables only.

Required environment variables:

```bash
export KIS_ACCOUNT_ID=...
export KIS_APP_KEY=...
export KIS_APP_SECRET=...
export TELEGRAM_BOT_TOKEN=...
```

`KIS_ACCESS_TOKEN` is optional. If it is absent, Maestro can use the configured
token cache path.

## Readiness Check

Run one local readiness summary:

```bash
maestro personal-check --config ~/maestro-operator/maestro_personal.yaml
```

The output reports these stages:

- `paper_ready`: config, state, audit, and DataHub checks are usable.
- `readonly_ready`: KIS env, broker snapshot, and reconciliation are ready.
- `telegram_ready`: Telegram approval config and token are ready.
- `dry_run_ready`: approval-gated dry-run config is ready.
- `minimum_live_ready`: private beta gate is ready for one minimum-size
  approval-gated live order.

`personal-check` does not call broker submit endpoints and does not send
Telegram messages. Use the `next="..."` command printed for the first failing
stage.

## Daily Operating Loop

Use the same order every day:

```bash
maestro heartbeat --config ~/maestro-operator/maestro_personal.yaml
maestro health --config ~/maestro-operator/maestro_personal.yaml
maestro kis-sync --config ~/maestro-operator/maestro_personal.yaml
maestro reconcile --config ~/maestro-operator/maestro_personal.yaml
maestro live-smoke --config ~/maestro-operator/maestro_personal.yaml --check telegram-approval
maestro live-smoke --config ~/maestro-operator/maestro_personal.yaml --check live-dry-run
```

Review the dry-run order, audit log, broker UI, and read-only dashboard before
changing `execution.live_order_enabled` or `execution.live_order_dry_run`.

## First Minimum-size Live Order

Only after `personal-check` reports `minimum_live_ready status=ok`:

1. Keep `execution.allowed_order_type=limit`.
2. Set the smallest practical `max_live_order_notional`,
   `max_daily_live_notional`, and `max_daily_live_order_count`.
3. Set `execution.live_order_dry_run=false`.
4. Set `execution.live_order_enabled=true`.
5. Run `maestro beta-preflight --config <operator-config>`.
6. Run one `maestro run-once --config <operator-config>`.
7. Approve only the exact expected Telegram proposal.
8. Stop scheduled runs after the first order.

After the order, verify broker status, fill reconciliation, broker
reconciliation, audit events, dashboard state, and the broker UI before any
repeated operation.

## Recovery

If Maestro records a halt, unknown order status, reconciliation failure,
ambiguous submit, or recovery-required event, stop scheduled jobs and follow
`docs/operator_runbook.md`. Do not place another live approval order until
read-only sync, broker reconciliation, fill reconciliation, and
`maestro recover-live-order --reason "<...>"` complete successfully.
