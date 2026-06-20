# Multi-account Broker Routing

Maestro normally routes broker execution with `strategy.account_id`.

- One enabled Virtuoso app maps to exactly one broker account unless it opts into
  an operator-owned `multi_account_contributions` group.
- Multiple strategies may share one broker account.
- A multi-account contribution group keeps one strategy signal but lets Maestro
  create separate `account_id + execution_sleeve` order scopes.
- Existing single-account `kis:` configs remain valid and are migrated internally
  to `default_kis`.
- `paper` strategies may omit `account_id`; Maestro uses the logical
  `paper_default` account in payloads.
- `live_approval` strategies with explicit `accounts` must declare
  `strategy.account_id`.
- Operator configs may set `broker_accounts_path` to load shared broker account
  definitions from one YAML file. The accounts file is included in config
  identity and runtime fingerprint checks.
- Operator configs may set `strategy_account_map_path` to load shared
  strategy routing from one YAML file: account binding, execution sleeve,
  signal/readonly visibility, and order posture. The mapping file is included
  in config identity and runtime fingerprint checks.
- Operator configs may set `app_fragment_paths` to import Virtuoso app-owned
  fragments before account mapping is applied. Fragment bytes are included in
  the config fingerprint.

## Virtuoso App Fragments

App fragments prevent single-app examples and Symphony operator profiles from
copying the same strategy defaults, symbols, and DataHub hints. The ownership
line is strict:

- App fragment: strategy id, entrypoint, default strategy config, required
  instruments, currency sleeve membership, DataHub symbol maps, and
  recommendations such as a preferred order generation mode.
- Operator profile: broker accounts, approval, execution posture, risk,
  monitoring, state, audit, real/paper routing, execution sleeves, and
  safety gates.
- Operator-local overlay: secrets, account IDs, promotion-specific posture,
  budgets, dates, and environment-specific paths.

Fragments are recommendations and defaults, not live-trading authority. Maestro
rejects fragments that contain operator-owned keys such as `execution`,
`approval`, `risk`, `state`, `audit`, `kis`, `accounts`, or `monitoring`.
`profile-validate` checks recommendation drift for live approval, KIS
paper-trading, and production profiles; for example, an Tranquillo fragment can
recommend `execution.order_generation_mode=buy_only_contribution` while the
operator profile remains responsible for the budget and buy day.

Example:

```yaml
app_fragment_paths:
  - ../../../Virtuoso/virtuoso-tranquillo/configs/fragments/tranquillo.yaml
broker_accounts_path: broker_accounts.yaml
strategy_account_map_path: strategy_accounts.yaml

strategies:
  - id: tranquillo
    enabled: true
    weight: 1.0

  - id: crescendo
    entrypoint: crescendo.plugin:CrescendoPlugin
    weight: 1.0

  - id: fugue
    entrypoint: fugue.strategy:FugueStrategy
    weight: 1.0

  - id: candidate_strategy
    entrypoint: candidate_strategy.plugin:CandidateStrategyPlugin
    weight: 1.0
```

KIS account entries define the credential/account boundary. The actual KIS REST
API families available to an account are declared through `broker_products`:
KRX-listed stocks and ETFs use `kis_domestic_stock`, while US-listed stocks and
ETFs use `kis_overseas_stock`. Maestro keeps separate product-specific adapters
for those market differences while sharing common KIS REST plumbing underneath
them.

Shared mapping file. The legacy `strategy_id: account_id` form remains valid,
but operator workflows should prefer the object form so phase visibility,
execution sleeve, order posture, and multi-account contribution groups are
explicit:

```yaml
execution_sleeves:
  accounts:
    kis_mock:
      tranquillo_isa:
        currency_sleeve: KRW
        target_weight: 1.0
        order_generation_mode: buy_only_contribution
        contribution:
          enabled: true
          currency: KRW
          sleeve: KRW
          monthly_budget: 3000000
          buy_day: 25

    toss_brokerage:
      crescendo_us:
        currency_sleeve: USD
        target_weight: 1.0
        order_generation_mode: target_rebalance

account_strategy_targets:
  toss_brokerage:
    crescendo_us:
      target_weight: 0.7
    manual:
      target_weight: 0.3

multi_account_contributions:
  tranquillo:
    strategy_id: tranquillo
    allocation_basis: aggregate_current_holdings
    order_generation_mode: buy_only_contribution
    account_targets:
      - account_id: kis_ps
        execution_sleeve: tranquillo_ps
        allowed_symbols: [KODEX_US_DIVIDEND_DOWJONES]
        monthly_budget: 500000
      - account_id: kis_isa
        execution_sleeve: tranquillo_isa
        allowed_symbols: [TIGER_NASDAQ100_LEVERAGE, KODEX_US_DIVIDEND_DOWJONES]
        min_monthly_budget: 1660000
        max_monthly_budget: 4000000

strategies:
  tranquillo:
    account_id: multi_account_contributions.tranquillo
    readonly: true
    signal: true
    order_posture: dry_run

  crescendo_us:
    account_id: toss_brokerage
    execution_sleeve: crescendo_us
    readonly: true
    signal: true
    order_posture: dry_run

  fugue:
    account_id: dev_sandbox
    execution_sleeve: fugue
    readonly: true
    signal: false
    order_posture: disabled
```

Execution sleeves are virtual operator books inside a broker account. They sit
above `portfolio.currency_sleeves`: a currency sleeve describes cash/symbol
membership such as `KRW` or `USD`, while an execution sleeve owns strategy
routing, target account weight, order generation mode, and contribution budget.
Maestro enforces one `order_generation_mode` per `account_id + execution_sleeve`.
When multiple active execution sleeves share one account, their `target_weight`
values must sum to `1.0`.

`account_strategy_targets` declares the operator-facing account books for
accounts that mix Maestro-managed strategies with manual investing. The
`manual` bucket is not an automatic trading target; it is reserved capacity that
Maestro does not sell or rebalance. For `toss_brokerage`, `crescendo_us` uses
70% target capacity and `manual` reserves 30%. If manual holdings exceed their
target, Maestro reduces automatic strategy capacity and reports the drift
instead of selling manual positions.

Multi-account contribution groups are also operator-owned. They are for cases
where one Virtuoso strategy target must be allocated across more than one
account because account capabilities differ. Tranquillo v1 uses this to keep one
aggregate domestic ETF target while routing pension-savings (`kis_ps`) cash to
the SCHD-like ETF only and ISA (`kis_isa`) cash to the remaining SCHD-like /
QLD-like mix. Maestro calculates the aggregate current holdings across the group
accounts, applies fixed account buys first, and then allocates variable account
cash toward the group target. It does not sell positions to force the target.
The strategy entry must use the virtual account marker
`account_id: multi_account_contributions.<group_id>` and must not set
`execution_sleeve`; the group `account_targets` are the only source of concrete
account and sleeve routing.


For `buy_only_contribution` sleeves, `contribution.funding_request.enabled` is an
explicit opt-in. When enabled and the monthly buy is due but available cash after
the fee buffer is below `min_monthly_budget`, Maestro records a
`contribution_funding_request` event and can send a Telegram operator request.
The request asks the operator to add at least the minimum shortfall; it does not
move money, submit orders, or replace the normal Telegram order approval. After
the operator confirms that cash was added, Maestro refreshes broker/account
state, generates a fresh signal, and any resulting orders still require the
regular approval flow.

`contribution.budget_request.enabled` is a separate opt-in for variable
buy-only sleeves. When cash is at or above `min_monthly_budget`, Maestro records
a `contribution_budget_request` instead of immediately generating orders for
that sleeve. The operator chooses the minimum, recommended monthly amount, full
available cash after fee buffer, or a custom `/budget <request_id> <amount>`.
The selected amount is saved as a budget decision, Maestro regenerates the
signal, and any generated orders still require the normal approval flow.
`max_monthly_budget` is kept only as a compatibility field and is not used as a
cash cap.

Cash rebalance v1 only allocates available account cash across execution sleeves.
It does not sell existing positions to force sleeve weights. If a sleeve is below
its target account weight, new cash is allocated to the shortfall first; if no
sleeve is underweight, cash is split by target weights. Separate execution
sleeves in the same account may not target the same tradable symbol in v1.
When account attribution snapshots exist, execution scopes use the attributed
strategy quantity for order generation so manually owned shares of the same
symbol are not treated as strategy inventory.

Phase 1 operational support:

- KIS `environment: real` uses the real KIS base URL behavior.
- KIS `environment: paper_trading` uses the existing KIS VTS behavior.
- Toss accounts validate, appear in mappings, and use the common Toss OpenAPI
  adapter for snapshots, submit, status, modification, and cancellation.
  Initial armed operation permits Telegram-approved integer DAY limit orders.

Current operator split:

- `configs/operator/broker_accounts.yaml`: shared account inventory for every
  Symphony phase, including env-var names and broker product capability.
- `configs/operator/symphony_readonly.yaml`: all configured accounts, no
  strategies, dashboard and Telegram account refresh.
- `configs/operator/symphony_signal.yaml`: all configured accounts plus Virtuoso
  apps, emits a persisted `signal_run_id`; order submission is disabled.
- `configs/operator/symphony_approval.yaml`: consumes `signal_run_id`, does not
  re-run strategies, and executes approval/order lifecycle after freshness and
  safety checks. It is intentionally `order_posture: dry_run` by default.

The current strategy mapping routes Tranquillo through the
`multi_account_contributions.tranquillo` group: `kis_ps / tranquillo_ps` buys
only the SCHD-like domestic ETF with a fixed 500,000 KRW monthly budget, while
`kis_isa / tranquillo_isa` uses 1,660,000-4,000,000 KRW toward the aggregate
60/40 Tranquillo target. `crescendo_us -> toss_brokerage / crescendo_us` is a
normal single-account strategy mapping with manual bucket capacity reserved, and
`fugue -> dev_sandbox / fugue` remains disabled for operator visibility.
Account definitions are centralized in `configs/operator/broker_accounts.yaml`.

## Strategy Promotion And Account Binding

Strategy registration and account binding are intentionally separate.

Development-only Virtuoso apps are not added to the Symphony operator mapping.
For example, a Fugue wrapper can be developed and tested in paper/dev
configs without any `strategy_accounts.yaml` entry. If a strategy id appears in
the shared mapping but is not present in the signal/approval strategy list,
Maestro fails closed with an unknown strategy-id error.

When a strategy becomes an operator candidate, add it to
`symphony_signal.yaml` and `symphony_approval.yaml` first. It may remain
`enabled: false` while the code and config are staged. When rehearsal begins,
enable it and add one mapping entry, usually to the safest available account:

```yaml
strategies:
  tranquillo:
    account_id: multi_account_contributions.tranquillo
  crescendo_us: kis_brokerage
  fugue: kis_mock
```

Promotion should move in this order:

1. Development or paper config: no live operator account binding.
2. Symphony candidate rehearsal: bind to `kis_mock` or another paper-trading
   account.
3. Live dry-run candidate: keep approval gated and inspect generated orders.
4. Real account promotion: bind to `kis_isa`, `kis_brokerage`, or another real
   account only after operator approval and evidence review.

Toss-backed strategies use the common broker snapshot path and the official
OpenAPI document in `docs/toss_openapi.json`. Market and US amount orders are
implemented at the adapter boundary but remain blocked by the initial live
safety policy.

The first broker sync writes an `auto_baseline` attribution candidate. Adopt it
with `maestro adopt-account-attribution --account-id toss_brokerage --reason
"..."` or `/attribution toss_brokerage` in Telegram. Missing, unapproved, stale,
or quantity-mismatched attribution blocks submit and modification.

## Phase Controls And Order Posture

`readonly`, `signal`, and `order_posture` are strategy-level operator controls.

- `readonly: true` means the Virtuoso app appears in dashboard/Telegram
  management views. It does not make `live_readonly` execute strategy code.
- `signal: true` means `symphony_signal` may load and run the strategy.
- `signal: false` keeps the app visible when `readonly: true`, but excludes it
  from signal generation.
- `order_posture: disabled` excludes the strategy's orders from Telegram
  approval requests and broker submit.
- `order_posture: dry_run` includes the strategy's orders in Telegram approval,
  but approved orders are recorded as dry-run events only.
- `order_posture: armed` allows approved orders to become live broker-submit
  candidates, subject to the global posture ceiling and all safety gates.

The global `execution.order_posture` remains the safety ceiling. `disabled`
prevents approval/submit for every strategy, `dry_run` downgrades strategy
`armed` orders to dry-run, and only global `armed` allows strategy `armed`
orders to reach broker submit.

`dev_sandbox` is the recommended account for development-stage strategies that
need signal and approval UX rehearsal without touching KIS mock, KIS real, or
Toss broker APIs:

```yaml
accounts:
  - id: dev_sandbox
    broker: sandbox
    environment: paper_trading
    enabled: true
    broker_products: [kis_domestic_stock, kis_overseas_stock]
```

Maestro rejects mixed strategy posture within the same account. For example,
`kis_isa` cannot host both an `armed` strategy and a `dry_run` strategy in the
same Symphony run. Use a separate account such as `dev_sandbox` for development
rehearsals.

Audit/dashboard payloads now expose `account_id` on strategy runs, orders,
approvals, risk decisions, live order requests, and broker snapshots where that
logical account is known.
