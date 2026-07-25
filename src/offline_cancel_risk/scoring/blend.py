from typing import Any


def blend_scores(
    rule_scores: dict[str, float],
    ml_scores: dict[str, float | None] | None,
    policy: dict[str, Any],
) -> dict[str, float]:
    """Blend rule and ML scores per head using policy blend weights."""
    out: dict[str, float] = {}
    blend_cfg = policy.get("blend", {})
    for head, rule_v in rule_scores.items():
        weights = blend_cfg.get(head, {"rule_weight": 1.0, "ml_weight": 0.0})
        rw = float(weights.get("rule_weight", 1.0))
        mw = float(weights.get("ml_weight", 0.0))
        ml_v = None if ml_scores is None else ml_scores.get(head)
        if ml_v is None or mw <= 0.0:
            out[head] = float(rule_v)
            continue
        denom = rw + mw
        if denom <= 0:
            out[head] = float(rule_v)
            continue
        out[head] = float((rw * rule_v + mw * float(ml_v)) / denom)
    return out
