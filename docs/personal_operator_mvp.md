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
- `execution.order_posture=dry_run`
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

Required operator environment values live in `/etc/maestro/maestro.env` on the
VPS:

```bash
KIS_MOCK_ACCOUNT_ID=...
KIS_MOCK_APP_KEY=...
KIS_MOCK_APP_SECRET=...
TELEGRAM_BOT_TOKEN=...
```

That file is the operator environment source of truth used by systemd. For
manual CLI runs from the repository root, load it before invoking Maestro:

```bash
set -a
. /etc/maestro/maestro.env
set +a
.venv/bin/maestro ...
```

Maestro CLI commands still load a repo-local `.env` when one exists, without
overriding variables already set by the shell. Use that only for isolated local
development, not for VPS operator secrets.

`KIS_ACCESS_TOKEN` is optional. Leave it unset unless you have a real pre-issued
token. If it is absent, Maestro can use the configured token cache path.
`KIS_APPROVAL_KEY` is also optional and is needed only when using a pre-issued
KIS WebSocket approval key; otherwise Maestro can issue `/oauth2/Approval` when
a future WebSocket path needs it.

## Readiness Check

Run one local readiness summary:

```bash
maestro operator-evidence --config ~/maestro-operator/maestro_personal.yaml --output ~/maestro-operator/evidence-before.json
maestro personal-check --config ~/maestro-operator/maestro_personal.yaml
```

The output reports these stages:

- `paper_ready`: config, state, audit, and DataHub checks are usable.
- `readonly_ready`: KIS env, broker snapshot, and reconciliation are ready.
- `telegram_ready`: Telegram approval config and token are ready.
- `dry_run_ready`: approval-gated dry-run config is ready.
- `minimum_live_ready`: private beta gate is ready for one minimum-size
  approval-gated live order.

`operator-evidence` and `personal-check` do not call broker submit endpoints,
do not send Telegram messages, and do not run strategies. `operator-evidence`
stores a JSON snapshot of readiness stages, health checks, latest broker and
reconciliation state, latest approval/proposal/dry-run events, lifecycle
events, fill reconciliation, and recovery markers. Use the `next="..."`
command printed for the first failing stage.

## Daily Operating Loop

Use the same order every day:

```bash
maestro heartbeat --config ~/maestro-operator/maestro_personal.yaml
maestro health --config ~/maestro-operator/maestro_personal.yaml
maestro kis-sync --config ~/maestro-operator/maestro_personal.yaml
maestro reconcile --config ~/maestro-operator/maestro_personal.yaml
maestro live-smoke --config ~/maestro-operator/maestro_personal.yaml --check telegram-approval
systemctl stop maestro-telegram-operator.service
maestro live-smoke --config ~/maestro-operator/maestro_personal.yaml --check live-dry-run
systemctl restart maestro-telegram-operator.service
maestro operator-evidence --config ~/maestro-operator/maestro_personal.yaml --output ~/maestro-operator/evidence-after.json
```

The polling Telegram operator service and approval polling cannot use the same
bot token at the same time. Stop `maestro-telegram-operator.service` before
approval-gated dry-run or `run-once` rehearsals, then restart it afterward.

For the first R1 rehearsal on a verified broker baseline, adopt the latest
read-only KIS snapshot before reconciliation:

```bash
maestro adopt-broker-snapshot --config ~/maestro-operator/maestro_personal.yaml --reason "operator baseline accepted"
maestro reconcile --config ~/maestro-operator/maestro_personal.yaml
```

Review the dry-run order, audit log, broker UI, and read-only dashboard before
changing `execution.order_posture` to `armed`.

## First Minimum-size Live Order

Only after `personal-check` reports `minimum_live_ready status=ok`:

1. Keep `execution.allowed_order_type=limit`.
2. Set the smallest practical
   `execution.live_order_limits.max_order_notional_by_currency`,
   `execution.live_order_limits.max_daily_notional_by_currency`, and
   `execution.live_order_limits.max_daily_order_count`.
3. Set `execution.order_posture=armed`.
4. Run `maestro beta-preflight --config <operator-config>`.
5. Stop `maestro-telegram-operator.service` if it uses the same Telegram bot.
6. Run one `maestro run-once --config <operator-config>`.
7. Approve only the exact expected Telegram proposal.
8. Restart `maestro-telegram-operator.service`.
10. Stop scheduled runs after the first order.

After the order, verify broker status, fill reconciliation, broker
reconciliation, audit events, dashboard state, and the broker UI before any
repeated operation.

## Recovery

If Maestro records a halt, unknown order status, reconciliation failure,
ambiguous submit, or recovery-required event, stop scheduled jobs and follow
`docs/operator_runbook.md`. Do not place another live approval order until
read-only sync, broker reconciliation, fill reconciliation, and
`maestro recover-live-order --reason "<...>"` complete successfully.
