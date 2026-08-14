# Deployment Guide

Maestro is not production-ready by default. Deploy only approval-gated,
read-only, or paper workflows until the relevant live adapter milestone is
complete.

## Current Runnable Modes

- Paper: `configs/paper.yaml`
- Broker read-only: `configs/live_readonly.yaml`
- Approval-gated live: `configs/live_approval.yaml`

The Symphony operator deployment splits operation into `symphony_readonly`,
`symphony_signal`, and `symphony_approval`. The signal phase produces
`signal_run_id`; the approval phase consumes that saved signal without
re-running strategies. `run-once` remains available as a legacy single-pipeline
entrypoint for paper and older operator flows.

The root `configs/` directory is reserved for operator-facing mode skeletons.
Production-candidate profiles live under `configs/operator/`. Historical
reference configs used by tests live under `tests/fixtures/configs/` and are not
operator entrypoints.

Personal operator live approval configs are generated with `maestro
init-personal`.

Do not enable `live_auto`, market orders, direct buy/sell/cancel CLI paths,
dashboard write controls, or Telegram admin controls.

## Host Setup

1. Clone the repo onto the target host.
2. Install Python and `uv`.
3. Install dependencies:

```bash
uv sync --extra dev --extra yahoo
uv pip install -e examples/sample_static_allocation
```

4. Copy an example config to a local, untracked path.
5. Set secrets as environment variables, not YAML values.
6. Keep `var/` owner-readable only when it contains broker state, audit logs, or
   token cache files.

KIS environment variable names, `var/kis_access_token.json`, and strict
reconciliation tolerances are schema defaults; operator configs only need to
override them when the deployment uses different names or paths.

For a single-user operator setup, generate an untracked config first:

```bash
maestro init-personal --output ~/maestro-operator/maestro_personal.yaml
maestro operator-evidence --config ~/maestro-operator/maestro_personal.yaml --output ~/maestro-operator/evidence-before.json
maestro personal-check --config ~/maestro-operator/maestro_personal.yaml
```

Then follow `docs/personal_operator_mvp.md` before enabling any live submission.
For the Symphony operator flow, copy all three operator configs to an
operator-local config set, for example `/home/symphony/maestro-operator/`. Copy the
shared `broker_accounts.yaml` and `strategy_accounts.yaml` files beside them;
the phase configs load them through `broker_accounts_path` and
`strategy_account_map_path`. Use
`symphony_readonly.yaml` for account refresh, `symphony_signal.yaml` for
Virtuoso signal generation, and `symphony_approval.yaml` for approval-gated
dry-run execution. Keep the copied files on the same `state.sqlite_path`,
`state.identity_group`, and `audit.jsonl_path`, then wire systemd through
`MAESTRO_READONLY_CONFIG`, `MAESTRO_SIGNAL_CONFIG`, and
`MAESTRO_APPROVAL_CONFIG`. Copying only `symphony_approval.yaml` is not enough
for the three-phase workflow because approval must consume the signal package
from the same shared state DB.

## Health Check

Run local health checks before and after scheduled jobs:

```bash
maestro health --config configs/live_readonly.yaml
```

`health` does not call live KIS endpoints. It checks config loading, SQLite
state, audit path, safety state, recent halt/failure events, DataHub config, KIS
environment variable presence, token cache path, broker snapshot age, and latest
reconciliation status.

`personal-check` summarizes the same local gates as a staged product-readiness
view: paper, read-only KIS, Telegram approval, live dry-run, and minimum-size
approval-gated live order readiness. `operator-evidence` writes the same
readiness shape plus latest state evidence to JSON. Neither command submits
broker orders, sends Telegram messages, or runs strategies.

## Logging

Maestro CLI configures structured JSON logging. Secret-like fields such as app
keys, app secrets, access tokens, authorization headers, and passwords are
redacted before they are emitted through structured log extras. Do not log raw
config dictionaries or environment dumps outside Maestro's logging helpers.
