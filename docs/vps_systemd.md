# VPS / systemd Guide

This guide is for read-only and paper workflows. It must not be used to enable
unguarded live trading.

## Environment File

Create an owner-readable environment file outside the repo, for example
`/etc/maestro/maestro.env`:

```ini
KIS_MOCK_ACCOUNT_ID=...
KIS_MOCK_APP_KEY=...
KIS_MOCK_APP_SECRET=...
KIS_ISA_ACCOUNT_ID=
KIS_ISA_APP_KEY=
KIS_ISA_APP_SECRET=
KIS_BROKERAGE_ACCOUNT_ID=
KIS_BROKERAGE_APP_KEY=
KIS_BROKERAGE_APP_SECRET=
KIS_ACCESS_TOKEN=
KIS_APPROVAL_KEY=
MAESTRO_CONFIG=/root/maestro-operator/maestro_personal.yaml
MAESTRO_READONLY_CONFIG=/root/maestro-operator/symphony_readonly.yaml
MAESTRO_SIGNAL_CONFIG=/root/maestro-operator/symphony_signal.yaml
MAESTRO_APPROVAL_CONFIG=/root/maestro-operator/symphony_approval.yaml
```

Do not commit this file. Do not paste secret values into tickets, docs, audit
logs, or dashboard rows.

## Operator Config

Single-profile deployments can use one operator-local config outside the git
checkout, for example `/root/maestro-operator/maestro_personal.yaml`, and point
`MAESTRO_CONFIG` at that file.

The Symphony multi-account deployment uses an operator-local config set instead:
`symphony_readonly.yaml`, `symphony_signal.yaml`, and
`symphony_approval.yaml`, plus the shared `strategy_accounts.yaml` mapping used
by signal and approval. These files intentionally have different modes and
execution posture, but must share the same state DB, `state.identity_group`, and
audit log. The examples below use `MAESTRO_READONLY_CONFIG`,
`MAESTRO_SIGNAL_CONFIG`, and `MAESTRO_APPROVAL_CONFIG` for that config set.

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
ExecStart=/opt/maestro/.venv/bin/maestro telegram-operator --config ${MAESTRO_READONLY_CONFIG} --signal-config ${MAESTRO_SIGNAL_CONFIG} --timeout-seconds 10
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
or `maestro approve-signal` when an approval-gated run is active.
Telegram Bot API polling allows only one active `getUpdates` consumer per bot
token, so stop this service during approval-gated `run-once`,
`approve-signal`, or `live-smoke --check live-dry-run` rehearsals when they use
the same bot. The Symphony signal wrapper handles this stop/start boundary.

Register the slash command menu once after bot setup or command changes:

```bash
maestro telegram-set-commands --config ${MAESTRO_READONLY_CONFIG} --signal-config ${MAESTRO_SIGNAL_CONFIG}
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
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro dashboard --config ${MAESTRO_READONLY_CONFIG} --host 127.0.0.1 --port 8503
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

## Example Symphony Read-only Refresh Timer

Install `deploy/systemd/maestro-symphony-readonly.service` and
`deploy/systemd/maestro-symphony-readonly.timer` to refresh all configured KIS
accounts and run reconciliation against the shared Symphony state.

```ini
[Unit]
Description=Maestro Symphony read-only account refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro kis-sync --config ${MAESTRO_READONLY_CONFIG}
ExecStartPost=/root/projects/Symphony/Maestro/.venv/bin/maestro reconcile --config ${MAESTRO_READONLY_CONFIG}
TimeoutStartSec=300
```

```ini
[Unit]
Description=Run Maestro Symphony read-only refresh periodically

[Timer]
OnCalendar=*:0/15
Persistent=true
Unit=maestro-symphony-readonly.service

[Install]
WantedBy=timers.target
```

## Example Symphony Signal and Conditional Approval Timer

Install `deploy/systemd/maestro-symphony-signal.service` and
`deploy/systemd/maestro-symphony-signal.timer`. The service calls
`maestro daily-signal-approval`, which obtains a file lock, refreshes read-only
broker state when configured, runs `maestro run-signal` semantics through
`${MAESTRO_SIGNAL_CONFIG}`, sends the daily Telegram signal summary, and only
continues into approval when `action_required=true`.

During approval polling the command stops `maestro-telegram-operator.service`
and restarts it on exit so the shared Telegram bot has one `getUpdates`
consumer. The legacy `scripts/operator/symphony_signal_then_approval.sh` wrapper
remains available for compatibility, but the systemd timer should use the CLI
orchestrator.

```ini
[Unit]
Description=Maestro Symphony daily signal approval orchestration
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/projects/Symphony/Maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/root/projects/Symphony/Maestro/.venv/bin/maestro daily-signal-approval --readonly-config ${MAESTRO_READONLY_CONFIG} --signal-config ${MAESTRO_SIGNAL_CONFIG} --approval-config ${MAESTRO_APPROVAL_CONFIG}
TimeoutStartSec=1200
```

```ini
[Unit]
Description=Run Maestro Symphony signal workflow on trading days

[Timer]
OnCalendar=Mon..Fri 09:10:00
Persistent=true
Unit=maestro-symphony-signal.service

[Install]
WantedBy=timers.target
```

## Legacy Scheduled Run-once Timer

The `maestro-run-once.*` templates are now legacy single-pipeline examples. Use
the Symphony read-only and signal timers above for the three-phase
`symphony_readonly` -> `symphony_signal` -> `symphony_approval` workflow.

For the polling-based Telegram approval flow, stop
`maestro-telegram-operator.service` while `run-once` is active so only one
process consumes Telegram `getUpdates` for the shared bot token. The service
below restarts the Telegram operator when `run-once` exits.

When the shared operator config uses `approval.provider: telegram`, has
`telegram_allowed_chat_ids`, and the configured bot token environment variable
is present, `maestro run-once` sends a Telegram completion or failure
notification to those chats. Notification delivery failures are logged as a
warning and do not turn a successful `run-once` into a failed one; execution
failures still fail the service after the failure notification is attempted.
For `mode: live_approval` with KIS enabled and
`execution.require_reconciliation_pass=true`, the same `run-once` refreshes and
adopts the KIS broker snapshot, records broker reconciliation under the run id,
and only then reaches approval/order gates. A separate reconcile timer can still
be useful for monitoring, but scheduled `run-once` satisfies its own live-order
reconciliation precondition.

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
sudo systemctl enable --now maestro-symphony-readonly.timer
sudo systemctl enable --now maestro-symphony-signal.timer
```

Keep service output in journald or a controlled log sink. Confirm structured
logs do not contain raw secrets before widening access to logs.
