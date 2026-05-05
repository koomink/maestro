from maestro.core.ids import new_approval_id, new_order_id, new_run_id


def test_id_prefixes_are_semantic():
    assert new_run_id().startswith("run_")
    assert new_order_id().startswith("ord_")
    assert new_approval_id().startswith("appr_")


def test_generated_ids_are_unique_for_normal_use():
    ids = {new_run_id() for _ in range(100)}
    ids.update(new_order_id() for _ in range(100))
    ids.update(new_approval_id() for _ in range(100))

    assert len(ids) == 300
