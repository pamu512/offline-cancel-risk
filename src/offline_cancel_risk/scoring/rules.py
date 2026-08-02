from typing import Any


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_rule_scores(
    features: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, float], list[str]]:
    final_stop = float(features.get("final_stop_confidence", 0.0))
    sequence = float(features.get("sequence_score", 0.0))
    dwell = float(features.get("dwell_fraction", 0.0))
    has_replacement = bool(features.get("has_replacement", False))
    replacement_valid = bool(features.get("replacement_valid", False))
    abuse_score = float(features.get("abuse_score", 0.0))
    theft_score = float(features.get("theft_score", 0.0))

    seq_w = float(policy.get("sequence", {}).get("offline_weight", 1.0))
    seq_w = max(0.0, seq_w)
    denom = 2.0 + seq_w
    base = (final_stop + sequence * seq_w + dwell) / denom if denom else 0.0
    if not has_replacement:
        base = max(base, base * 0.5 + 0.35)
    if has_replacement and not replacement_valid:
        base = min(1.0, base + 0.15)
    cancelled_offline = _clip01(base)

    reasons: list[str] = []
    for key in ("abuse_reasons", "theft_reasons", "replacement_reasons"):
        reasons.extend(str(r) for r in features.get(key, []) or [])

    invalid_replacement = (
        "invalid_replacement" in reasons
        or (has_replacement and not replacement_valid)
    )
    cancel_abuse = abuse_score
    if invalid_replacement and abuse_score >= 0.5:
        cancel_abuse = abuse_score + 0.1
    cancel_abuse = _clip01(cancel_abuse)
    selective_theft = _clip01(theft_score)

    scores = {
        "cancelled_offline": cancelled_offline,
        "cancel_abuse": cancel_abuse,
        "selective_theft": selective_theft,
    }
    return scores, reasons
