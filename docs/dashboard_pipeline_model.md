# Dashboard Pipeline Model

This document records the current Maestro Dashboard pipeline model for later production use.

## Placement

The pipeline/map belongs inside the `Maestro` dashboard tab. It is not the whole dashboard. The overall dashboard tab order remains:

1. `Portfolio`
2. `Maestro`
3. `Virtuoso`

## Core Flow

The map should show operational relationships, not pretend that accounts consume market data.

Primary strategy flow:

```text
Data Inputs -> Virtuoso Apps -> Accounts -> Operating Gate
```

Broker/read-model flow:

```text
Broker Sync -> Accounts -> State / Audit
```

Operator config controls the bindings and posture:

```text
Operator Config -> Virtuoso Apps
Operator Config -> Accounts
Operator Config -> Operating Gate
```

## Entity Groups

### Data Inputs

Strategy-facing inputs only:

- `Market`: Yahoo price data and benchmark context
- `Macro`: FRED macro context
- `Narrative`: news, GDELT, sentiment; optional or inactive until wired

Do not show `DEV_sandbox` or fixtures as data providers in the main graph. They are test/account context, not production strategy data feeds.

### Virtuoso Apps

Show all configured apps, even when not selected. Non-selected apps may fade visually, but their key information must remain readable.

Current mock entities:

- `Tranquillo`: enabled, fresh signal, buy-only contribution intent
- `Crescendo`: enabled, target allocation intent, signal missing
- `Fugue`: disabled, target allocation intent off

### Accounts

Accounts are target/synced state objects. Show all current accounts:

- `KIS_mock`
- `KIS_ps`
- `DEV_sandbox`
- `KIS_brokerage`

`KIS REST` / broker readonly sync belongs near Accounts as a broker sync source, not inside Data Inputs.

### Signal Intent

Do not use a separate Signal Package column in the high-level map. Show signal intent as a label on the app-to-account route.

Examples:

- `Tranquillo -> KIS_mock`: `BUY-ONLY CONTRIBUTION`
- `Crescendo -> DEV_sandbox`: `TARGET ALLOCATION`
- `Fugue -> DEV_sandbox`: `TARGET ALLOCATION`

### Operating Gate

Combine approval, live execution posture, Telegram approval, execution, and audit into one stage:

```text
Operating Gate = Telegram approval + execution posture + audit trail
```

The gate must clearly show whether it is read-only, dry-run, approval-gated, or live-capable.

### State / Audit

Show the published read model and audit state as the final visible state object. This is where the dashboard reads operational truth from.

## Motion Rules

Motion communicates how far the system is currently allowed to operate.

- `readonly`: only broker/account sync and read-model publication may glow. Data -> Virtuoso and Virtuoso -> Account signal routes must not glow.
- `signal`: data and signal intent routes may glow, but operating gate/live execution remains locked.
- `approval/live`: signal route and operating gate route may glow only when approval/live posture is enabled.
- inactive, missing, disabled, or stale routes stay visible but do not glow.

## Visual Rules

- Prefer a professional black operations surface.
- Avoid toy-like flowchart styling.
- Keep object groups readable: Data, Virtuoso, Accounts, Operating Gate, Broker Sync, State/Audit.
- Use route labels instead of extra repeated Signal/Risk nodes.
- Selected app may receive stronger emphasis, but all other apps/accounts must remain legible.
- The map should support both Palantir Gotham style and Bloomberg Terminal style without changing the underlying information architecture.

## Current Design Question

The next decision is the overall Dashboard design concept:

- Bloomberg Terminal: dense, fast-scanning, market-console feel.
- Palantir Gotham: ontology-first, object relationship, operational command feel.

The comparison mockup uses the same layout for both concepts so the choice is about visual language and operator experience, not information architecture.
