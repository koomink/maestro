# Market-Separated KIS Profiles

Maestro operator configs can separate KIS real-account operation by account and market:

- `kis_isa`: ISA account, KRW/KRX only, Ataraxia monthly buy-only contribution.
- `kis_brokerage_us`: brokerage account, USD/US only, Snowball US target rebalance.
- `kis_brokerage_kr`: brokerage account, KRW/KRX only, read-only scaffold for later use.

This keeps ISA, Korean brokerage, and US brokerage operations in separate state, audit,
token-cache, approval, and scheduling surfaces. The split also avoids automatic FX conversion
or cross-market order generation.

## Profiles

`configs/operator/kis_isa.yaml` uses `KIS_ISA_ACCOUNT_ID`, `kis_domestic_stock`, and a
KRW sleeve. It enables only Ataraxia and schedules buy-only contribution orders for day 25.

`configs/operator/kis_brokerage_us.yaml` uses `KIS_BROKERAGE_ACCOUNT_ID`,
`kis_overseas_stock`, and a USD sleeve. It enables only `snowball_us` and uses target
rebalance order generation. Existing US holdings should be adopted at the profile level with
`kis-sync` and `adopt-broker-snapshot` before live approval rehearsals.

`configs/operator/kis_brokerage_kr.yaml` uses the same brokerage account environment variable
with `kis_domestic_stock`, but starts in `live_readonly` with no enabled strategies. It is a
safe scaffold for later Korean-market brokerage strategies.

## Operation

Run each profile with its own config path:

```bash
maestro kis-sync --config configs/operator/kis_isa.yaml
maestro adopt-broker-snapshot --config configs/operator/kis_isa.yaml --reason "verified ISA baseline"
maestro run-once --config configs/operator/kis_isa.yaml

maestro kis-sync --config configs/operator/kis_brokerage_us.yaml
maestro adopt-broker-snapshot --config configs/operator/kis_brokerage_us.yaml --reason "verified US brokerage baseline"
maestro run-once --config configs/operator/kis_brokerage_us.yaml
```

Do not edit these files per run. Keep account IDs, API keys, and Telegram credentials in
environment variables. Before arming live orders, verify the broker snapshot, reconciliation,
approval channel, and profile-specific state/audit paths.
