from typing import Any


def blend_scores(
    rule_scores: dict[str, float],
    ml_scores: dict[str, float | None] | None,
    policy: dict[str, Any],
) -> dict[str, float]:
    # ponytail: MVP has ml_weight 0 / ml all None — return rules as-is; blend when ML lands
    del policy
    if ml_scores is None or all(v is None for v in ml_scores.values()):
        return dict(rule_scores)
    return dict(rule_scores)
