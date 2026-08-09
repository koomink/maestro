# Problem: A fully invested rotation cannot buy

**Status:** problem definition, no solution chosen
**Date:** 2026-08-05
**Repo:** `Symphony/Maestro` (nested git repo; working tree is dirty by convention)

## Summary

When the Crescendo strategy rotates out of one asset and into another, the buy
leg never reaches the broker. The account is fully invested at rebalance time,
so the cash that funds the buy exists only because the sell in the same batch
releases it — and every layer that sizes or gates the buy looks at cash the
account holds *now*.

The result: the sell goes out, the buy is dropped, and the book sits in cash
until the next monthly run. The rotation lags one full cycle, every cycle.

Four layers have been fixed (see [What has already changed](#what-has-already-changed)).
One layer has not, and it is the one that talks to the broker. Fixing it with
arithmetic is not possible — it requires the sells to actually fill first, which
is an execution-sequencing change, not a calculation change.

## Context

`crescendo_us` is a monthly dynamic-asset-allocation strategy. It runs four
sub-strategies (DGA, Accelerated Dual Momentum, GTT-UE, BAA(A)) at equal weight,
and each one picks essentially a single asset per month
(`Virtuoso/virtuoso-crescendo/src/crescendo/strategy.py`). A month-over-month
change in any sub-strategy's pick produces a full-size sell of last month's
asset and a full-size buy of this month's.

Live wiring (`/root/maestro-operator/symphony_signal.yaml`, mirrored at
`configs/operator/symphony_signal.yaml`):

- `mode: live_approval`
- `portfolio.allocation_mode: currency_sleeves`, with a `USD` sleeve
- `crescendo_us` runs with `config.sleeve: USD`, `execution_sleeve: crescendo_us`
- `execution.order_generation_mode: target_rebalance`
- `portfolio.unknown_broker_position_policy: include_readonly`

So the live path goes through `OrderBuilder._build_sleeve_orders`, not the
flat-allocation path.

## The order pipeline

| # | Layer | File | Nets sell proceeds? |
|---|-------|------|---------------------|
| 1 | Order sizing | `src/maestro/execution/order_builder.py` | ✅ fixed |
| 2 | Risk gate | `src/maestro/orchestration/live_gates.py` | ✅ fixed |
| 3 | **Broker capacity partition** | `src/maestro/execution/order_capacity.py` | ❌ **still drops the buy** |
| 4 | Batch submission | `src/maestro/execution/live_order_batch.py` | ordering only, no fill wait |
| 5 | Broker acceptance (KIS) | — | unverified |

### Layer 3 is the blocker

`MaestroOrchestrator._partition_orders_by_capacity`
(`src/maestro/orchestration/orchestrator.py:2064`, called at `:686` just before
the approval package is built) runs in `LIVE_APPROVAL` mode and calls
`OrderCapacityService.partition`. That method (`order_capacity.py:38-88`):

```python
if order.side != OrderSide.BUY:
    accepted.append(order)          # sells skip the check entirely
    continue
...
available_cash = max(0.0, capacity.cash_buying_power - reserved)
if order.notional > available_cash + 1e-9:
    reason = "cash_buying_power_exceeded"
```

`capacity.cash_buying_power` is fetched live from the broker
(`orchestrator._lookup_order_capacity` → `client.get_buying_power`). On a fully
invested account it is approximately zero, so the buy is blocked and only the
sell survives.

**This layer cannot be fixed by netting.** Layers 1 and 2 are Maestro's own
projections, so adding expected sell proceeds there is legitimate. Layer 3 is a
reading of actual broker state; adding unfilled sell proceeds to it would be
asserting something false about the account, and the order would simply be
rejected downstream by the broker instead.

Blocked buys are recorded as `live_order_capacity_blocked` events and become
Telegram recovery candidates (`integrations/telegram/handlers.py:3804`).

## What has already changed

All four changes are green (`1119 passed, 9 skipped`) but **uncommitted**.

1. **`order_builder.py` — buys sized against same-batch sell proceeds.**
   `_scale_buy_orders_to_cash` now adds `_sell_proceeds(orders)` (sell notional
   net of `fee_buffer_pct`) to the spendable cash. Called per sleeve, so
   proceeds cannot cross a currency boundary.

2. **`order_builder.py` — holdings dropped by the target are now exited.**
   The flat-allocation path used to iterate only `target.allocations`, so a
   position missing from today's target was never sold. It now iterates
   `target symbols ∪ held positions`, bounded by `_exitable_positions`, which
   only includes symbols present in `universe.instruments` — otherwise
   `unknown_broker_position_policy: include_readonly` would cause unrelated
   broker holdings to be liquidated. (The sleeve path already did this
   correctly, bounded by `currency_sleeves.<cur>.symbols`.)

3. **`live_order_batch.py` — sells submit before buys.**
   `_sells_first` stable-sorts the batch. Previously the builder's symbol
   ordering put buys first.

4. **`live_gates.py` — `required_buying_power` nets sell notional.**
   Was `buy_notional + fee_buffer` compared against pre-sell broker buying
   power, which blocked every rotation with `buying_power_exceeded`. The
   sibling line `cash_after_orders` already netted sells. Now
   `max(0.0, buy_notional - sell_notional) + fee_buffer`.
   **Note this change is optimistic**: it passes a buy that depends on a sell
   that has not filled. It is a prerequisite for the two-phase design below, not
   a standalone fix.

New tests: `tests/test_target_rebalance_orders.py` (7). Modified:
`tests/test_kis_multi_asset.py` (3 expectations that encoded the old behavior),
`tests/test_live_approval_run_once.py` (1), `tests/test_live_order_batch.py`
(2 new), `tests/test_v07_production_hardening.py` (1 new).

### Why this is not enough

Paper mode is fully fixed — `PaperExecutionEngine.execute_orders` fills
everything unconditionally without a cash check.

Live mode is not. Layer 3 still drops the buy. And even if layer 3 were passed,
layer 4 submits the sell and the buy back-to-back without waiting for a fill, so
the outcome depends entirely on whether KIS credits unsettled or unfilled sell
proceeds to overseas-stock buying power — which is account-configuration
dependent and unverified.

Partial fills are unhandled at every layer: a 50%-filled sell still leaves a
full-size buy that cannot be funded.

## Proposed direction (not yet decided)

Two-phase execution:

- **Phase A** — submit sells only; poll to terminal status.
- **Phase B** — re-query broker buying power; re-size buys from *realized*
  proceeds; run the capacity partition; submit.

A partial sell fill naturally produces a proportionally smaller buy. A sell that
never fills produces no buy, and the next scheduled run re-proposes.

Reusable machinery that already exists:

- `LiveOrderBatchLifecycleService` already polls in rounds until terminal
  (`live_order_batch.py:136`). `PARTIALLY_FILLED` is deliberately not in
  `_TERMINAL_STATUSES` (`:412`), so partial fills keep being tracked.
- `LiveOrderModificationService.modify_order` can reprice or shrink an
  OPEN/`PARTIALLY_FILLED` order (`live_order_modification.py:33`), but requires
  a matching Telegram `ApprovalDecision`.
- Capacity-blocked buys already become manual retry candidates, retryable the
  same trading day only (`handlers.py:1589`).

## Open questions

### Q1 (blocking) — Does resizing a buy stay within operator approval?

The operator approves a specific order via Telegram ("buy 100 TLT"). If the sell
fills 90%, phase B would submit 90 shares. Is a downward resize inside the
approved scope, or does it require re-approval? Upward resizing must never
happen.

This determines whether phase B can be automatic or must round-trip through
Telegram — which in turn determines whether the rotation can complete inside one
run.

### Q2 — What is the end state when the rotation only partly completes?

US ETF instruments use `quantity_step: 1` / `min_order_quantity: 1` (whole
shares), and buy quantity is floored (`order_builder.py:264`), so every rotation
leaves sub-one-share residual cash regardless of fills. A partial sell leaves a
proportionally larger gap.

- (a) Accept it; the next monthly rebalance absorbs the drift. Simple.
- (b) Chase the remainder intraday via the existing recovery path.
- (c) Redefine "buy only what was actually sold" as normal completion.

The buy vanishing entirely requires realized proceeds below one share's price
(<1% fill on a typical $10k sleeve) — rare, so this is really a question about
tolerating drift, not about the minimum-quantity rule.

### Q3 — Should rotation sells be priced more aggressively?

Phase B cannot start until the phase A sell reaches terminal status, so sell
fill probability determines whether the rotation happens at all.

Constraints:

- **Market orders are structurally forbidden.** `allowed_order_type: limit`, and
  a config validator rejects anything else (`config/execution.py:326`).
- Limit price is `round_price_to_tick(current price)`, and `round_price_to_tick`
  truncates (`order_builder.py:27`) — so a sell lands one tick below the quote
  (marginally aggressive) and a buy one tick below (marginally passive).
- The gate allows up to `max_quote_deviation_pct: 0.02` (2%) deviation from the
  broker quote, so there is headroom to price sells below the quote.
- Quotes come from a broker snapshot that may be minutes stale, and a
  KR-timezone run may be pricing against a non-RTH US quote.
- Polling budget: `order_status_max_polls: 20` × `order_status_poll_interval_seconds: 30`,
  capped by `order_status_terminal_timeout_seconds: 1800`.

Options: leave as-is; price rotation sells at quote − N bp; or reprice during
polling (needs the approval-identity question resolved).

The trade-off is explicit slippage cost versus rotation completion rate. This is
a cost judgement for the operator, not a technical default.

## How to verify

```bash
cd /root/projects/Symphony/Maestro && uv run pytest -q
```

Relevant files:

- `src/maestro/execution/order_builder.py`
- `src/maestro/execution/order_capacity.py`
- `src/maestro/execution/live_order_batch.py`
- `src/maestro/orchestration/orchestrator.py` (`_partition_orders_by_capacity`)
- `src/maestro/orchestration/live_gates.py` (`_cash_and_exposure_risk_issues`)
- `tests/test_target_rebalance_orders.py`
- `/root/maestro-operator/symphony_signal.yaml` (deployed live config)

The project follows strict TDD — see `AGENTS.md`. Write the failing test first
and watch it fail before implementing.
