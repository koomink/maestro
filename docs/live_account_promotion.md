# Live Account Promotion

This runbook promotes one operator-local configuration from safe paper runs to
limited approval-gated real-account operation. It does not authorize
`live_auto`, market orders, direct broker CLI trading, dashboard write controls,
or Telegram admin controls.

## Required Order

1. Mock paper: run the strategy with deterministic local data until state,
   orders, risk decisions, and audit logs are stable.
2. Real-data paper: run the same strategy with the intended DataHub provider and
   canonical symbols.
3. KIS read-only: run real account sync and broker reconciliation with an
   operator-local read-only config.
4. Telegram approval smoke: verify the real bot can reach only the configured
   chats and requires whitelisted users for approvals.
5. Live dry-run: set `execution.live_order_dry_run=true`, run approval-gated
   `run_once`, and inspect `live_order_dry_run` plus
   `live_proposal_data_snapshot` events.
6. First minimum-size order: set the smallest practical per-order and daily caps,
   keep limit orders only, approve one expected order, and stop.
7. Limited repeated operation: continue only after post-order broker status,
   fill reconciliation, broker reconciliation, audit review, and dashboard review
   pass.

## Operator-local Config Checklist

- Copy an example config outside source control and use operator-owned state,
  audit, and token-cache paths.
- Keep secrets in environment variables only: KIS app key, KIS app secret,
  optional KIS access token, and Telegram bot token.
- Confirm `portfolio.allowed_symbols`, `universe.instruments`, DataHub symbol
  maps, and KIS broker symbols describe the same intended instruments.
- Keep `execution.live_order_enabled=false` until read-only sync,
  reconciliation, Telegram smoke, and live dry-run all pass.
- Use small `max_live_order_notional`, `max_daily_live_notional`, and
  `max_daily_live_order_count` values for the first order.
- Enable market-session and broker-quote validation in the operator config once
  the required market calendar and broker snapshot flow are verified.
- Enable broker-risk validation only after the latest broker snapshot is
  reconciled; confirm buying power, cash reserve, fee buffer, pending orders,
  exposure limits, and daily PnL source before first live submission.

## First Minimum-size Order Checklist

- Run `maestro operator-evidence --config <operator-live-approval-config> --output <evidence-before.json>`.
- Run `maestro live-preflight --config <operator-live-approval-config>`.
- Run `maestro live-smoke --config <operator-readonly-config> --check kis-readonly`.
- Run `maestro live-smoke --config <operator-live-approval-config> --check telegram-approval`.
- Stop `maestro-telegram-operator.service` if it uses the same Telegram bot
  token as approval polling.
- Run `maestro live-smoke --config <operator-live-approval-config> --check live-dry-run`.
- Restart `maestro-telegram-operator.service`.
- Run `maestro operator-evidence --config <operator-live-approval-config> --output <evidence-after.json>`.
- Review `live_proposal_data_snapshot` and confirm symbol, side, limit price,
  quantity, notional, DataHub price basis, and broker product.
- Enable live submission only for the intended order, approve only the expected
  proposal, and stop scheduled runs afterward.

## Post-order Review

- Confirm `live_order_result`, `live_order_status`, and
  `live_order_lifecycle` events exist for the approved order.
- Run fill reconciliation and confirm only new cumulative fill deltas were
  applied.
- Run KIS read-only sync and broker reconciliation again.
- Compare dashboard state, audit JSONL, SQLite state, and broker UI.
- Do not repeat operation after any mismatch, halt, timeout, unknown state, or
  manual broker action until the root cause is resolved and reconciliation
  passes again.
- If Maestro records `live_order_recovery_required` or blocks on
  `live_order_recovery_halt`, rerun read-only broker sync, broker reconciliation,
  fill reconciliation, and `maestro recover-live-order --reason "<...>"` before
  the next live approval order.

Normal `pytest -q` must remain fake-client and fixture based. Real KIS and
Telegram checks are opt-in operator rehearsals only.
