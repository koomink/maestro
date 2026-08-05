# Design: Two-phase rotation execution

**Status:** approved design
**Date:** 2026-08-05
**Problem definition:** [2026-08-05-rotation-sell-then-buy-problem.md](2026-08-05-rotation-sell-then-buy-problem.md)

## Summary

A fully invested Crescendo rotation cannot buy: the cash that funds the buy exists
only because the sell releases it, and every layer that sizes or gates the buy
reads the cash the account holds now. The deepest of those layers,
`TossLiveOrderClient.validate_pre_submit_order`, queries `/api/v1/buying-power`
live at submission time, so no amount of arithmetic netting can pass it.

The fix is execution sequencing: **submit the sells, wait for them to fill, re-read
the broker's buying power, then submit the buys.**

Two supporting changes make that sequencing viable. Orders are priced off the Toss
order book so the sells actually fill, and an aborted rotation cancels its own
outstanding sells so the operator can retry the same day.

## Goals

- A fully invested monthly rotation completes in a single run.
- Orders still leave only at the moment the operator approves them.
- A rotation that cannot complete stops cleanly and leaves the account ready for a
  retry, rather than silently sitting in cash.

## Non-goals

- Market orders. The limit-only invariant stays.
- Automatic intraday retry. A stalled rotation waits for the operator.
- Any change to the 09:40 ET schedule.

## Design

### 1. Two-phase cohort, driven in-process

The `daily-signal-approval` oneshot holds the process until the rotation
finishes. No new service, no new timer. Order submission stays tied to the moment
of operator approval.

- **Cohort unit:** `account_id + currency sleeve`. Cash is fungible at the currency
  sleeve, not the execution sleeve, so that is the boundary that matters. One
  cohort per account+currency at a time, enforced as an invariant. (Today
  `toss_brokerage` USD has exactly one armed execution sleeve, `crescendo_us`;
  `fugue` is `enabled: false`.)
- **Phase A:** submit only the cohort's sells; poll to terminal using the existing
  loop (30s × 20, capped at 1800s).
- **Phase B:** once every sell is filled, re-query broker buying power and submit
  the buys, scaled to the realized figure. Quantity may only go down, never up.
  Whole-share flooring and the 0.2% fee buffer apply.
- **Approval happens once**, up front, covering sells and buys together. Phase B
  stays inside that contract: same symbol, side, and account; quantity only
  reduced; price within the 2% deviation band.
- Cohort state is persisted as DB events for **observability and idempotency
  only** — it does not drive progress. A restart must not resubmit.

### 2. Order-book-based limit pricing

Limit prices are currently `round_price_to_tick(current price)`, which does not
guarantee a fill. Toss exposes `/api/v1/orderbook` with `bids`/`asks` (price and
volume per level) — currently unused by the codebase.

- Walk the book — `bids` for sells, `asks` for buys — to the price level where
  **cumulative volume covers the order quantity**, then take one or two levels
  more as margin. This adapts to the day's spread and depth in a way a fixed
  percentage cannot.
- Because these are limit orders, fills come from the best levels first. The
  posted price is only a worst-case bound, not the expected fill price.
- **Never exceed `max_quote_deviation_pct` (2%).** If the book is too thin to be
  covered inside that band, treat it as "not tradable right now" and abort the
  cohort rather than widening the guard.
- Check the order book's `timestamp` for freshness; the schema allows null. Stale
  or missing → fall back to the current pricing.
- **Toss only.** KIS accounts keep their existing behavior.

### 3. Abort cancels its own sells

If any sell partially fills, is rejected, ends in an unclear state, or cannot be
priced inside the deviation band, the cohort aborts without generating buys. On
abort:

- **Cancel the outstanding sell quantity and confirm the cancellation.** Nothing is
  resubmitted until cancellation is confirmed — a cancel request that succeeds
  without a confirmed state is not a cancellation.
- Notify the operator: what filled, what did not, and the resulting allocation.

Cancellation is not optional. An unfilled order left at the broker trips the
`pending_broker_orders` gate, which blocks the **entire** next run. Without
cancelling, the operator has to wait for the DAY order to expire at the close,
which ends the rotation for that day.

After cancellation the operator simply presses `/rebalance` again. That path
regenerates the signal from scratch and sizes orders from current holdings, so it
converges on the original target — no new convergence logic is needed.

### 4. Disposition of the in-flight changes

| Change | Disposition |
|---|---|
| `order_builder` counts same-batch sell proceeds | Keep — Phase B buy sizing builds on it |
| `order_builder` exits holdings dropped by the target | Keep — correct independently |
| `live_gates` nets sell notional into `required_buying_power` | Keep — approval-time gating still sees the whole cohort. Document in the code that the weakened `buying_power_exceeded` is compensated by Phase B's re-query |
| `live_order_batch` sells-first sort | Superseded by the cohort barrier; keep as a defensive default |

### 5. Capacity partition

`_partition_orders_by_capacity` preserves sell-dependent buys as
`sell_fill_pending` instead of dropping them. The real capacity check moves to
Phase B, against post-sell buying power.

## Error handling

Every failure mode converges on the same behavior: stop, cancel, tell the
operator, stay retryable.

| Situation | Behavior |
|---|---|
| Sell partially filled at deadline | Abort, cancel remainder, notify |
| Sell rejected or status unclear | Abort, cancel remainder, notify |
| Book too thin for the 2% band | Abort before submitting, notify |
| Order book stale or missing | Fall back to current pricing |
| Sells filled but buy rejected | Notify — the account is in cash and needs a retry |
| Process crash mid-cohort | Sells expire as DAY orders; no buys were sent; operator retries |

## Testing

Unit-level: order-book price selection (shallow book, deep book, outside the 2%
band), freshness fallback, Phase B proportional scaling with whole-share flooring
and the approved-quantity ceiling.

Integration: a zero-cash account completes a rotation; a single bad sell blocks
every buy; abort cancels and confirms before anything is resubmitted; a restart
does not resubmit; KIS accounts keep the old pricing path.

Baseline before this work: `1119 passed, 9 skipped`.

## Verification

```bash
cd /root/projects/Symphony/Maestro && uv run pytest -q && uv run ruff check src tests
```

Then observe the next US rotation through `live_order_batch_lifecycle` events:
how long Phase A took to fill, and whether Phase B completed. If unfilled sells
show up in practice, revisit the pricing policy then — not before.
