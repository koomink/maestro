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
EXCHANGERATE_API_KEY=
MAESTRO_CONFIG=/home/symphony/maestro-operator/maestro_personal.yaml
MAESTRO_READONLY_CONFIG=/home/symphony/maestro-operator/symphony_readonly.yaml
MAESTRO_SIGNAL_CONFIG=/home/symphony/maestro-operator/symphony_signal.yaml
MAESTRO_APPROVAL_CONFIG=/home/symphony/maestro-operator/symphony_approval.yaml
```

Do not commit this file. Do not paste secret values into tickets, docs, audit
logs, or dashboard rows.
The ExchangeRate-API free plan supports 1500 requests/month; Maestro's FX
refresh path reuses a successful snapshot for one hour by default, keeping
normal automated usage to about 744 requests in a 31-day month. Use
`maestro fx-refresh --force` only for explicit provider checks.

## Operator Config

Single-profile deployments can use one operator-local config outside the git
checkout, for example `/home/symphony/maestro-operator/maestro_personal.yaml`, and point
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
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro health
```

## Example Read-only Sync Service

```ini
[Unit]
Description=Maestro KIS multi-asset read-only sync

[Service]
Type=oneshot
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro kis-sync
```

## Example Telegram Operator Service

```ini
[Unit]
Description=Maestro Telegram Operator UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro telegram-operator --config ${MAESTRO_READONLY_CONFIG} --signal-config ${MAESTRO_SIGNAL_CONFIG} --timeout-seconds 10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Set `MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS` and
`MAESTRO_TELEGRAM_WHITELISTED_USER_IDS` in the shared Maestro operator
environment. In `live_approval` mode with Telegram approval, these IDs are
required through the shared environment or per-config `telegram_allowed_chat_ids`
and `whitelisted_user_ids` overrides. This service handles
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
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro dashboard --config ${MAESTRO_READONLY_CONFIG} --host 127.0.0.1 --port 8503
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
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro heartbeat
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

Install the `maestro-symphony-readonly`, `-kr`, and `-us` service/timer pairs.
The hourly timer observes all accounts with a 60-minute cache. The KR timer
observes `kis_isa,kis_ps` every 15 minutes from 09:00 through 15:30 KST, and the
US timer observes `toss_brokerage` every 15 minutes from 09:30 through 16:00
America/New_York. Per-account locks collapse overlapping timer, Dashboard, and
Telegram refreshes. Each account is refreshed and reconciled independently;
one failure does not prevent the remaining accounts from being stored.

```ini
[Unit]
Description=Maestro Symphony read-only account refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro kis-sync --config ${MAESTRO_READONLY_CONFIG} --max-age-seconds 3599 --source timer_off_market
TimeoutStartSec=300
```

```ini
[Unit]
Description=Run Maestro Symphony off-market read-only refresh hourly

[Timer]
OnCalendar=hourly
Persistent=true
Unit=maestro-symphony-readonly.service

[Install]
WantedBy=timers.target
```

## Example Symphony Signal and Conditional Approval Timers (Per Market)

Install the per-market pairs `deploy/systemd/maestro-symphony-signal-kr.service`
/ `.timer` and `deploy/systemd/maestro-symphony-signal-us.service` / `.timer`.
Each service calls `maestro daily-signal-approval` scoped with
`--strategy-ids`, which obtains a file lock, refreshes read-only broker state
when configured, runs `maestro run-signal` semantics through
`${MAESTRO_SIGNAL_CONFIG}` for only the listed strategies, sends the daily
Telegram signal summary, and only continues into approval when
`action_required=true`. If any orchestration step fails, it sends a best-effort
Telegram failure briefing before preserving the non-zero systemd failure
status.

Scoping each run to one market keeps the proposed orders inside that market's
session window: a combined KR+US run can never pass the market-session gate for
both exchanges at once, so the unscoped run halts at either trigger time.

During approval polling the command stops `maestro-telegram-operator.service`
and restarts it on exit so the shared Telegram bot has one `getUpdates`
consumer. The legacy `scripts/operator/symphony_signal_then_approval.sh` wrapper
remains available for compatibility, but the systemd timer should use the CLI
orchestrator.

```ini
[Unit]
Description=Maestro Symphony daily signal approval (KR strategies, KRX session)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStart=/home/symphony/maestro/.venv/bin/maestro daily-signal-approval --readonly-config ${MAESTRO_READONLY_CONFIG} --signal-config ${MAESTRO_SIGNAL_CONFIG} --approval-config ${MAESTRO_APPROVAL_CONFIG} --strategy-ids tranquillo
TimeoutStartSec=1200
```

```ini
[Unit]
Description=Run Maestro Symphony KR signal workflow during the KRX session

[Timer]
OnCalendar=Mon..Fri 09:10:00 Asia/Seoul
Persistent=true
Unit=maestro-symphony-signal-kr.service

[Install]
WantedBy=timers.target
```

The US pair is identical except for `--strategy-ids crescendo_us` and
`OnCalendar=Mon..Fri 09:40:00 America/New_York`. The KR trigger covers the KRX
session shortly after the Seoul open; the US trigger covers US-listed symbols
shortly after the New York open, and systemd handles daylight-saving
transitions through the explicit timezone. Both services share the default
`/tmp/maestro-symphony-signal.lock`, so overlapping runs cannot race the state
DB. The unscoped `maestro-symphony-signal.service` remains available for
manual full runs, but its combined dual-timezone timer should stay disabled.

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
User=symphony
Group=symphony
UMask=0077
WorkingDirectory=/home/symphony/maestro
EnvironmentFile=/etc/maestro/maestro.env
ExecStartPre=-/bin/systemctl stop maestro-telegram-operator.service
ExecStart=/home/symphony/maestro/.venv/bin/maestro run-once
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
sudo systemctl enable --now maestro-symphony-readonly-kr.timer
sudo systemctl enable --now maestro-symphony-readonly-us.timer
sudo systemctl enable --now maestro-symphony-signal.timer
```

Keep service output in journald or a controlled log sink. Confirm structured
logs do not contain raw secrets before widening access to logs.

## Dashboard reliability (auto-reload + auto-heal)

The dashboard runs as a long-lived process, so a config or code change does not
take effect until the service is restarted. If it is not restarted, the daemon
keeps serving stale models and the operator config can fail validation, which
the dashboard now surfaces as a clear `422 config_invalid` instead of a blank
`500`. Three extra units keep it reliable and current:

- `maestro-dashboard.service` — adds `StartLimitIntervalSec=0` so systemd never
  stops retrying restarts (the dashboard always comes back).
- `maestro-dashboard.path` + `maestro-dashboard-reload.service` — watch the
  operator config files and the built frontend (`src/maestro/dashboard/web`) and
  restart the dashboard automatically when they change (e.g. after a deploy or a
  config edit). Adjust the watched paths in the `.path` unit to match your
  operator config locations.
- `maestro-dashboard-health.timer` + `maestro-dashboard-health.service` — probe
  `/api/health` every minute and restart the dashboard only if it is unreachable
  (hung/dead). A reachable server returning an HTTP error is left alone so a bad
  config is diagnosed in the UI rather than triggering a restart loop.

Install / refresh:

```bash
sudo cp deploy/systemd/maestro-dashboard.service \
        deploy/systemd/maestro-dashboard.path \
        deploy/systemd/maestro-dashboard-reload.service \
        deploy/systemd/maestro-dashboard-health.service \
        deploy/systemd/maestro-dashboard-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart maestro-dashboard.service          # pick up the new ExecStart
sudo systemctl enable --now maestro-dashboard.path
sudo systemctl enable --now maestro-dashboard-health.timer
```

Verify:

```bash
systemctl status maestro-dashboard.service maestro-dashboard.path maestro-dashboard-health.timer
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8503/api/health   # 200
```

Frontend deploys: after `npm run dashboard:build` writes to
`src/maestro/dashboard/web`, the `.path` unit restarts the dashboard within a
few seconds. The frontend itself self-heals — it polls the snapshot on an
interval with backoff and keeps the last good data on screen during a restart,
so clients reconnect automatically.

## FX rate freshness

`fx-refresh` is the only place USD/KRW rates get written; no other scheduled
job (`kis-sync`, `reconcile`, `daily-signal-approval`) touches FX at all, and
those only run once or twice a day around market open
(`maestro-symphony-signal.timer` fires weekdays at 09:10 KST / 09:40 America/
New York). FX's own staleness threshold is 4h (`fx.stale_after_seconds`,
default 14400s), so without a dedicated periodic refresh it goes stale every
afternoon and stays stale overnight and on weekends until someone manually
clicks "Refresh" in the dashboard — at which point every total that requires a
currency conversion (Total assets in KRW/USD, Portfolio Pulse cash/exposure,
the speedometer) reports `n/a` instead of a number.

`maestro-fx-refresh.timer` runs `maestro fx-refresh` hourly to keep it
comfortably ahead of that 4h threshold. The call is read-only, hits only the
external FX provider (no broker credentials, no KIS rate limits), and is
internally throttled to `fx.refresh_interval_seconds` (default 3600s), so
running it hourly is exactly matched to the throttle — no wasted provider
calls, and a single missed run still leaves hours of buffer before staleness.

Install:

```bash
sudo cp deploy/systemd/maestro-fx-refresh.service \
        deploy/systemd/maestro-fx-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-fx-refresh.timer
```

Verify:

```bash
systemctl list-timers maestro-fx-refresh.timer
journalctl -u maestro-fx-refresh.service -n 5 --no-pager
```

## Outstanding live order tracking

The live order lifecycle polls order status on a bounded loop
(`order_status_max_polls` x `order_status_poll_interval_seconds`, 20 x 30s =
10 minutes in the operator config). An order still working at the last poll is
left live at the broker with nobody watching it, and fill reconciliation replays
recorded `live_order_status` events, so a fill arriving after that point can
never be applied — `reconcile-fills` and `recover-live-order` both replay the
same events and find nothing. The position then drifts until someone adopts the
broker snapshot by hand, losing the real fill quantity, price, and settlement
costs.

The lifecycle now records `live_order_tracking_incomplete` and notifies when the
window closes on a working order, including when a post-fill reconciliation exits
before the broker order is terminal. `maestro resume-order-tracking` re-polls those
orders so the fill lands on the normal reconciliation path and the operator gets a
Telegram message with the outcome. As a recovery fallback it also discovers
accepted `live_order_result` records that have no later terminal status snapshot;
this repairs orders created before tracking-incomplete events were available.

For Toss, raw working statuses such as `PENDING` are normalized to `OPEN` in the
read-only snapshot. Cash reconciliation also adds the `reserved_cash` of unfilled
buy orders back to buying power before comparing it with the Maestro cash ledger,
so a normal broker reservation is not escalated as an L3 cash drift.

If a scheduled broker snapshot observes a strategy sell before its delayed fill
is reconciled, attribution records an `external_strategy_reduction_warning` and
may remove the position first. Restore only that warning-backed quantity, then
replay fills through the normal idempotent path:

```bash
maestro restore-pending-maestro-sell-attribution \
  --config <readonly-config> \
  --account-id toss_brokerage \
  --symbol <symbol> \
  --bucket crescendo_us \
  --quantity <filled-quantity> \
  --reason "broker snapshot preceded delayed Maestro fill reconciliation"
maestro reconcile-fills --config <readonly-config>
```

The restore command rejects quantities not backed by an unclaimed strategy
reduction warning and writes an audited attribution version. Do not use snapshot
adoption to repair this case because it loses the fill's quantity, price, and cash
provenance.

`maestro-resume-order-tracking.timer` runs it every 2 minutes during KRX and US
market hours. That bounds notification latency after the inline 10-minute window
to 2 minutes. When nothing is outstanding the run makes no broker API calls,
writes no events, and finishes in about half a second, so a short period is
cheap. The US windows cover both EDT (22:30-05:00 KST) and EST
(23:30-06:00 KST).

Note this deliberately does **not** raise `live_order_recovery_required`: that
blocks all live trading until an operator clears it, and a limit order still
working after ten minutes is normal. A poll that cannot reach the broker exits
non-zero so the failure surfaces, and one unreachable order does not stop the
others from being recovered.

Install:

```bash
sudo cp deploy/systemd/maestro-resume-order-tracking.service \
        deploy/systemd/maestro-resume-order-tracking.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-resume-order-tracking.timer
```

Verify:

```bash
systemctl list-timers maestro-resume-order-tracking.timer
journalctl -u maestro-resume-order-tracking.service -n 5 --no-pager
```
