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
```

Do not commit this file. Do not paste secret values into tickets, docs, audit
logs, or dashboard rows.

## Example Health Service

```ini
[Unit]
Description=Maestro health check

[Service]
Type=oneshot
WorkingDirectory=/opt/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/opt/maestro/.venv/bin/maestro health --config /opt/maestro/configs/kis_overseas_readonly.example.yaml
```

## Example Read-only Sync Service

```ini
[Unit]
Description=Maestro KIS overseas read-only sync

[Service]
Type=oneshot
WorkingDirectory=/opt/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/opt/maestro/.venv/bin/maestro kis-sync --config /opt/maestro/configs/kis_overseas_readonly.example.yaml
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
ExecStart=/opt/maestro/.venv/bin/maestro telegram-operator --config /etc/maestro/telegram_operator.yaml --timeout-seconds 10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Use an operator-local config outside the git checkout, such as
`/etc/maestro/telegram_operator.yaml` or `/root/maestro-operator/telegram_approval_operator.yaml`.
Do not point systemd at `configs/telegram_approval_paper.yaml`; that file is a
repo example and may contain placeholder chat/user IDs. The operator-local
config must set `telegram_allowed_chat_ids` and `whitelisted_user_ids` to the
real operator account. This service handles read-only Telegram commands and the
limited `/pause` and `/kill_switch` confirmations. Approval request polling
still happens inside `maestro run-once` when an approval-gated run is active.
Telegram Bot API polling allows only one active `getUpdates` consumer per bot
token, so stop this service during approval-gated `run-once` or
`live-smoke --check live-dry-run` rehearsals when they use the same bot.

Register the slash command menu once after bot setup or command changes:

```bash
maestro telegram-set-commands --config /opt/maestro/configs/telegram_approval_paper.yaml
```

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
