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

    if ctx.get("cancel_after_pickup"):
        score += float(policy.get("cancel_after_pickup_bonus", 0.2))
        reasons.append("cancel_after_pickup")

    if ctx.get("no_progress"):
        score += float(policy.get("no_progress_bonus", 0.2))
        reasons.append("no_progress_to_pickup")

    if ctx.get("wrong_direction"):
        score += float(policy.get("wrong_direction_bonus", 0.25))
        reasons.append("wrong_direction")

    min_cancels = int(policy.get("cancel_rate_min_count", 4))
    rate_thr = float(policy.get("cancel_rate_threshold", 2.0))  # cancels/hour
    if (
        int(ctx.get("driver_cancel_count") or 0) >= min_cancels
        and float(ctx.get("driver_cancel_rate") or 0.0) >= rate_thr
    ):
        score += float(policy.get("cancel_rate_bonus", 0.2))
        reasons.append("high_cancel_rate")

    pair_min = int(policy.get("pair_cancel_min", 3))
    if int(ctx.get("pair_cancel_count") or 0) >= pair_min:
        score += float(policy.get("pair_density_bonus", 0.2))
        reasons.append("pair_cancel_density")

    device = ctx.get("device_risk") or {}
    if device.get("spoof_suspected") or float(device.get("risk_score") or 0) >= float(
        policy.get("device_risk_score_threshold", 0.7)
    ):
        score += float(policy.get("device_risk_bonus", 0.15))
        reasons.append("device_risk")
    if device.get("rooted"):
        score += float(policy.get("device_rooted_bonus", 0.1))
        if "device_risk" not in reasons:
            reasons.append("device_rooted")

    return min(score, 1.0), reasons
