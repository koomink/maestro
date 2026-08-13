import pytest

from maestro.integrations.telegram.ui.approval_stage import (
    PROGRESS_RANK,
    approval_needs_attention,
    approval_progress,
    card_stage,
)
from maestro.integrations.telegram.ui.cards import render_daily_card


@pytest.mark.parametrize(
    "ack,completed,recovery,expected",
    [
        (None, None, False, "pending"),
        ({"status": "approved"}, None, False, "in_progress"),
        ({"status": "rejected"}, None, False, "done"),
        ({"status": "expired"}, None, False, "done"),
        # The 2026-08-12 shape: approved, but one order failed. Mapping on the
        # event type alone would render this half-executed rotation as done.
        (
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 1},
            False,
            "attention",
        ),
        (
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 0},
            False,
            "done",
        ),
        (
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 0},
            True,
            "attention",
        ),
    ],
)
def test_the_stage_reads_payloads_not_event_types(ack, completed, recovery, expected):
    progress = approval_progress(ack, completed)
    assert card_stage(progress, approval_needs_attention(ack, completed, recovery)) == expected


def test_a_resolved_recovery_releases_the_attention_flag():
    """The contradiction the first draft shipped: attention could never clear.

    Ranking attention above done and forbidding backward transitions meant a
    recovered incident stayed on the card forever, so a real new incident
    became indistinguishable from an old resolved one.
    """
    ack = {"status": "approved"}
    completed = {"approval_status": "approved", "orders_failed": 0}

    assert approval_needs_attention(ack, completed, unresolved_recovery=True) is True
    assert approval_needs_attention(ack, completed, unresolved_recovery=False) is False


def test_progress_never_walks_back():
    """Out-of-order arrival must not undo how far the run actually got."""
    assert PROGRESS_RANK["done"] > PROGRESS_RANK["in_progress"] > PROGRESS_RANK["pending"]


def test_a_failed_order_keeps_attention_even_after_recovery_clears():
    """orders_failed is a fact about the past; it does not resolve itself.

    Stage 2 has no way to close it out -- that is the exception wizard in
    stage 4 -- and letting it lapse would render a half-executed rotation as
    complete.
    """
    assert (
        approval_needs_attention(
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 1},
            unresolved_recovery=False,
        )
        is True
    )


def test_the_daily_card_lists_every_approval_group_with_its_stage():
    card = render_daily_card(
        "signal_1",
        [
            {"label": "트랑필로", "stage": "done"},
            {"label": "크레센도", "stage": "pending"},
        ],
    )

    assert "트랑필로" in card.text
    assert "크레센도" in card.text
    assert "✅" in card.text
    assert "⏳" in card.text


def test_the_daily_card_carries_no_action_buttons():
    """Only the approval card is actionable.

    Each group is approved against its own approval_id, so a button on the
    parent would have nothing unambiguous to bind to.
    """
    card = render_daily_card("signal_1", [{"label": "트랑필로", "stage": "pending"}])

    assert card.reply_markup is None


def test_a_mixed_outcome_is_rendered_without_collapsing_to_one_stage():
    card = render_daily_card(
        "signal_1",
        [
            {"label": "트랑필로", "stage": "done"},
            {"label": "크레센도", "stage": "attention"},
        ],
    )

    assert "✅" in card.text
    assert "⚠️" in card.text
