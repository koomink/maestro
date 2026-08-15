import pytest

from maestro.orchestration.dispatch_group import dispatch_group_id

# NFC-equivalent, different codepoints: precomposed U+C548 versus the
# conjoining jamo sequence that normalizes to it. Written as escapes so the
# distinction survives editors and review tools that normalize on save.
COMPOSED_HANGUL = "안"
DECOMPOSED_HANGUL = "안"


def test_the_same_group_serializes_the_same_way_whatever_order_it_arrives_in():
    forward = dispatch_group_id("signal-1", ["tranquillo", "crescendo"])
    reversed_order = dispatch_group_id("signal-1", ["crescendo", "tranquillo"])
    assert forward == reversed_order


def test_a_separator_inside_an_identifier_does_not_merge_two_groups():
    # A plain join on ":" would render both of these as the same string.
    left = dispatch_group_id("signal-1", ["a:b", "c"])
    right = dispatch_group_id("signal-1", ["a", "b:c"])
    assert left != right


def test_a_separator_inside_the_signal_run_id_does_not_merge_two_groups():
    left = dispatch_group_id("signal-1:x", ["a"])
    right = dispatch_group_id("signal-1", ["x", "a"])
    assert left != right


def test_unicode_equivalent_but_distinct_codepoints_stay_distinct():
    # Normalizing here would let two genuinely different configured
    # identifiers share one envelope, and each would supersede the other.
    assert COMPOSED_HANGUL != DECOMPOSED_HANGUL
    composed = dispatch_group_id("signal-1", [COMPOSED_HANGUL])
    decomposed = dispatch_group_id("signal-1", [DECOMPOSED_HANGUL])
    assert composed != decomposed


def test_the_id_carries_the_whole_scope_not_a_hash():
    group_id = dispatch_group_id("signal-1", ["tranquillo"])
    assert group_id == 'dispatch-group:signal-1:["tranquillo"]'


def test_a_duplicate_strategy_id_does_not_change_the_group():
    assert dispatch_group_id("signal-1", ["a", "a", "b"]) == dispatch_group_id(
        "signal-1", ["a", "b"]
    )


@pytest.mark.parametrize("bad", [None, 3, ""])
def test_a_strategy_id_that_is_not_a_non_empty_string_is_refused(bad):
    # The caller already coerces to str and drops falsy values. Pin that
    # assumption instead of silently coercing here: a quiet coercion would
    # invent an id that no resume can match.
    with pytest.raises(ValueError):
        dispatch_group_id("signal-1", ["a", bad])


def test_an_empty_signal_run_id_is_refused():
    with pytest.raises(ValueError):
        dispatch_group_id("", ["a"])


def test_an_empty_group_is_refused():
    with pytest.raises(ValueError):
        dispatch_group_id("signal-1", [])
