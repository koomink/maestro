# VPS / systemd Guide

This guide is for read-only and paper workflows. It must not be used to enable
unguarded live trading.

## Environment File

Create an owner-readable environment file outside the repo, for example
`/etc/maestro/maestro.env`:

```ini
KIS_ACCOUNT_ID=...
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCESS_TOKEN=
KIS_APPROVAL_KEY=
MAESTRO_CONFIG=/root/maestro-operator/maestro_personal.yaml
```

Do not commit this file. Do not paste secret values into tickets, docs, audit
logs, or dashboard rows.

## Operator Config

Create one operator-local config outside the git checkout, for example
`/root/maestro-operator/maestro_personal.yaml`, and use that same file for
health checks, sync timers, Telegram operator, dashboard, and manual rehearsals.
This keeps mode, state DB, audit log, approval settings, and KIS settings
aligned across the hybrid operator architecture.
The examples below rely on `MAESTRO_CONFIG` from the environment file so every
unit resolves the same operator config by default.

Do not run Telegram from a separate Telegram-only config when it is expected to
represent the live operator state. Repo example configs are copy-and-customize
templates only.

## Example Health Service

```ini
[Unit]
Description=Maestro health check

[Service]
Type=oneshot
WorkingDirectory=/opt/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/opt/maestro/.venv/bin/maestro health
```

## Example Read-only Sync Service

```ini
[Unit]
Description=Maestro KIS multi-asset read-only sync

[Service]
Type=oneshot
WorkingDirectory=/opt/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/opt/maestro/.venv/bin/maestro kis-sync
```

## Example Telegram Operator Service

```ini
[Unit]
Description=Maestro Telegram Operator UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/opt/maestro/.venv/bin/maestro telegram-operator --timeout-seconds 10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The shared operator config must set `telegram_allowed_chat_ids` and
`whitelisted_user_ids` to the real operator account. This service handles
read-only Telegram commands and the limited `/pause` and `/kill_switch`
confirmations. Approval request polling still happens inside `maestro run-once`
when an approval-gated run is active.
Telegram Bot API polling allows only one active `getUpdates` consumer per bot
token, so stop this service during approval-gated `run-once` or
`live-smoke --check live-dry-run` rehearsals when they use the same bot.

Register the slash command menu once after bot setup or command changes:

```bash
MAESTRO_CONFIG=/root/maestro-operator/maestro_personal.yaml maestro telegram-set-commands
```

## Example Private Dashboard Service

Run the Streamlit dashboard as a localhost-only service and expose it through
Tailscale Serve. Do not expose Streamlit directly on the public internet.

```ini
[Unit]
Description=Maestro read-only Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/python -m streamlit run src/maestro/dashboard/app.py --server.address 127.0.0.1 --server.port 8503
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

After installing Tailscale and authenticating the VPS into the operator tailnet,
publish the local dashboard privately:

```bash
tailscale serve --bg --https=443 localhost:8503
tailscale serve status
```

Normal dashboard access should use the Tailscale Serve HTTPS URL. Keep
`8503/tcp` closed in `ufw`; use SSH port forwarding only as a fallback.

## Example Timer

```ini
[Unit]
Description=Run Maestro KIS read-only sync periodically

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
```

Reload systemd after installing unit files:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-kis-sync.timer
```

Keep service output in journald or a controlled log sink. Confirm structured
logs do not contain raw secrets before widening access to logs.
