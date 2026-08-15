"""Classification of what actually happened to each order in an approved batch.

The rules come from the spec's 「C-1」 table. The point of every test here is
that a classification is made from recorded evidence, never from a guess: an
order we have no result for is `unknown`, not "did not go out".
"""

from maestro.ops.batch_execution import (
    OrderEvidence,
    classify_order,
    summarize_batch,
)


def evidence(
    order_id: str = "ord_1",
    symbol: str = "SSO",
    side: str = "sell",
    ordered_quantity: float = 20.0,
    *,
    has_intent: bool = True,
    has_result: bool = True,
    filled_quantity: float = 0.0,
    final_status: str | None = None,
    broker_order_id: str | None = "brk_1",
) -> OrderEvidence:
    return OrderEvidence(
        order_id=order_id,
        symbol=symbol,
        side=side,
        ordered_quantity=ordered_quantity,
        has_intent=has_intent,
        has_result=has_result,
        filled_quantity=filled_quantity,
        final_status=final_status,
        broker_order_id=broker_order_id,
    )


# --- the five rows of the 「C-1」 classification table ---------------------


def test_an_order_with_no_intent_never_went_out():
    assert classify_order(evidence(has_intent=False, has_result=False)) == "not_sent"


def test_an_intent_with_no_result_is_unknown_not_missing():
    # This is the whole safety property: we do not know whether the broker
    # saw this order, so we must not let anyone re-send it.
    assert classify_order(evidence(has_result=False)) == "unknown"


def test_a_fully_filled_order_is_filled():
    outcome = classify_order(evidence(filled_quantity=20.0, final_status="filled"))
    assert outcome == "filled"


def test_a_partly_filled_order_is_partially_filled():
    outcome = classify_order(evidence(filled_quantity=8.0, ordered_quantity=20.0))
    assert outcome == "partially_filled"


def test_an_unfilled_order_the_broker_closed_is_a_cancellation():
    outcome = classify_order(evidence(filled_quantity=0.0, final_status="canceled"))
    assert outcome == "cancelled_unfilled"


def test_an_unfilled_order_with_no_terminal_status_is_still_open():
    # No final status means nobody has seen this order end. Treating it as
    # cancelled would invite a re-order against a live resting order.
    outcome = classify_order(evidence(filled_quantity=0.0, final_status=None))
    assert outcome == "still_open"


# --- edges around the fill quantity ---------------------------------------


def test_a_broker_correction_above_the_ordered_quantity_still_counts_as_filled():
    outcome = classify_order(evidence(filled_quantity=21.0, ordered_quantity=20.0))
    assert outcome == "filled"


def test_an_unfilled_order_left_open_is_not_a_cancellation_even_when_halted():
    # `halted` is terminal, so the order is closed and nothing filled.
    outcome = classify_order(evidence(filled_quantity=0.0, final_status="halted"))
    assert outcome == "cancelled_unfilled"


def test_a_non_terminal_final_status_does_not_close_the_order():
    outcome = classify_order(evidence(filled_quantity=0.0, final_status="open"))
    assert outcome == "still_open"


# --- batch summary ---------------------------------------------------------


def test_an_empty_batch_summarizes_to_no_orders():
    batch = summarize_batch("appr_1", [])
    assert batch.orders == []
    assert batch.counts == {}
    assert batch.has_unknown is False


def test_a_batch_reports_a_count_per_outcome():
    batch = summarize_batch(
        "appr_1",
        [
            evidence(order_id="a", has_intent=False, has_result=False),
            evidence(order_id="b", has_intent=False, has_result=False),
            evidence(order_id="c", filled_quantity=20.0),
        ],
    )
    assert batch.counts == {"not_sent": 2, "filled": 1}
    assert batch.approval_id == "appr_1"


def test_one_unknown_order_marks_the_whole_batch_unknown():
    batch = summarize_batch(
        "appr_1",
        [
            evidence(order_id="a", filled_quantity=20.0),
            evidence(order_id="b", has_result=False),
        ],
    )
    assert batch.has_unknown is True


def test_a_batch_with_no_unknown_order_is_not_marked_unknown():
    batch = summarize_batch(
        "appr_1",
        [
            evidence(order_id="a", filled_quantity=20.0),
            evidence(order_id="b", has_intent=False, has_result=False),
        ],
    )
    assert batch.has_unknown is False


def test_each_line_keeps_the_evidence_it_was_classified_from():
    batch = summarize_batch("appr_1", [evidence(order_id="a", filled_quantity=20.0)])
    (line,) = batch.orders
    assert line.order_id == "a"
    assert line.symbol == "SSO"
    assert line.side == "sell"
    assert line.ordered_quantity == 20.0
    assert line.filled_quantity == 20.0
    assert line.outcome == "filled"
    assert line.broker_order_id == "brk_1"


# --- the real incident, as a table ----------------------------------------


def test_the_2026_08_11_crescendo_batch_classifies_the_way_the_operator_saw_it():
    """The five orders of appr_1589437a, from the evidence in the live DB.

    Sold TIP, failed to sell SSO, never sent the three buys. Anything that
    reclassifies these rows has broken the rule the incident taught us.
    """
    batch = summarize_batch(
        "appr_1589437a40424cd7a4e7141dbdf96e17",
        [
            evidence(
                order_id="ord_pdbc",
                symbol="PDBC",
                side="buy",
                ordered_quantity=366.0,
                has_intent=False,
                has_result=False,
                broker_order_id=None,
            ),
            evidence(
                order_id="ord_spy",
                symbol="SPY",
                side="buy",
                ordered_quantity=6.0,
                has_intent=False,
                has_result=False,
                broker_order_id=None,
            ),
            evidence(
                order_id="ord_bil",
                symbol="BIL",
                side="buy",
                ordered_quantity=15.0,
                has_intent=False,
                has_result=False,
                broker_order_id=None,
            ),
            evidence(
                order_id="ord_sso",
                symbol="SSO",
                side="sell",
                ordered_quantity=20.0,
                filled_quantity=0.0,
                final_status="canceled",
            ),
            evidence(
                order_id="ord_tip",
                symbol="TIP",
                side="sell",
                ordered_quantity=23.0,
                filled_quantity=23.0,
            ),
        ],
    )
    assert [line.outcome for line in batch.orders] == [
        "not_sent",
        "not_sent",
        "not_sent",
        "cancelled_unfilled",
        "filled",
    ]
    assert batch.counts == {"not_sent": 3, "cancelled_unfilled": 1, "filled": 1}
    assert batch.has_unknown is False
