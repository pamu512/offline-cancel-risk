from offline_cancel_risk.control_plane.metrics import holdout_split


def test_holdout_split_deterministic_and_partitioned():
    feedback = [{"order_display_id": f"O{i}", "labels": {}} for i in range(40)]
    a_train, a_hold = holdout_split(feedback, holdout_fraction=0.3)
    b_train, b_hold = holdout_split(feedback, holdout_fraction=0.3)
    assert [x["order_display_id"] for x in a_train] == [
        x["order_display_id"] for x in b_train
    ]
    assert [x["order_display_id"] for x in a_hold] == [
        x["order_display_id"] for x in b_hold
    ]
    ids = {x["order_display_id"] for x in a_train} | {
        x["order_display_id"] for x in a_hold
    }
    assert ids == {f"O{i}" for i in range(40)}
    assert len(a_train) + len(a_hold) == 40
    assert len(a_hold) > 0
    assert len(a_train) > len(a_hold)
