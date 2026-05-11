# Deployment Guide

Maestro is not production-ready by default. Deploy only approval-gated,
read-only, or paper workflows until the relevant live adapter milestone is
complete.

## Current Runnable Modes

- Mock paper: `configs/paper.yaml`
- CSV paper: `configs/csv_paper.yaml`
- Real-data US stock/ETF paper: `configs/us_etf_yahoo_paper.yaml`
- Mock KIS read-only: `configs/live_readonly.yaml`
- KIS overseas read-only: `configs/kis_overseas_readonly.example.yaml`
- Personal operator live approval config generated with `maestro init-personal`

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

For a single-user operator setup, generate an untracked config first:

```bash
maestro init-personal --output ~/maestro-operator/maestro_personal.yaml
maestro operator-evidence --config ~/maestro-operator/maestro_personal.yaml --output ~/maestro-operator/evidence-before.json
maestro personal-check --config ~/maestro-operator/maestro_personal.yaml
```

Then follow `docs/personal_operator_mvp.md` before enabling any live submission.

## Health Check

Run local health checks before and after scheduled jobs:

```bash
maestro health --config configs/kis_overseas_readonly.example.yaml
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
