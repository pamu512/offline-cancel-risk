from typing import Any


def abuse_feature_score(
    ctx: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if ctx.get("order_still_active"):
        score += 0.5
        reasons.append("order_still_active_after_driver_cancel")

    if ctx.get("cancel_event_count", 0) >= 3:
        score += 0.25
        reasons.append("multi_cancel_pattern")

    if ctx.get("driver_chain_count", 0) >= 3:
        score += 0.25
        reasons.append("driver_chain_pattern")

    if ctx.get("cancel_near_destination"):
        score += 0.25
        reasons.append("cancel_near_destination")

    return min(score, 1.0), reasons
