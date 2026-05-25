# Ataraxia KIS Paper Submit Pilot

This runbook promotes the Ataraxia KRW domestic ETF strategy from Maestro paper
mode to the KIS mock-investment approval-gated broker-submit path. It uses the
real KIS OpenAPI VTS endpoint, not fake clients or mock market data. It is
limited to `kis_domestic_stock`, `kis.paper_trading=true`, Telegram manual
approval, limit orders only, and four scheduled trading-day cycles. It does not
authorize a real cash account, `live_auto`, market orders, direct broker trading
CLI commands, dashboard write controls, or Telegram live enablement.

## Operator Config

Create an operator-local config outside source control from the current
Symphony approval profile or the KIS paper-trading fixture. Do not run scheduled
jobs directly from a source-controlled template.

Keep these values for the initial dry-run stages:

```yaml
mode: live_approval
execution:
  allowed_order_type: limit
  order_posture: dry_run
  require_reconciliation_pass: true
  market_session:
    required: true
  broker_validation:
    require_quote_validation: true
    require_risk_validation: true
  live_order_limits:
    daily_loss_limit: 100000

monitoring:
  heartbeat_max_age_seconds: 3600
  scheduled_run_max_age_seconds: 86400
kis:
  provider: kis
  broker_product: kis_domestic_stock
  broker_products:
    - kis_domestic_stock
  paper_trading: true
```

For the first KIS mock-investment broker-submit pilot only, set
`execution.order_posture=armed`.
Keep `execution.live_order_limits.max_order_notional`,
`execution.live_order_limits.max_daily_notional`, and
`execution.live_order_limits.max_daily_order_count` at minimum-order rehearsal
levels.

Secrets belong only in environment variables or an operator-local env file:
`KIS_MOCK_ACCOUNT_ID`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, optional
`KIS_ACCESS_TOKEN`, optional `KIS_APPROVAL_KEY`, `TELEGRAM_BOT_TOKEN`,
`MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS`, and
`MAESTRO_TELEGRAM_WHITELISTED_USER_IDS`.

## Promotion Steps

1. Verify the baseline Maestro paper run:

```bash
maestro run-once --config configs/paper.yaml
maestro status --config configs/paper.yaml
```

Confirm the paper state and audit log are expected. Keep Ataraxia installed in
the Maestro virtualenv as a non-editable package.

2. Establish the KIS mock-investment read-only baseline using the real KIS VTS
   account:

```bash
maestro kis-sync --config <operator-config>
maestro kis-account --config <operator-config>
```

Compare the KIS UI and Maestro snapshot account number, cash, holdings, broker
symbols, and current prices. For the first accepted baseline only:

```bash
maestro adopt-broker-snapshot --config <operator-config> --reason "operator accepted KIS paper baseline"
maestro reconcile --config <operator-config>
```

Do not continue unless reconciliation passes.

3. Rehearse Telegram approval:

```bash
maestro live-smoke --config <operator-config> --check telegram-approval
```

Confirm messages go only to allowed chats and only whitelisted users can approve
or reject. Stop `maestro-telegram-operator.service` during approval polling if
it uses the same bot token.

4. Rehearse the approval-gated live path without broker submit:

```bash
maestro live-smoke --config <operator-config> --check live-dry-run
```

Review `live_proposal_data_snapshot`, `live_order_dry_run`, and audit JSONL.
Stop if the symbol, side, limit price, quantity, notional, or KIS broker product
differs from the expected Ataraxia domestic ETF proposal.
Dry-run events do not count as monthly contribution execution and must not block
the later submit pilot in the same state database.

5. Run the beta gate for the KIS mock-investment submit pilot:

For the first submit pilot, change only these execution switches in the
operator-local config after the dry-run payload is accepted:

```yaml
execution:
  order_posture: armed
```

```bash
maestro heartbeat --config <operator-config>
maestro health --config <operator-config>
maestro beta-preflight --config <operator-config>
```

Continue only when health is ok and beta preflight prints
`check=private_beta_preflight status=ok message=ready`.
On a brand-new state database, `beta-preflight` can fail with
`health:scheduled_run` until a recent `run_once_completed` event exists. Seed
that evidence with a no-order warmup run, then rerun `kis-sync`, `reconcile`,
and `beta-preflight` before enabling the submit pilot.

When `kis.paper_trading=true`, Maestro uses the KIS VTS base URL and domestic
stock demo order TR_IDs such as `VTTC0012U` for buy and `VTTC0011U` for sell.
Changing to a real cash account is a separate operator-local config change:
use the real account credentials and set `kis.paper_trading=false` only after a
separate real-account baseline and approval.

## Four-cycle Pilot

Run exactly four trading-day scheduled cycles. A cycle with no proposal still
counts when heartbeat, sync, reconciliation, beta-preflight, and run-once finish
normally.

Use this sequence for each cycle:

```bash
maestro heartbeat --config <operator-config>
maestro kis-sync --config <operator-config>
maestro reconcile --config <operator-config>
maestro beta-preflight --config <operator-config>
systemctl stop maestro-telegram-operator.service
maestro run-once --config <operator-config>
# Approve in Telegram only when the proposal is exactly expected.
systemctl restart maestro-telegram-operator.service
maestro reconcile-fills --config <operator-config>
maestro kis-sync --config <operator-config>
maestro reconcile --config <operator-config>
maestro operator-evidence --config <operator-config> --output <cycle-evidence.json>
```

Turn off the schedule and follow `docs/operator_runbook.md` if any cycle
records a halt, unknown order status, reconciliation failure, stale data,
unexpected proposal, or unreviewed broker submit.

## Acceptance

Before the pilot, run the targeted local regression suite and confirm
`git diff --check` is clean.

Per cycle, verify there is no unreviewed broker submit, every live order has
`live_order_result`, `live_order_status`, and `live_order_lifecycle` events, fill
reconciliation applies only new cumulative fill deltas, and the KIS UI, Maestro
state, audit log, and dashboard agree after the cycle.
