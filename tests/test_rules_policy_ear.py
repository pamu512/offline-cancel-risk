from pathlib import Path

from offline_cancel_risk.settings import load_policy
from offline_cancel_risk.scoring.rules import compute_rule_scores
from offline_cancel_risk.scoring.ear import compute_ear

full_policy = load_policy(Path("config/policy.default.yaml"))


def test_offline_boost_on_invalid_replacement():
    features = {
        "final_stop_confidence": 0.8,
        "sequence_score": 0.8,
        "dwell_fraction": 0.8,
        "replacement_valid": False,
        "has_replacement": True,
        "abuse_score": 0.2,
        "theft_score": 0.0,
        "abuse_reasons": [],
        "theft_reasons": [],
        "replacement_reasons": ["invalid_replacement"],
    }
    scores, reasons = compute_rule_scores(features, full_policy)
    assert "invalid_replacement" in reasons
    assert scores["cancelled_offline"] >= 0.75


def test_theft_high_offline_low_independence():
    features = {
        "final_stop_confidence": 0.0,
        "sequence_score": 0.0,
        "dwell_fraction": 0.0,
        "replacement_valid": False,
        "has_replacement": False,
        "abuse_score": 0.0,
        "theft_score": 0.9,
        "abuse_reasons": [],
        "theft_reasons": ["food_category", "next_driver_no_order"],
        "replacement_reasons": ["no_replacement"],
    }
    scores, _ = compute_rule_scores(features, full_policy)
    assert scores["selective_theft"] >= 0.75
    assert scores["cancelled_offline"] < 0.5


def test_attention_uses_ear_weights():
    scores = {"cancelled_offline": 1.0, "cancel_abuse": 0.0, "selective_theft": 0.0}
    ear, attention = compute_ear(scores, order_value=100.0, policy=full_policy)
    assert ear["cancelled_offline"] == 100.0
    assert attention > 0
