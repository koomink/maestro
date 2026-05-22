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

Set `MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS` and
`MAESTRO_TELEGRAM_WHITELISTED_USER_IDS` in the shared Maestro operator
environment. Per-config `telegram_allowed_chat_ids` and `whitelisted_user_ids`
are optional overrides. This service handles
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

Run the FastAPI/React dashboard as a localhost-only service and expose it
through Tailscale Serve. Do not expose the dashboard directly on the public
internet.

```ini
[Unit]
Description=Maestro read-only Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro dashboard --host 127.0.0.1 --port 8503
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

## Example Heartbeat Timer

```ini
[Unit]
Description=Maestro heartbeat

[Service]
Type=oneshot
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro heartbeat
```

```ini
[Unit]
Description=Run Maestro heartbeat periodically

[Timer]
OnCalendar=*:0/15
Persistent=true
Unit=maestro-heartbeat.service

[Install]
WantedBy=timers.target
```

## Example Scheduled Run-once Timer

For the current polling-based Telegram approval flow, stop
`maestro-telegram-operator.service` while `run-once` is active so only one
process consumes Telegram `getUpdates` for the shared bot token. The service
below restarts the Telegram operator when `run-once` exits.

```ini
[Unit]
Description=Maestro scheduled run-once
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStartPre=-/bin/systemctl stop maestro-telegram-operator.service
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro run-once
ExecStopPost=-/bin/systemctl start maestro-telegram-operator.service
TimeoutStartSec=900
```

```ini
[Unit]
Description=Run Maestro scheduled run-once daily

[Timer]
OnCalendar=*-*-* 09:10:00
Persistent=true
Unit=maestro-run-once.service

[Install]
WantedBy=timers.target
```

Reload systemd after installing unit files:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-heartbeat.timer
sudo systemctl enable --now maestro-run-once.timer
```

Keep service output in journald or a controlled log sink. Confirm structured
logs do not contain raw secrets before widening access to logs.
