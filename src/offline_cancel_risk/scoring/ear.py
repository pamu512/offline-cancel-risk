from typing import Any


def compute_ear(
    scores: dict[str, float],
    order_value: float,
    policy: dict[str, Any],
) -> tuple[dict[str, float], float]:
    ear_cfg = policy.get("ear") or {}
    recoverability = ear_cfg.get("recoverability") or {}
    attention_weights = ear_cfg.get("attention_weights") or {}

    ear: dict[str, float] = {}
    attention = 0.0
    for head, score in scores.items():
        rec = float(recoverability.get(head, 0.0))
        ear_value = float(order_value) * float(score) * rec
        ear[head] = ear_value
        attention += float(attention_weights.get(head, 0.0)) * ear_value
    return ear, attention
