# Two-Phase Rotation Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a fully invested Crescendo rotation complete in one run by submitting its sells, waiting for them to fill, then sizing and submitting its buys against the broker's real post-sell buying power.

**Architecture:** The orchestrator owns the two-phase flow and calls the existing `LiveOrderBatchLifecycleService` twice — once with the cohort's sells, once with its buys. The batch service stays an unchanged "submit and track" primitive. Between phases the orchestrator re-queries broker buying power and shrinks the approved buys to fit. If any sell fails to fill completely, the cohort aborts, cancels its own outstanding sells, and notifies the operator so they can retry the same day.

**Tech Stack:** Python 3, Pydantic v2, pytest, ruff, uv.

**Spec:** [docs/superpowers/specs/2026-08-05-rotation-two-phase-execution-design.md](../specs/2026-08-05-rotation-two-phase-execution-design.md)
**Problem definition:** [docs/superpowers/specs/2026-08-05-rotation-sell-then-buy-problem.md](../specs/2026-08-05-rotation-sell-then-buy-problem.md)

## Global Constraints

- Run everything with `uv run` — `python` is not on PATH. Tests: `uv run pytest -q`. Lint: `uv run ruff check src tests`.
- Baseline before this plan: `1119 passed, 9 skipped`. Every task must end green.
- Strict TDD. Write the failing test, run it, watch it fail for the right reason, then implement. See `AGENTS.md`.
- `ruff format` is NOT clean on this repo's existing files. Run `uv run ruff check`, not `ruff format`, or you will produce unrelated diff noise.
- Limit orders only. Do not touch `LiveOrderRequest.validate_limit_order` or the `allowed_order_type` validator in `src/maestro/config/execution.py`.
- Order submission stays tied to operator approval. Do not add a background job that submits orders.
- Phase B may only reduce approved quantities, never increase them.
- Out of scope for this plan: order-book-based limit pricing (separate plan), market orders, automatic intraday retry.

## File Structure

| File | Responsibility |
|---|---|
| `src/maestro/execution/rotation_cohort.py` (new) | Pure logic: group orders into cohorts, judge whether a sell phase completed, shrink buys to available cash. No I/O. |
| `src/maestro/execution/order_builder.py` | Add module-level `floor_quantity_to_step` for reuse by cohort rescaling. |
| `src/maestro/execution/order_capacity.py` | Keep sell-funded buys instead of blocking them. |
| `src/maestro/orchestration/orchestrator.py` | Own the two-phase flow, the between-phase buying-power re-query, and the abort path. |
| `tests/test_rotation_cohort.py` (new) | Unit tests for the pure logic. |
| `tests/test_two_phase_rotation.py` (new) | Orchestrator-level two-phase and abort tests. |
| `tests/test_order_capacity.py` | Extended for sell-funded buys. |

---

### Task 1: Cohort splitting

Cash is fungible per account and currency — not per execution sleeve — so that pair is the boundary at which one order's proceeds can fund another.

**Files:**
- Create: `src/maestro/execution/rotation_cohort.py`
- Test: `tests/test_rotation_cohort.py`

**Interfaces:**
- Consumes: `OrderIntent` from `maestro.execution.base`, `OrderSide` from `maestro.core.enums`.
- Produces: `RotationCohort(account_id, currency, sells, buys)` and `split_rotation_cohorts(orders: list[OrderIntent]) -> list[RotationCohort]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rotation_cohort.py`:

```python
from maestro.core.enums import Currency, OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.rotation_cohort import split_rotation_cohorts


def _order(order_id: str, side: OrderSide, currency: Currency, account_id: str = "toss") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol="QQQ",
        side=side,
        quantity=1,
        price=100.0,
        notional=100.0,
        currency=currency,
        account_id=account_id,
    )


def test_split_groups_by_account_and_currency():
    orders = [
        _order("usd_sell", OrderSide.SELL, Currency.USD),
        _order("usd_buy", OrderSide.BUY, Currency.USD),
        _order("krw_sell", OrderSide.SELL, Currency.KRW),
    ]

    cohorts = split_rotation_cohorts(orders)

    by_currency = {cohort.currency: cohort for cohort in cohorts}
    assert set(by_currency) == {"USD", "KRW"}
    assert [order.order_id for order in by_currency["USD"].sells] == ["usd_sell"]
    assert [order.order_id for order in by_currency["USD"].buys] == ["usd_buy"]
    assert [order.order_id for order in by_currency["KRW"].sells] == ["krw_sell"]
    assert by_currency["KRW"].buys == ()


def test_split_keeps_accounts_apart_within_one_currency():
    orders = [
        _order("a_sell", OrderSide.SELL, Currency.USD, account_id="toss"),
        _order("b_buy", OrderSide.BUY, Currency.USD, account_id="kis"),
    ]

    cohorts = split_rotation_cohorts(orders)

    assert {cohort.account_id for cohort in cohorts} == {"toss", "kis"}
    assert all(len(cohort.sells) + len(cohort.buys) == 1 for cohort in cohorts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_cohort.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'maestro.execution.rotation_cohort'`

- [ ] **Step 3: Write minimal implementation**

Create `src/maestro/execution/rotation_cohort.py`:

```python
from dataclasses import dataclass

from maestro.core.enums import OrderSide
from maestro.execution.base import OrderIntent


@dataclass(frozen=True)
class RotationCohort:
    """Orders that draw on one broker cash pool.

    A sell can only fund a buy that settles against the same account and
    currency, so that pair — not the execution sleeve — is the boundary the
    two-phase barrier runs on.
    """

    account_id: str | None
    currency: str | None
    sells: tuple[OrderIntent, ...]
    buys: tuple[OrderIntent, ...]


def _cohort_key(order: OrderIntent) -> tuple[str | None, str | None]:
    currency = order.currency.value if order.currency is not None else order.sleeve
    return (order.account_id, currency)


def split_rotation_cohorts(orders: list[OrderIntent]) -> list[RotationCohort]:
    """Group orders into one cohort per account and currency, order preserved."""
    grouped: dict[tuple[str | None, str | None], tuple[list[OrderIntent], list[OrderIntent]]] = {}
    for order in orders:
        sells, buys = grouped.setdefault(_cohort_key(order), ([], []))
        if order.side == OrderSide.SELL:
            sells.append(order)
        else:
            buys.append(order)
    return [
        RotationCohort(
            account_id=account_id,
            currency=currency,
            sells=tuple(sells),
            buys=tuple(buys),
        )
        for (account_id, currency), (sells, buys) in grouped.items()
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_cohort.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/maestro/execution/rotation_cohort.py tests/test_rotation_cohort.py
git commit -m "feat: group rotation orders into account+currency cohorts"
```

---

### Task 2: Sell-phase completion check

Buys may only follow a sell phase where every sell filled completely. A partial fill, a rejection, or an order still working at the deadline all mean the cash is not there.

**Files:**
- Modify: `src/maestro/execution/rotation_cohort.py`
- Test: `tests/test_rotation_cohort.py`

**Interfaces:**
- Consumes: `LiveOrderLifecycleResult`, `OrderStatus`.
- Produces: `SellPhaseOutcome(complete: bool, reason: str | None, unfilled: tuple[LiveOrderLifecycleResult, ...])` and `evaluate_sell_phase(results: list[LiveOrderLifecycleResult]) -> SellPhaseOutcome`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rotation_cohort.py`:

```python
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_models import LiveOrderLifecycleResult
from maestro.execution.rotation_cohort import evaluate_sell_phase


def _lifecycle(order_id: str, status: OrderStatus) -> LiveOrderLifecycleResult:
    return LiveOrderLifecycleResult(
        run_id="run_1",
        order_id=order_id,
        final_status=status,
        broker_order_id=f"broker_{order_id}",
        checked_at="2026-08-05T13:40:00+00:00",
    )


def test_sell_phase_completes_when_every_sell_filled():
    outcome = evaluate_sell_phase(
        [_lifecycle("a", OrderStatus.FILLED), _lifecycle("b", OrderStatus.FILLED)]
    )

    assert outcome.complete is True
    assert outcome.unfilled == ()


def test_sell_phase_with_no_sells_completes():
    # A buy-only rebalance has nothing to wait for.
    assert evaluate_sell_phase([]).complete is True


def test_partially_filled_sell_blocks_the_buy_phase():
    outcome = evaluate_sell_phase(
        [_lifecycle("a", OrderStatus.FILLED), _lifecycle("b", OrderStatus.PARTIALLY_FILLED)]
    )

    assert outcome.complete is False
    assert [result.order_id for result in outcome.unfilled] == ["b"]
    assert "partially_filled" in outcome.reason


def test_rejected_sell_blocks_the_buy_phase():
    outcome = evaluate_sell_phase([_lifecycle("a", OrderStatus.REJECTED)])

    assert outcome.complete is False
    assert "rejected" in outcome.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_cohort.py -q`
Expected: FAIL with `ImportError: cannot import name 'evaluate_sell_phase'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/maestro/execution/rotation_cohort.py` (and add the imports at the top):

```python
from maestro.core.enums import OrderStatus
from maestro.execution.live_order_models import LiveOrderLifecycleResult


@dataclass(frozen=True)
class SellPhaseOutcome:
    complete: bool
    reason: str | None
    unfilled: tuple[LiveOrderLifecycleResult, ...]


def evaluate_sell_phase(results: list[LiveOrderLifecycleResult]) -> SellPhaseOutcome:
    """Decide whether the buy phase may run.

    Only a completely filled sell releases the cash the buy was sized against, so
    anything short of FILLED — partial, rejected, still working at the deadline —
    stops the cohort.
    """
    unfilled = tuple(result for result in results if result.final_status != OrderStatus.FILLED)
    if not unfilled:
        return SellPhaseOutcome(complete=True, reason=None, unfilled=())
    statuses = sorted({result.final_status.value for result in unfilled})
    return SellPhaseOutcome(
        complete=False,
        reason="sell_phase_incomplete:" + ",".join(statuses),
        unfilled=unfilled,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_cohort.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/maestro/execution/rotation_cohort.py tests/test_rotation_cohort.py
git commit -m "feat: gate the buy phase on complete sell fills"
```

---

### Task 3: Shrink approved buys to realized cash

**Files:**
- Modify: `src/maestro/execution/order_builder.py` (extract `floor_quantity_to_step`)
- Modify: `src/maestro/execution/rotation_cohort.py`
- Test: `tests/test_rotation_cohort.py`

**Interfaces:**
- Consumes: `TradableInstrument` from `maestro.core.instruments`.
- Produces: `floor_quantity_to_step(raw_quantity: float, instrument: TradableInstrument | None) -> float` in `order_builder`, and `rescale_buys_to_cash(buys: list[OrderIntent], available_cash: float, instruments: dict[str, TradableInstrument]) -> list[OrderIntent]` in `rotation_cohort`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rotation_cohort.py`:

```python
from maestro.core.enums import AssetType, BrokerProduct, MarketRegion
from maestro.core.instruments import TradableInstrument
from maestro.execution.rotation_cohort import rescale_buys_to_cash


def _instrument(symbol: str) -> TradableInstrument:
    return TradableInstrument(
        symbol=symbol,
        asset_type=AssetType.ETF,
        region=MarketRegion.US,
        currency=Currency.USD,
        broker="toss",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        broker_symbol=symbol,
        exchange_code="NASD",
        quantity_step=1,
        price_tick=0.01,
        min_order_quantity=1,
        min_order_notional=1,
    )


def _buy(order_id: str, symbol: str, quantity: float, price: float) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        price=price,
        notional=quantity * price,
        currency=Currency.USD,
        account_id="toss",
    )


def test_buys_keep_approved_size_when_cash_covers_them():
    buys = [_buy("b1", "TLT", 100, 100.0)]

    rescaled = rescale_buys_to_cash(buys, 10_000.0, {"TLT": _instrument("TLT")})

    assert [(order.symbol, order.quantity) for order in rescaled] == [("TLT", 100)]


def test_buys_never_grow_beyond_the_approved_quantity():
    # The operator approved 100 shares; extra cash must not buy 150.
    buys = [_buy("b1", "TLT", 100, 100.0)]

    rescaled = rescale_buys_to_cash(buys, 15_000.0, {"TLT": _instrument("TLT")})

    assert rescaled[0].quantity == 100


def test_short_cash_shrinks_buys_proportionally_and_floors_to_step():
    buys = [_buy("b1", "TLT", 100, 100.0), _buy("b2", "SCHD", 100, 100.0)]

    # $15,000 of $20,000 -> 0.75 each -> 75 whole shares each.
    rescaled = rescale_buys_to_cash(
        buys,
        15_000.0,
        {"TLT": _instrument("TLT"), "SCHD": _instrument("SCHD")},
    )

    assert {order.symbol: order.quantity for order in rescaled} == {"TLT": 75, "SCHD": 75}
    assert all(order.notional == order.quantity * order.price for order in rescaled)


def test_buy_that_shrinks_below_minimum_quantity_is_dropped():
    buys = [_buy("b1", "TLT", 1, 900.0)]

    rescaled = rescale_buys_to_cash(buys, 450.0, {"TLT": _instrument("TLT")})

    assert rescaled == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_cohort.py -q`
Expected: FAIL with `ImportError: cannot import name 'rescale_buys_to_cash'`

- [ ] **Step 3: Write minimal implementation**

In `src/maestro/execution/order_builder.py`, add a module-level function next to `round_price_to_tick` and make the method delegate:

```python
def floor_quantity_to_step(raw_quantity: float, instrument: TradableInstrument | None) -> float:
    """Round a quantity down to the instrument's tradable step."""
    if instrument is None:
        return raw_quantity
    steps = int(Decimal(str(raw_quantity)) / Decimal(str(instrument.quantity_step)))
    return float(Decimal(steps) * Decimal(str(instrument.quantity_step)))
```

Then replace the body of `OrderBuilder._order_quantity` with:

```python
    def _order_quantity(self, symbol: str, raw_quantity: float) -> float:
        return floor_quantity_to_step(raw_quantity, self.instruments.get(symbol))
```

Append to `src/maestro/execution/rotation_cohort.py`:

```python
from maestro.core.instruments import TradableInstrument
from maestro.execution.order_builder import floor_quantity_to_step


def rescale_buys_to_cash(
    buys: list[OrderIntent],
    available_cash: float,
    instruments: dict[str, TradableInstrument],
) -> list[OrderIntent]:
    """Fit approved buys inside the cash the sells actually raised.

    Quantities only ever shrink. The operator approved these sizes as a ceiling,
    so surplus cash must not buy more than was shown to them.
    """
    total_notional = sum(order.notional for order in buys)
    if total_notional <= 0:
        return []
    scale = min(1.0, max(0.0, available_cash) / total_notional)
    rescaled: list[OrderIntent] = []
    for order in buys:
        instrument = instruments.get(order.symbol)
        quantity = floor_quantity_to_step(order.quantity * scale, instrument)
        notional = quantity * order.price
        min_quantity = instrument.min_order_quantity if instrument else 0.0
        min_notional = instrument.min_order_notional if instrument else 0.0
        if quantity <= 0 or quantity < min_quantity or notional < min_notional:
            continue
        rescaled.append(order.model_copy(update={"quantity": quantity, "notional": notional}))
    return rescaled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_cohort.py tests/test_kis_multi_asset.py tests/test_buy_only_contribution.py -q`
Expected: all pass (the `_order_quantity` delegation must not change existing behavior)

- [ ] **Step 5: Commit**

```bash
git add src/maestro/execution/order_builder.py src/maestro/execution/rotation_cohort.py tests/test_rotation_cohort.py
git commit -m "feat: shrink approved buys to the cash the sells realized"
```

---

### Task 4: Keep sell-funded buys through the capacity partition

`OrderCapacityService.partition` drops any buy whose notional exceeds the broker's current cash. That is the layer that kills the rotation today. It must keep a buy that the sells in the same batch will fund, and let phase B do the real check.

**Files:**
- Modify: `src/maestro/execution/order_capacity.py`
- Test: `tests/test_order_capacity.py`

**Interfaces:**
- Produces: buys kept by `partition` carry `metadata["sell_fill_pending"] = True`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_order_capacity.py`:

```python
def test_buy_funded_by_a_sell_in_the_same_batch_is_kept():
    # Fully invested account: zero buying power until the sell fills.
    orders = [
        _order("sell_qqq", "QQQ", notional=10_000.0, side=OrderSide.SELL),
        _order("buy_tlt", "TLT", notional=10_000.0),
    ]
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(cash_buying_power=0.0, source="test")
    )

    accepted, blocked = service.partition(orders)

    assert blocked == []
    assert {order.order_id for order in accepted} == {"sell_qqq", "buy_tlt"}
    buy = next(order for order in accepted if order.order_id == "buy_tlt")
    assert buy.metadata["sell_fill_pending"] is True


def test_buy_beyond_cash_and_sell_proceeds_is_still_blocked():
    orders = [
        _order("sell_qqq", "QQQ", notional=1_000.0, side=OrderSide.SELL),
        _order("buy_tlt", "TLT", notional=10_000.0),
    ]
    service = OrderCapacityService(
        lambda order: BrokerBuyingPower(cash_buying_power=0.0, source="test")
    )

    accepted, blocked = service.partition(orders)

    assert [item.order.order_id for item in blocked] == ["buy_tlt"]
    assert blocked[0].reason == "cash_buying_power_exceeded"
```

Adjust the existing `_order` helper in that file to take a `side` keyword defaulting to `OrderSide.BUY`, mirroring the pattern already used in `tests/test_live_order_batch.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_capacity.py -q`
Expected: FAIL — `buy_tlt` appears in `blocked` with `cash_buying_power_exceeded`

- [ ] **Step 3: Write minimal implementation**

In `src/maestro/execution/order_capacity.py`, compute per-account sell proceeds up front and fold them into the available figure:

```python
    def partition(
        self,
        orders: list[OrderIntent],
    ) -> tuple[list[OrderIntent], list[OrderCapacityBlock]]:
        accepted: list[OrderIntent] = []
        blocked: list[OrderCapacityBlock] = []
        reserved_by_account: dict[str, float] = {}
        # A rotation's buy is funded by the sell filed alongside it. Blocking it
        # against pre-sell buying power leaves the book in cash for a whole cycle,
        # so keep it and let the post-sell phase run the authoritative check.
        proceeds_by_account: dict[str, float] = {}
        for order in orders:
            if order.side == OrderSide.SELL:
                key = order.account_id or "default"
                proceeds_by_account[key] = proceeds_by_account.get(key, 0.0) + order.notional
        for order in orders:
            if order.side != OrderSide.BUY:
                accepted.append(order)
                continue
            account_key = order.account_id or "default"
            reserved = reserved_by_account.get(account_key, 0.0)
            proceeds = proceeds_by_account.get(account_key, 0.0)
            ...
```

Then change the availability line and the accept branch:

```python
            available_cash = max(0.0, capacity.cash_buying_power + proceeds - reserved)
```

```python
            if proceeds > 0:
                order = order.model_copy(
                    update={"metadata": {**order.metadata, "sell_fill_pending": True}}
                )
            accepted.append(order)
            reserved_by_account[account_key] = reserved + order.notional
```

Leave `cash_quantity`/`available_quantity` computed from `available_cash` as they are.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_order_capacity.py tests/test_v07_production_hardening.py -q`
Expected: all pass. Telegram callers use `partition([order])` with a single order, so no sells are present and their behavior is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/maestro/execution/order_capacity.py tests/test_order_capacity.py
git commit -m "feat: keep sell-funded buys through the capacity partition"
```

---

### Task 5: Two-phase execution in the orchestrator

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py` — `_execute_live_approval_orders` (currently at `:2441`)
- Test: `tests/test_two_phase_rotation.py` (new)

**Interfaces:**
- Consumes: `split_rotation_cohorts`, `evaluate_sell_phase`, `rescale_buys_to_cash` from Tasks 1–3; `self._lookup_order_capacity` (`orchestrator.py:2101`); `LiveOrderBatchLifecycleService.run`.
- Produces: `_run_cohort_phases(...) -> list[LiveOrderLifecycleResult]` on the orchestrator.

- [ ] **Step 1: Extract batch-item building**

Pull the loop that builds `batch_items` (currently `orchestrator.py:2485-2537`) into a helper so it can be called once per phase. Signature:

```python
    def _build_batch_items(
        self,
        orders: list[OrderIntent],
        run_id: str,
        approval_id: str,
        *,
        signal_run_id: str | None,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> list[tuple[LiveOrderRequest, BatchOrderDependencies]]:
```

Move the body verbatim; it already populates `dependencies_by_account` as a cache, so passing it in lets both phases share the built dependencies.

- [ ] **Step 2: Run the suite to confirm the extraction changed nothing**

Run: `uv run pytest -q`
Expected: `1119 passed, 9 skipped`

- [ ] **Step 3: Commit the extraction**

```bash
git add src/maestro/orchestration/orchestrator.py
git commit -m "refactor: extract live order batch item building"
```

- [ ] **Step 4: Write the failing two-phase test**

Create `tests/test_two_phase_rotation.py` with two tests, built on the fixtures already used by `tests/test_live_approval_run_once.py` (reuse `_live_orchestrator` and `_save_broker_snapshot_with_quotes` from `tests/test_v07_production_hardening.py` by importing them, or copy the minimal fixture setup):

```python
def test_rotation_submits_buys_only_after_every_sell_filled(tmp_path, monkeypatch):
    """The buy must not reach the broker until the sell that funds it is done.

    Assert on submission ORDER, not just presence: a buy submitted alongside the
    sell is exactly the failure this plan exists to remove.
    """
```

The test drives `_execute_live_approval_orders` with one SELL and one BUY on a zero-cash account, using a fake safety service that records submissions in order and reports the sell FILLED. Assert the recorded order is `[sell, buy]` and that the buy was built only after the sell's terminal status.

```python
def test_partially_filled_sell_blocks_every_buy(tmp_path, monkeypatch):
```

Same setup but the fake reports the sell `PARTIALLY_FILLED`. Assert no buy was ever submitted.

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest tests/test_two_phase_rotation.py -q`
Expected: FAIL — both orders are submitted in one batch, so the second test sees a buy submission

- [ ] **Step 6: Implement the two-phase flow**

Replace the single `LiveOrderBatchLifecycleService(...).run(batch_items, approval_decision)` call with per-cohort phases:

```python
        lifecycle_results: list[LiveOrderLifecycleResult] = []
        for cohort in split_rotation_cohorts(armed_orders):
            lifecycle_results.extend(
                self._run_cohort_phases(
                    cohort,
                    run_id=run_id,
                    approval_id=approval_id,
                    approval_decision=approval_decision,
                    signal_run_id=signal_run_id,
                    dependencies_by_account=dependencies_by_account,
                )
            )
```

And add:

```python
    def _run_cohort_phases(
        self,
        cohort: RotationCohort,
        *,
        run_id: str,
        approval_id: str,
        approval_decision: ApprovalDecision,
        signal_run_id: str | None,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> list[LiveOrderLifecycleResult]:
        """Sell, wait for the fills, then buy against the cash they raised.

        The broker re-checks buying power at submission time against its own live
        balance, so the buy can only be sized once the sell has actually settled
        into cash. Sizing it any earlier is what left the book in cash for a cycle.
        """
        results: list[LiveOrderLifecycleResult] = []
        sell_results = self._run_batch_phase(
            list(cohort.sells), run_id, approval_id, approval_decision,
            signal_run_id=signal_run_id, dependencies_by_account=dependencies_by_account,
        )
        results.extend(sell_results)
        outcome = evaluate_sell_phase(sell_results)
        self._record_event(
            run_id,
            "rotation_cohort_phase",
            {
                "account_id": cohort.account_id,
                "currency": cohort.currency,
                "phase": "sell",
                "complete": outcome.complete,
                "reason": outcome.reason,
                "sell_order_ids": [order.order_id for order in cohort.sells],
                "buy_order_ids": [order.order_id for order in cohort.buys],
            },
        )
        if not outcome.complete:
            self._abort_cohort(cohort, outcome, run_id, approval_decision, dependencies_by_account)
            return results
        if not cohort.buys:
            return results
        buys = rescale_buys_to_cash(
            list(cohort.buys),
            self._cohort_available_cash(cohort),
            {instrument.symbol: instrument for instrument in self.config.universe.instruments},
        )
        results.extend(
            self._run_batch_phase(
                buys, run_id, approval_id, approval_decision,
                signal_run_id=signal_run_id, dependencies_by_account=dependencies_by_account,
            )
        )
        return results
```

`_run_batch_phase` builds items via `_build_batch_items` and runs one `LiveOrderBatchLifecycleService(...).run(...)`, returning `[item.lifecycle for item in batch.items]` and returning `[]` for an empty order list.

`_cohort_available_cash` re-queries the broker through the existing lookup and applies the fee buffer:

```python
    def _cohort_available_cash(self, cohort: RotationCohort) -> float:
        """Broker buying power after the sells settled, net of the fee buffer."""
        capacity = self._lookup_order_capacity(cohort.buys[0])
        buffer = 1.0 - self.config.execution.live_order_limits.fee_buffer_pct
        return max(0.0, capacity.cash_buying_power) * max(0.0, buffer)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_two_phase_rotation.py -q && uv run pytest -q`
Expected: new tests pass; full suite green

- [ ] **Step 8: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_two_phase_rotation.py
git commit -m "feat: run rotations as sell-then-buy cohort phases"
```

---

### Task 6: Abort cancels its own outstanding sells

Without this, an aborted rotation leaves a working sell at the broker, and `_pending_broker_order_issues` (`live_gates.py:591`) blocks the operator's entire next run until the DAY order expires at the close. Cancelling is what makes "stop and let the operator retry" actually retryable the same day.

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py`
- Test: `tests/test_two_phase_rotation.py`

**Interfaces:**
- Consumes: `LiveApprovalDependencies.cancel_service` (`live_order_factory.py:48`, already built), `LiveOrderCancelRequest(run_id, approval_id, broker_order, reason)`, `LiveOrderCancelResult.status`.
- Produces: `_abort_cohort(...) -> None` on the orchestrator.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_two_phase_rotation.py`:

```python
def test_abort_cancels_the_outstanding_sell(tmp_path, monkeypatch):
    """A partially filled sell must be cancelled so the operator can retry today."""
```

Drive a cohort whose sell comes back `PARTIALLY_FILLED`, with a fake cancel service recording its requests. Assert exactly one cancel was issued, carrying the sell's `broker_order_id`, and that a `rotation_cohort_aborted` event was recorded.

```python
def test_abort_skips_cancel_when_the_sell_never_reached_the_broker(tmp_path, monkeypatch):
```

Sell comes back `REJECTED` with `broker_order_id=None`. Assert no cancel was attempted — there is nothing at the broker to cancel.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_two_phase_rotation.py -q`
Expected: FAIL — no cancel is issued, `AttributeError` or an empty recorded-cancel list

- [ ] **Step 3: Implement the abort path**

```python
    def _abort_cohort(
        self,
        cohort: RotationCohort,
        outcome: SellPhaseOutcome,
        run_id: str,
        approval_decision: ApprovalDecision,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> None:
        """Stop the rotation and hand the account back in a retryable state.

        An order still working at the broker trips the pending-orders gate and
        blocks the operator's whole next run, so the remaining sell quantity has
        to come off the book before we hand back.
        """
        dependencies = dependencies_by_account.get(cohort.account_id)
        cancel_service = dependencies.cancel_service if dependencies else None
        canceled: list[dict[str, Any]] = []
        cancel_failures: list[dict[str, Any]] = []
        for result in outcome.unfilled:
            broker_order = result.submitted_order.broker_order if result.submitted_order else None
            if broker_order is None or cancel_service is None:
                continue
            try:
                cancel_result = cancel_service.cancel_order(
                    LiveOrderCancelRequest(
                        run_id=run_id,
                        approval_id=approval_decision.approval_id,
                        broker_order=broker_order,
                        reason="rotation_cohort_aborted",
                    ),
                    approval_decision,
                )
            except Exception as exc:
                cancel_failures.append(
                    {
                        "order_id": result.order_id,
                        "broker_order_id": broker_order.broker_order_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                continue
            canceled.append(
                {
                    "order_id": result.order_id,
                    "broker_order_id": broker_order.broker_order_id,
                    "status": cancel_result.status.value,
                    "canceled_quantity": cancel_result.canceled_quantity,
                }
            )
        self._record_event(
            run_id,
            "rotation_cohort_aborted",
            {
                "account_id": cohort.account_id,
                "currency": cohort.currency,
                "reason": outcome.reason,
                "unfilled_order_ids": [result.order_id for result in outcome.unfilled],
                "skipped_buy_order_ids": [order.order_id for order in cohort.buys],
                "canceled": canceled,
                "cancel_failures": cancel_failures,
            },
        )
```

`LiveOrderCancellationService._validate_cancellation_policy` already refuses to cancel anything outside OPEN/PARTIALLY_FILLED, refuses duplicates, and requires a passed broker reconciliation — so a cancel that raises is recorded rather than swallowed, and the operator sees it in `cancel_failures`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_two_phase_rotation.py -q && uv run pytest -q`
Expected: all green

- [ ] **Step 5: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_two_phase_rotation.py
git commit -m "feat: cancel outstanding sells when a rotation cohort aborts"
```

---

### Task 7: Restart safety and operator notification

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py`
- Test: `tests/test_two_phase_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
def test_rerunning_a_cohort_does_not_resubmit_a_filled_sell(tmp_path, monkeypatch):
    """Idempotency comes from duplicate_key; prove it holds across a re-run."""
```

Run `_execute_live_approval_orders` twice with the same `signal_run_id` and order ids. Assert the safety service saw the sell submitted once. `build_live_order_idempotency_key` feeds `LiveOrderRequest.duplicate_key`, and `LiveOrderSafetyService` checks `duplicate_key_exists` (`live_order_safety.py:336-338`) before submitting — this test pins that the two-phase flow does not bypass it.

```python
def test_abort_notifies_the_operator(tmp_path, monkeypatch):
```

Assert the notification client received a message naming the cohort and the unfilled sell.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_two_phase_rotation.py -q`
Expected: the notification test fails (no notification is sent on abort)

- [ ] **Step 3: Implement the notification**

In `_abort_cohort`, after `_record_event`, send through the same notification client the batch already uses, with a message naming: account, currency, which sells did not fill, which buys were skipped, and whether the cancels succeeded.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: full suite green, lint clean

- [ ] **Step 5: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_two_phase_rotation.py
git commit -m "feat: notify the operator when a rotation cohort aborts"
```

---

### Task 8: Documentation

**Files:**
- Modify: `docs/operator_runbook.md`, `docs/ROADMAP.md`, `docs/TASKS.md`

- [ ] **Step 1: Document the operator-facing behavior**

In `docs/operator_runbook.md`, add a "Rotation aborted" section: what the `rotation_cohort_aborted` notification means, that the outstanding sells were cancelled, that the account is now in a mixed or cash state, and that pressing `/rebalance` again regenerates the signal and sizes orders from current holdings — so it converges on the original target without any manual arithmetic.

- [ ] **Step 2: Update roadmap and task lists**

Record two-phase rotation execution as delivered in `docs/ROADMAP.md`, and note the follow-up plan for order-book-based limit pricing in `docs/TASKS.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: document two-phase rotation execution and abort recovery"
```

---

## Follow-up plan (not in scope here)

**Order-book-based limit pricing.** Toss exposes `/api/v1/orderbook` (`bids`/`asks` with price and volume), unused by the codebase today. Walking the book to the level where cumulative volume covers the order quantity — plus one or two levels of margin, capped by `max_quote_deviation_pct` — would raise the sell fill rate and reduce how often this plan's abort path fires. It is independently valuable and independently testable, so it gets its own plan. Ship this plan first and observe actual fill behavior before deciding the pricing policy.

## Self-Review

**Spec coverage:** Two-phase in-process cohort → Tasks 1, 2, 5. Cohort unit of account+currency → Task 1. Phase B re-query and downward-only rescaling → Tasks 3, 5. Approval-once contract → Task 3 (quantity ceiling) and Task 5 (same approval decision reused). Abort with cancel and confirmation → Task 6. Operator notification → Task 7. Capacity `sell_fill_pending` → Task 4. Restart idempotency → Task 7. Disposition of in-flight changes → unchanged by this plan; the sells-first sort in `live_order_batch.py` is now redundant but harmless and stays as a defensive default. Order-book pricing → deferred to the follow-up plan, as agreed.

**Placeholder scan:** No TBDs. Task 5 Step 4 and Task 6 Step 1 describe test setup in prose rather than full code because they depend on fixtures in `tests/test_v07_production_hardening.py` that the implementer must read first; the assertions and the failure each test catches are stated exactly.

**Type consistency:** `RotationCohort`, `SellPhaseOutcome`, `split_rotation_cohorts`, `evaluate_sell_phase`, `rescale_buys_to_cash`, and `floor_quantity_to_step` are named identically everywhere they appear. `_run_cohort_phases`, `_run_batch_phase`, `_build_batch_items`, `_cohort_available_cash`, and `_abort_cohort` are consistent across Tasks 5–7.
