from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.theft import theft_feature_score


def test_abuse_order_still_active():
    score, reasons = abuse_feature_score(
        {
            "order_still_active": True,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
        },
        {"multi_cancel_window_minutes": 120},
    )
    assert score >= 0.5
    assert "order_still_active_after_driver_cancel" in reasons


def test_abuse_ignores_next_driver_no_order():
    score_a, _ = abuse_feature_score(
        {
            "order_still_active": False,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
            "next_driver_no_order": True,
        },
        {"multi_cancel_window_minutes": 120},
    )
    score_b, _ = abuse_feature_score(
        {
            "order_still_active": False,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
            "next_driver_no_order": False,
        },
        {"multi_cancel_window_minutes": 120},
    )
    assert score_a == score_b


def test_theft_food_high_value_no_order():
    score, reasons = theft_feature_score(
        {"category": "FOOD", "order_value": 800, "next_driver_no_order": True},
        {"high_value_amount": 500, "food_categories": ["FOOD", "FOOD_DELIVERY"]},
    )
    assert score >= 0.75
    assert "food_category" in reasons
    assert "high_value" in reasons
    assert "next_driver_no_order" in reasons


def test_theft_independent_of_offline_inputs():
    score, _ = theft_feature_score(
        {"category": "HAUL", "order_value": 10, "next_driver_no_order": False},
        {"high_value_amount": 500, "food_categories": ["FOOD"]},
    )
    assert score == 0.0
