"""Pattern-cohort membership for precision-first learning on common strata."""

from __future__ import annotations

from typing import Any

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")

_DEFAULT_STRATA: dict[str, dict[str, Any]] = {
    "cancelled_offline": {"score_min": 0.85},
    "cancel_abuse": {"score_min": 0.70},
    "selective_theft": {"score_min": 0.70},
}


def learning_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    raw = dict(policy.get("learning") or {})
    strata = dict(_DEFAULT_STRATA)
    for head, spec in (raw.get("pattern_strata") or {}).items():
        if head not in _HEADS or not isinstance(spec, dict):
            continue
        strata[head] = dict(spec)
    return {
        "target_precision": float(raw.get("target_precision", 0.98)),
        "min_pattern_support": int(raw.get("min_pattern_support", 15)),
        "min_pattern_recall": float(raw.get("min_pattern_recall", 0.35)),
        "pattern_mass_fraction": float(raw.get("pattern_mass_fraction", 0.7)),
        "blend_search_min_support": int(raw.get("blend_search_min_support", 40)),
        "pattern_strata": strata,
    }


def _score_for_head(assess: dict[str, Any] | Any, head: str) -> float:
    scores = getattr(assess, "scores", None)
    if scores is not None and not isinstance(scores, dict):
        return float(getattr(scores, head, 0.0))
    if isinstance(assess, dict):
        raw = assess.get("scores") or {}
        if hasattr(raw, head) and not isinstance(raw, dict):
            return float(getattr(raw, head))
        return float(raw.get(head, 0.0))
    return 0.0


def _reasons(assess: dict[str, Any] | Any) -> list[str]:
    reasons = getattr(assess, "reasons", None)
    if reasons is None and isinstance(assess, dict):
        reasons = assess.get("reasons") or []
    return [str(r) for r in (reasons or [])]


def in_pattern_cohort(
    assess: dict[str, Any] | Any,
    head: str,
    policy: dict[str, Any],
) -> bool:
    """True when assessment matches the configured pattern stratum for head."""
    cfg = learning_cfg(policy)
    stratum = (cfg.get("pattern_strata") or {}).get(head) or {}
    score_min = stratum.get("score_min")
    if score_min is not None and _score_for_head(assess, head) >= float(score_min):
        return True
    want = stratum.get("reason_any") or []
    if want:
        have = set(_reasons(assess))
        if any(str(r) in have for r in want):
            return True
    return False


def pattern_heads(
    assess: dict[str, Any] | Any,
    policy: dict[str, Any],
) -> list[str]:
    return [h for h in _HEADS if in_pattern_cohort(assess, h, policy)]
