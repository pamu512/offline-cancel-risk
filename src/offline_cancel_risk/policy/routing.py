"""Prioritized review routing from flags + attention_score."""

from __future__ import annotations

from typing import Any


def build_routing(
    *,
    flags: dict[str, int],
    attention_score: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    routing_cfg = policy.get("routing", {})
    p1_min = float(routing_cfg.get("p1_attention_min", 200))
    p2_min = float(routing_cfg.get("p2_attention_min", 50))
    prefer = list(
        routing_cfg.get(
            "prefer_flag_order",
            ["selective_theft", "cancelled_offline", "cancel_abuse"],
        )
    )

    any_flag = any(int(flags.get(h, 0)) == 1 for h in prefer)
    if not any_flag and attention_score < p2_min:
        priority = "skip"
    elif attention_score >= p1_min or (
        any_flag and attention_score >= p2_min
    ):
        priority = "P1"
    elif any_flag or attention_score >= p2_min:
        priority = "P2"
    else:
        priority = "P3"

    queue = "mixed"
    for head in prefer:
        if int(flags.get(head, 0)) == 1:
            queue = {
                "selective_theft": "theft",
                "cancelled_offline": "offline",
                "cancel_abuse": "abuse",
            }.get(head, "mixed")
            break
    if not any_flag:
        queue = "low_signal"

    return {
        "priority": priority,
        "queue": queue,
        "attention_score": float(attention_score),
        "route_reason": (
            f"flags={{{','.join(h for h in prefer if flags.get(h))}}};"
            f"attention={attention_score:.2f}"
        ),
    }
