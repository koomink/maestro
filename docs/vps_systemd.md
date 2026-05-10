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
