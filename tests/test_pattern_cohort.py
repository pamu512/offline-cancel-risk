from offline_cancel_risk.control_plane.metrics import compute_label_metrics
from offline_cancel_risk.control_plane.patterns import (
    in_pattern_cohort,
    learning_cfg,
    pattern_heads,
)
from offline_cancel_risk.settings import load_policy


def test_learning_cfg_defaults_from_policy():
    policy = load_policy("config/policy.default.yaml")
    cfg = learning_cfg(policy)
    assert cfg["target_precision"] == 0.98
    assert cfg["pattern_mass_fraction"] == 0.7
    assert cfg["pattern_strata"]["cancelled_offline"]["score_min"] == 0.85


def test_in_pattern_cohort_score_and_reason():
    policy = {
        "learning": {
            "pattern_strata": {
                "cancelled_offline": {
                    "score_min": 0.85,
                    "reason_any": ["invalid_replacement"],
                },
                "cancel_abuse": {"score_min": 0.7},
                "selective_theft": {"score_min": 0.7},
            }
        }
    }
    assert in_pattern_cohort(
        {"scores": {"cancelled_offline": 0.9}, "reasons": []},
        "cancelled_offline",
        policy,
    )
    assert not in_pattern_cohort(
        {"scores": {"cancelled_offline": 0.5}, "reasons": []},
        "cancelled_offline",
        policy,
    )
    assert in_pattern_cohort(
        {
            "scores": {"cancelled_offline": 0.5},
            "reasons": ["invalid_replacement"],
        },
        "cancelled_offline",
        policy,
    )
    assert pattern_heads(
        {"scores": {"cancelled_offline": 0.9, "cancel_abuse": 0.8, "selective_theft": 0.1}},
        policy,
    ) == ["cancelled_offline", "cancel_abuse"]


def test_compute_label_metrics_pattern_cohort_filters():
    assessments = [
        {
            "order_display_id": "IN",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.9,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
        },
        {
            "order_display_id": "OUT",
            "region_code": "PH",
            "city_code": "MNL",
            "scores": {
                "cancelled_offline": 0.5,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
        },
    ]
    feedback = [
        {
            "order_display_id": "IN",
            "labels": {
                "cancelled_offline": 1,
                "cancel_abuse": 0,
                "selective_theft": 0,
            },
        },
        {
            "order_display_id": "OUT",
            "labels": {
                "cancelled_offline": 1,
                "cancel_abuse": 0,
                "selective_theft": 0,
            },
        },
    ]
    policy = load_policy("config/policy.default.yaml")
    thr = {
        "cancelled_offline": 0.75,
        "cancel_abuse": 0.75,
        "selective_theft": 0.75,
    }
    global_rows = compute_label_metrics(
        assessments, feedback, thresholds=thr, region_code="PH", city_code="MNL"
    )
    pattern_rows = compute_label_metrics(
        assessments,
        feedback,
        thresholds=thr,
        region_code="PH",
        city_code="MNL",
        pattern_policy=policy,
    )
    g = next(r for r in global_rows if r["head"] == "cancelled_offline")
    p = next(r for r in pattern_rows if r["head"] == "cancelled_offline")
    assert g["support"] == 2
    assert p["support"] == 1
    assert p["cohort"] == "pattern"
    assert p["precision"] == 1.0
    assert p["recall"] == 1.0
