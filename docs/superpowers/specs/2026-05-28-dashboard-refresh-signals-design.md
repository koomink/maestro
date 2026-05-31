# Dashboard Refresh and Virtuoso Signal Design

## Summary

The Dashboard should keep global refresh and strategy signal generation as two
separate operator actions.

Global `Refresh` updates read-only broker/account state and checks whether the
latest persisted signals are fresh. It does not run Virtuoso apps. Signal
generation belongs inside the `Virtuoso` tab, where the operator can choose one
specific app and generate a new signal for that app only.

This keeps the Dashboard useful as an operator cockpit while preserving the
safety boundary: the Dashboard may refresh read-only state and generate
proposal-only signals, but it must not approve, submit, cancel, or recover
orders.

## Goals

- Let the operator update account/broker truth from the Dashboard.
- Show whether the latest persisted signal state is fresh, stale, missing, or
  failed.
- Add a `Virtuoso` overview sub-tab for all strategy apps.
- Add one sub-tab per Virtuoso app.
- Add per-app `Generate Signal` controls in each app sub-tab.
- Keep `Generate All Signals` deferred.
- Keep approval and execution out of the Dashboard.

## Non-Goals

- No order execution from the Dashboard.
- No approval consumption from the Dashboard.
- No live order submit/cancel/recover controls.
- No strategy enable/disable controls.
- No account/config/admin editing.
- No global `Generate All Signals` button in the first implementation.

## Interaction Model

### Global Refresh

`Refresh` becomes a read-only operational refresh:

1. Refresh all configured KIS account snapshots, equivalent to the safe parts
   of `maestro kis-sync`.
2. Recompute latest signal freshness from persisted signal packages and
   per-strategy run records.
3. Reload the Dashboard snapshot.

`Refresh` must not run strategies, create approvals, or submit orders.

Suggested endpoint:

```text
POST /api/dashboard/refresh
```

Suggested response:

```json
{
  "status": "ok",
  "accounts_synced": 2,
  "signal_freshness": {
    "overall": "stale",
    "strategies": [
      {
        "strategy_id": "tranquillo",
        "status": "stale",
        "latest_signal_run_id": "sig_...",
        "latest_signal_at": "2026-05-28T09:10:00+09:00"
      }
    ]
  }
}
```

The frontend should show progress and the last refresh result, then fetch the
normal Dashboard snapshot again.

### Virtuoso Tab

The `Virtuoso` top-level tab should contain sub-tabs:

```text
Overview
Tranquillo
Crescendo US
Fugue
...
```

`Overview` shows all app states in one place:

- configured app id
- Dashboard-visible/read-only state
- signal generation availability
- account mapping
- order posture
- latest strategy run
- latest signal age/freshness
- latest validation state
- latest action-required/no-op state when available

Each app sub-tab shows that app's details:

- latest signal summary
- latest strategy run
- validation result
- data/evidence summary
- account mapping
- strategy book performance
- `Generate Signal` button

### Per-App Generate Signal

Each app sub-tab may show `Generate Signal` when the app is signal-capable in
the configured signal profile.

Suggested endpoint:

```text
POST /api/dashboard/virtuoso/{strategy_id}/generate-signal
```

The backend should run exactly one requested strategy and persist a normal
signal package/run record for that strategy. It must not approve or execute the
signal.

Suggested response:

```json
{
  "status": "ok",
  "strategy_id": "tranquillo",
  "signal_run_id": "sig_...",
  "action_required": true,
  "orders_preview_count": 3
}
```

## Backend Design

The Dashboard server should know two configs:

- `--config`: read-only Dashboard and broker refresh config.
- `--signal-config`: signal-generation config used by Virtuoso actions.

Example:

```bash
maestro dashboard \
  --config configs/operator/symphony_readonly.yaml \
  --signal-config configs/operator/symphony_signal.yaml
```

The read-only config drives Dashboard state, health, account sync, and snapshot
rendering. The signal config drives per-app signal generation. Both configs
should share the same `state.identity_group` and runtime state/audit paths unless
the operator intentionally separates them.

Per-app signal generation requires a backend capability equivalent to:

```python
run_signal(strategy_ids=["tranquillo"])
```

The existing all-strategy `run_signal()` behavior should remain available for
CLI/scheduled workflows. The Dashboard endpoint should use the filtered form so
one app button cannot accidentally run every signal-enabled strategy.

## Frontend Design

`Refresh` remains in the top bar, but its copy should make the heavier behavior
visible through progress/status text such as:

```text
Refreshing account state...
Checking signal freshness...
Updated 2 accounts, latest signal stale
```

The `Virtuoso` tab should replace the current single selected-app layout with
a sub-tab layout:

- `Overview` first.
- One tab per strategy app.
- No `Generate All Signals` control.
- App tabs show `Generate Signal` only when available.
- Disabled/unavailable buttons should explain why in text near the button,
  for example `Signal disabled in signal config` or `Missing signal config`.

## Safety Rules

- Dashboard refresh may call read-only broker APIs.
- Dashboard signal generation may run strategy code and persist proposal-only
  signal artifacts.
- Dashboard must not call approval consumption or live order execution paths.
- Dashboard must not mutate configs, safety state, account mappings, or strategy
  enabled flags.
- All state-changing Dashboard endpoints must use `POST`.
- Endpoint responses should include a concise diagnostic payload for the Console
  drawer.

## Testing

- Unit-test signal freshness classification: fresh, stale, missing, failed.
- Unit-test per-app signal filtering so only the requested strategy runs.
- API-test `POST /api/dashboard/refresh` with fake KIS clients.
- API-test `POST /api/dashboard/virtuoso/{strategy_id}/generate-signal`:
  known strategy, unknown strategy, signal-disabled strategy, missing
  `--signal-config`.
- Frontend-test that `Refresh` does not call generate-signal endpoints.
- Frontend-test `Virtuoso` sub-tabs and per-app button disabled states.
- Regression-test that Dashboard endpoints never approve or execute orders.

## Open Decisions

- Freshness threshold for signals should reuse the existing approval
  `signal_max_age_seconds` unless a separate Dashboard threshold is needed.
- The first implementation should decide whether `Refresh` also runs
  reconciliation after KIS sync. The default should be broker snapshot refresh
  plus freshness classification only.
