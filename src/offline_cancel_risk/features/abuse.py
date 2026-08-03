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
    rate_thr = float(policy.get("cancel_rate_threshold", 2.0))  # cancels/hour (volume)
    if (
        int(ctx.get("driver_cancel_count") or 0) >= min_cancels
        and float(ctx.get("driver_cancel_rate") or 0.0) >= rate_thr
    ):
        score += float(policy.get("cancel_rate_bonus", 0.15))
        reasons.append("high_cancel_rate")

    for sig in ctx.get("marketplace_signals") or []:
        if sig == "high_accept_cancel_rate":
            score += float(policy.get("accept_cancel_rate_bonus", 0.25))
            reasons.append("high_accept_cancel_rate")
        elif sig == "low_completion_rate":
            score += float(policy.get("low_completion_rate_bonus", 0.2))
            reasons.append("low_completion_rate")
        elif sig == "cancel_without_cause_heavy":
            score += float(policy.get("without_cause_bonus", 0.2))
            reasons.append("cancel_without_cause_heavy")

    pair_min = int(policy.get("pair_cancel_min", 3))
    if int(ctx.get("pair_cancel_count") or 0) >= pair_min:
        score += float(policy.get("pair_density_bonus", 0.2))
        reasons.append("pair_cancel_density")

    # Device integrity: scaled bonus from evaluate_device_integrity (slice 2).
    device_eval = ctx.get("device_eval") or {}
    if device_eval.get("fires"):
        score += float(device_eval.get("abuse_bonus") or 0.0)
        for r in device_eval.get("reasons") or []:
            if r not in reasons:
                reasons.append(str(r))

    # Device graph: flat bonuses from evaluate signals (slice 3).
    for sig in ctx.get("device_graph_signals") or []:
        if sig == "multi_account_device":
            score += float(policy.get("multi_account_device_bonus", 0.25))
            reasons.append(sig)
        elif sig == "multi_user_device":
            score += float(policy.get("multi_user_device_bonus", 0.2))
            reasons.append(sig)
        elif sig == "device_hopping":
            score += float(policy.get("device_hopping_bonus", 0.2))
            reasons.append(sig)
        elif sig == "shared_device_pair":
            score += float(policy.get("shared_device_pair_bonus", 0.25))
            reasons.append(sig)

    # Chat / force-cancel: scaled + stall/repeat from evaluate_chat_signals (slice 4).
    chat_eval = ctx.get("chat_eval") or {}
    if chat_eval.get("abuse_bonus"):
        score += float(chat_eval.get("abuse_bonus") or 0.0)
        for r in chat_eval.get("reasons") or []:
            if r not in reasons:
                reasons.append(str(r))

    return min(score, 1.0), reasons
