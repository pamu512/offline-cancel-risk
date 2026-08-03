from typing import Any


def theft_feature_score(
    ctx: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    food_categories = policy.get("food_categories", [])
    category = ctx.get("category")
    if category is not None and category in food_categories:
        score += 0.34
        reasons.append("food_category")

    high_value_amount = policy.get("high_value_amount", 0)
    order_value = ctx.get("order_value")
    if order_value is not None and order_value >= high_value_amount:
        score += 0.33
        reasons.append("high_value")

    if ctx.get("next_driver_no_order"):
        score += 0.33
        reasons.append("next_driver_no_order")

    # Pickup-then-cancel on food is classic selective theft / ghost complete pattern.
    if ctx.get("cancel_after_pickup") and category in food_categories:
        score += float(policy.get("cancel_after_pickup_bonus", 0.2))
        reasons.append("cancel_after_pickup")
        score = min(score, 1.0)

    if ctx.get("stage") == "near_dropoff" and ctx.get("next_driver_no_order"):
        score += float(policy.get("near_dropoff_no_order_bonus", 0.15))
        reasons.append("near_dropoff_no_order")
        score = min(score, 1.0)

    return min(score, 1.0), reasons
