# Multi-account Broker Routing

Maestro routes broker execution with `strategy.account_id`.

- One enabled Virtuoso app maps to exactly one broker account.
- Multiple strategies may share one broker account.
- Existing single-account `kis:` configs remain valid and are migrated internally
  to `default_kis`.
- `paper` strategies may omit `account_id`; Maestro uses the logical
  `paper_default` account in payloads.
- `live_approval` strategies with explicit `accounts` must declare
  `strategy.account_id`.
- Operator configs may set `strategy_account_map_path` to load those
  `account_id` values from one shared YAML file. The mapping file is included
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
  monitoring, state, audit, real/paper routing, and safety gates.
- Operator-local overlay: secrets, account IDs, promotion-specific posture,
  budgets, dates, and environment-specific paths.

Fragments are recommendations and defaults, not live-trading authority. Maestro
rejects fragments that contain operator-owned keys such as `execution`,
`approval`, `risk`, `state`, `audit`, `kis`, `accounts`, or `monitoring`.
`profile-validate` checks recommendation drift for live approval, KIS
paper-trading, and production profiles; for example, an Ataraxia fragment can
recommend `execution.order_generation_mode=buy_only_contribution` while the
operator profile remains responsible for the budget and buy day.

Example:

```yaml
app_fragment_paths:
  - ../../../Virtuoso/Ataraxia/configs/fragments/ataraxia.yaml

accounts:
  - id: kis_mock
    broker: kis
    environment: paper_trading
    account_id_env: KIS_MOCK_ACCOUNT_ID
    app_key_env: KIS_MOCK_APP_KEY
    app_secret_env: KIS_MOCK_APP_SECRET
    token_cache_path: var/kis_mock_access_token.json
    broker_products: [kis_domestic_stock]

  - id: kis_isa
    broker: kis
    environment: real
    account_id_env: KIS_ISA_ACCOUNT_ID
    app_key_env: KIS_ISA_APP_KEY
    app_secret_env: KIS_ISA_APP_SECRET
    token_cache_path: var/kis_isa_access_token.json
    broker_products: [kis_domestic_stock]

  - id: toss_brokerage
    broker: toss
    environment: real

  - id: kis_brokerage
    broker: kis
    environment: real
    account_id_env: KIS_BROKERAGE_ACCOUNT_ID
    app_key_env: KIS_BROKERAGE_APP_KEY
    app_secret_env: KIS_BROKERAGE_APP_SECRET
    token_cache_path: var/kis_brokerage_access_token.json
    broker_products: [kis_overseas_stock]

strategy_account_map_path: strategy_accounts.yaml

strategies:
  - id: ataraxia
    enabled: true
    weight: 1.0

  - id: snowball
    entrypoint: snowball.plugin:SnowballPlugin
    weight: 1.0

  - id: trading_agents
    entrypoint: trading_agents.plugin:TradingAgentsPlugin
    weight: 1.0

  - id: candidate_strategy
    entrypoint: candidate_strategy.plugin:CandidateStrategyPlugin
    weight: 1.0
```

Shared mapping file. The legacy `strategy_id: account_id` form remains valid,
but operator workflows should prefer the object form so phase visibility and
order posture are explicit:

```yaml
strategies:
  ataraxia:
    account_id: kis_mock
    readonly: true
    signal: true
    order_posture: dry_run

  snowball_us:
    account_id: dev_sandbox
    readonly: true
    signal: true
    order_posture: dry_run

  trading_agents:
    account_id: dev_sandbox
    readonly: true
    signal: false
    order_posture: disabled
```

Phase 1 operational support:

- KIS `environment: real` uses the real KIS base URL behavior.
- KIS `environment: paper_trading` uses the existing KIS VTS behavior.
- Toss accounts validate and appear in mappings, but live broker submit/read-only
  calls raise `UnsupportedBrokerOperation` until official trading API support is
  added.

Current operator split:

- `configs/operator/symphony_readonly.yaml`: all configured accounts, no
  strategies, dashboard and Telegram account refresh.
- `configs/operator/symphony_signal.yaml`: all configured accounts plus Virtuoso
  apps, emits a persisted `signal_run_id`; order submission is disabled.
- `configs/operator/symphony_approval.yaml`: consumes `signal_run_id`, does not
  re-run strategies, and executes approval/order lifecycle after freshness and
  safety checks. It is intentionally `order_posture: dry_run` by default.

The current strategy mapping routes `ataraxia -> kis_mock`,
`snowball_us -> dev_sandbox`, and `trading_agents -> dev_sandbox` through
`configs/operator/strategy_accounts.yaml`.

## Strategy Promotion And Account Binding

Strategy registration and account binding are intentionally separate.

Development-only Virtuoso apps are not added to the Symphony operator mapping.
For example, a TradingAgents wrapper can be developed and tested in paper/dev
configs without any `strategy_accounts.yaml` entry. If a strategy id appears in
the shared mapping but is not present in the signal/approval strategy list,
Maestro fails closed with an unknown strategy-id error.

When a strategy becomes an operator candidate, add it to
`symphony_signal.yaml` and `symphony_approval.yaml` first. It may remain
`enabled: false` while the code and config are staged. When rehearsal begins,
enable it and add one mapping entry, usually to the safest available account:

```yaml
strategies:
  ataraxia: kis_isa
  snowball_us: kis_brokerage
  trading_agents: kis_mock
```

Promotion should move in this order:

1. Development or paper config: no live operator account binding.
2. Symphony candidate rehearsal: bind to `kis_mock` or another paper-trading
   account.
3. Live dry-run candidate: keep approval gated and inspect generated orders.
4. Real account promotion: bind to `kis_isa`, `kis_brokerage`, or another real
   account only after operator approval and evidence review.

Toss-backed strategies can be represented in config, but Toss broker operations
remain fail-closed until official trading API support is implemented.

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
