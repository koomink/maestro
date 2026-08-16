import unicodedata

import pytest

from maestro.state.funding_workflow import (
    child_key,
    claim_key,
    completed_key,
    funding_workflow_id,
    head_key,
    superseded_key,
    workflow_id_from_request,
)


def _wid(**kwargs):
    base = {
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
    }
    base.update(kwargs)
    return funding_workflow_id(**base)


def test_workflow_id_keeps_the_whole_scope_not_a_hash():
    assert _wid() == 'funding:["core","acct-1","krw","KRW"]:2026-08'


def test_null_account_is_json_null_not_a_sentinel_string():
    assert _wid(account_id=None) != _wid(account_id="-")
    assert _wid(account_id=None) != _wid(account_id="null")


def test_separator_bearing_identifiers_do_not_collide():
    left = _wid(contribution_group_id='a","b', account_id=None)
    right = _wid(contribution_group_id="a", account_id="b")
    assert left != right


def test_unicode_equivalent_scopes_stay_distinct():
    # NFC "가" vs NFD "가" - normalizing here would let one supersede the other.
    composed = unicodedata.normalize("NFC", "가")
    decomposed = unicodedata.normalize("NFD", "가")
    assert composed != decomposed  # guard: the two forms really do differ
    assert _wid(contribution_group_id=composed) != _wid(contribution_group_id=decomposed)


def test_same_scope_in_a_different_month_is_a_different_workflow():
    assert _wid(month_key="2026-08") != _wid(month_key="2026-09")


def test_workflow_id_from_request_reads_the_scope_fields():
    request = {
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
    }
    assert workflow_id_from_request(request) == _wid()


def test_workflow_id_from_request_rejects_a_request_without_a_month():
    with pytest.raises(ValueError, match="month_key"):
        workflow_id_from_request({"account_id": "acct-1"})


def test_head_key_moves_with_the_version():
    assert head_key("funding:x:2026-08", 1) == "head:funding:x:2026-08:v1"
    assert head_key("funding:x:2026-08", 2) != head_key("funding:x:2026-08", 1)


def test_claim_key_carries_the_attempt():
    assert claim_key("wf", "funding", "req-1", 1) == "wf:funding:req-1:a1"
    assert claim_key("wf", "funding", "req-1", 2) != claim_key("wf", "funding", "req-1", 1)


def test_child_completed_and_superseded_keys_are_namespaced():
    assert child_key("req-1", "funding") == "child:req-1:funding"
    assert completed_key("wf", "req-1", "funding") == "wf-completed:wf:req-1:funding"
    assert superseded_key("wf", "req-1") == "wf-superseded:wf:req-1"
