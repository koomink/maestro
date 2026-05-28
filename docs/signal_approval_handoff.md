# Signal-to-Approval Handoff

This document defines the operator workflow for separating observation,
strategy signal generation, and approval-gated execution. The initial
`signal_run_id` handoff exists through `maestro run-signal` and
`maestro approve-signal`; signal packages now include broker snapshot refs and
approval rejects stale broker refs, config mismatches, mapping drift, and stale
or missing DataHub evidence.

## Target Operator Configs

Maestro should converge on three Symphony-level operator configs:

- `configs/operator/symphony_readonly.yaml`
- `configs/operator/symphony_signal.yaml`
- `configs/operator/symphony_approval.yaml`

Strategy-to-account mapping is shared through `strategy_account_map_path`,
normally pointing both signal and approval configs at `strategy_accounts.yaml`.
The mapping file is part of config identity, so approval fails closed when the
mapping changes after a signal package was generated.

Development-only Virtuoso apps should not be mapped here yet. A strategy such
as TradingAgents can remain in paper/dev configs with no Symphony account
binding. Once it becomes an operator candidate, add it to both signal and
approval configs, then add exactly one shared mapping entry. Use `dev_sandbox`
when the operator wants signal/approval UX rehearsal without broker API access;
use `kis_mock` when the rehearsal intentionally targets the KIS mock-investment
account. Real account binding is a separate promotion decision.

Strategy controls in the shared mapping determine which phase sees each app:

- `enabled: true` admits the app into the operator-facing Symphony universe.
- `readonly: true` exposes the app in dashboard/Telegram management views only.
  `live_readonly` still runs no strategy code.
- `signal: true` lets `symphony_signal` execute the strategy and persist its
  result in the signal package.
- `order_posture: disabled` excludes generated orders from Telegram approval.
- `order_posture: dry_run` includes generated orders in Telegram approval, but
  approved orders become `live_order_dry_run` events only.
- `order_posture: armed` can submit only when the global
  `execution.order_posture` is also `armed`.

Signal packages persist the effective phase controls and per-order posture so
`symphony_approval` can apply them without re-running strategies.

The current operator mapping enables `ataraxia -> kis_mock` and
`snowball_us -> dev_sandbox`; `trading_agents -> dev_sandbox` is `enabled: false`,
`readonly: true`, and `signal: false`, so it can appear in
operator views without being imported or executed by `symphony_signal`.

## Responsibilities

### `symphony_readonly`

Purpose: observe broker truth.

- Refresh all configured broker accounts.
- Persist broker account snapshots and reconciliation evidence.
- Feed dashboard and Telegram status views.
- Run no Virtuoso strategies.
- Generate no portfolio targets, orders, approvals, or broker submits.

### `symphony_signal`

Purpose: run Virtuoso strategy apps and produce an immutable trading intent
preview.

- Run on a schedule, initially daily at 09:10 KST.
- Refresh broker/account state before strategy work.
- Run enabled Virtuoso apps.
- Persist strategy results, target allocation, risk preview, data snapshot,
  account mapping, broker snapshot refs, and generated preview orders if order
  preview is enabled.
- If no trading action is needed, record a no-op signal result and stop.
- If trading action is needed, emit a `signal_run_id` for the approval step.
- Submit no broker orders.

### `symphony_approval`

Purpose: execute the previously generated signal after operator approval.

- Accept a required `signal_run_id`.
- Load the persisted signal package.
- Do not re-run Virtuoso strategies.
- Re-validate safety, freshness, broker state, prices, limits, and kill-switch
  state before creating approval requests or orders.
- Generate orders only from the saved signal package.
- Include `signal_run_id` in approval payloads, order payloads, live order
  requests/results, lifecycle events, reconciliation events, and audit logs.
- Submit live orders only through the existing approval-gated live order
  lifecycle.

## Signal Package

A signal package should be immutable after creation and include:

- `signal_run_id`
- source config identity and profile stage
- generated time; approval treats the package as expired after
  `approval.signal_max_age_seconds`
- account mappings
- broker snapshot references used as baseline
- DataHub request/audit payloads and data freshness evidence
- strategy results and optional source signals
- portfolio target per account
- risk preview per account
- proposed order preview per account, if generated
- no-op reason when no approval is needed

`symphony_approval` must reject missing, expired, mutated, or config-mismatched
signal packages.

## Freshness and Safety Checks

Before approval or broker submit, Maestro must fail closed when:

- the signal package is expired;
- the active config identity no longer matches the signal package;
- the broker snapshot is stale or materially different from the signal baseline;
- broker quote validation exceeds configured tolerance;
- required DataHub evidence is missing or stale;
- account mapping no longer exists or now points to a different broker account;
- reconciliation is missing or failed;
- safety state is paused, killed, halted, or requires live order recovery;
- daily/per-order limits would be exceeded.

## CLI Direction

The operator flow starts with:

```bash
maestro kis-sync --config configs/operator/symphony_readonly.yaml
maestro run-signal --config configs/operator/symphony_signal.yaml
maestro approve-signal --config configs/operator/symphony_approval.yaml --signal-run-id <id>
```

`maestro daily-signal-approval` automates the scheduled handoff: it locks the
workflow, refreshes read-only broker state when configured, runs the signal
phase, sends the daily Telegram signal summary, skips approval for no-action
signals, and creates approval requests only for actionable signals. During
approval polling it stops `maestro-telegram-operator.service` and restarts it on
exit so the shared Telegram bot has only one `getUpdates` consumer. The legacy
`scripts/operator/symphony_signal_then_approval.sh` wrapper remains available for
compatibility, but systemd should use the CLI orchestrator.

The workflow must continue to evolve without weakening the current
`live_approval` safety posture: no `live_auto`, no market orders, no direct
buy/sell/cancel CLI, and no strategy-owned broker calls.
