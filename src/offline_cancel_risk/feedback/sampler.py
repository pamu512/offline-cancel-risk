"""Hybrid inline + batch label ticket sampler (pattern-mass first)."""

from __future__ import annotations

import logging
from typing import Any

from offline_cancel_risk.api.schemas import AssessmentResult
from offline_cancel_risk.control_plane.patterns import learning_cfg, pattern_heads
from offline_cancel_risk.feedback.tickets import LabelTicketStore, utc_day_key

_LOG = logging.getLogger(__name__)
_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")

_PRIORITY = {
    "disagreement": 100,
    "pattern_mass": 90,
    "uncertainty": 80,
    "bias_fp": 70,
    "bias_fn": 70,
    "coverage": 10,
}


def _feedback_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    return dict(policy.get("feedback") or {})


def _thresholds(policy: dict[str, Any]) -> dict[str, float]:
    return {k: float(v) for k, v in (policy.get("thresholds") or {}).items()}


def score_band(score: float, thr: float, delta: float) -> str:
    if score < thr - delta:
        return "low"
    if score > thr + delta:
        return "high"
    return "mid"


def evaluate_inline_reason(
    *,
    scores: dict[str, float],
    rule_scores: dict[str, float],
    ml_scores: dict[str, float | None],
    policy: dict[str, Any],
    bias_hints: dict[str, str] | None = None,
    reasons: list[str] | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Return (reason, primary_head, strata) or None.

    One primary head per ticket so daily per-head quota math stays honest.
    Priority: disagreement → pattern_mass → uncertainty → bias.
    """
    cfg = _feedback_cfg(policy)
    delta = float(cfg.get("uncertainty_delta", 0.1))
    thrs = _thresholds(policy)
    bias_hints = bias_hints or {}

    disagree_heads: list[str] = []
    for head in _HEADS:
        ml = ml_scores.get(head)
        if ml is None:
            continue
        thr = float(thrs.get(head, 0.75))
        rule_flag = float(rule_scores.get(head, 0.0)) >= thr
        ml_flag = float(ml) >= thr
        if rule_flag != ml_flag:
            disagree_heads.append(head)
    if disagree_heads:
        primary = disagree_heads[0]
        return (
            "disagreement",
            primary,
            {"heads": disagree_heads, "primary": primary, "delta": delta},
        )

    assess_proxy = {"scores": scores, "reasons": list(reasons or [])}
    mass_heads = pattern_heads(assess_proxy, policy)
    if mass_heads:
        primary = mass_heads[0]
        return (
            "pattern_mass",
            primary,
            {"heads": mass_heads, "primary": primary, "band": "pattern"},
        )

    uncertain: list[tuple[float, str]] = []
    bands: dict[str, str] = {}
    for head in _HEADS:
        thr = float(thrs.get(head, 0.75))
        score = float(scores.get(head, 0.0))
        bands[head] = score_band(score, thr, delta)
        dist = abs(score - thr)
        if dist <= delta:
            uncertain.append((dist, head))
    if uncertain:
        uncertain.sort()  # closest to threshold first
        primary = uncertain[0][1]
        return (
            "uncertainty",
            primary,
            {
                "heads": [h for _, h in uncertain],
                "primary": primary,
                "bands": bands,
                "delta": delta,
            },
        )

    # Bias: only ticket orders that match the failure mode for that head.
    for head in _HEADS:
        hint = bias_hints.get(head)
        if hint not in {"bias_fp", "bias_fn"}:
            continue
        thr = float(thrs.get(head, 0.75))
        score = float(scores.get(head, 0.0))
        if hint == "bias_fp" and score >= thr:
            return (
                "bias_fp",
                head,
                {"heads": [head], "primary": head, "source": "label_metrics"},
            )
        if hint == "bias_fn" and score < thr:
            return (
                "bias_fn",
                head,
                {"heads": [head], "primary": head, "source": "label_metrics"},
            )

    return None


def bias_hints_from_metrics(
    metrics_rows: list[dict[str, Any]],
) -> dict[str, str]:
    """Map head → bias_fp|bias_fn from latest metrics snapshots."""
    by_head: dict[str, dict[str, Any]] = {}
    for row in metrics_rows:
        head = row.get("head")
        if head in _HEADS and head not in by_head:
            by_head[str(head)] = row
    hints: dict[str, str] = {}
    for head, row in by_head.items():
        support = int(row.get("support") or 0)
        if support < 5:
            continue
        precision = float(row.get("precision") or 0.0)
        recall = float(row.get("recall") or 0.0)
        # Prefer FP signal when both look bad — precision collapse is louder for ops.
        if precision < 0.7 and float(row.get("fp") or 0) > 0:
            hints[head] = "bias_fp"
        elif recall < 0.5 and float(row.get("fn") or 0) > 0:
            hints[head] = "bias_fn"
    return hints


def try_inline_sample(
    store: LabelTicketStore,
    result: AssessmentResult,
    policy: dict[str, Any],
    *,
    bias_hints: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    cfg = _feedback_cfg(policy)
    quota = int(cfg.get("daily_review_quota", 50))
    soft = float(cfg.get("inline_soft_cap_fraction", 0.6))
    day = utc_day_key()
    soft_cap = max(0, int(quota * soft))
    if store.day_count(day) >= soft_cap:
        return None
    if store.has_ticket_today(result.order_display_id, day):
        return None
    reason = evaluate_inline_reason(
        scores=result.scores.model_dump(),
        rule_scores=result.rule_scores.model_dump(),
        ml_scores=result.ml_scores.model_dump(),
        policy=policy,
        bias_hints=bias_hints,
        reasons=list(result.reasons or []),
    )
    if reason is None:
        return None
    sampling_reason, primary_head, strata = reason
    return store.create(
        order_display_id=result.order_display_id,
        region_code=result.region_code or "",
        city_code=result.city_code or "",
        heads=[primary_head],
        sampling_reason=sampling_reason,
        strata=strata,
        priority=_PRIORITY.get(sampling_reason, 10),
        day_key=day,
    )


def run_batch_sample(
    store: LabelTicketStore,
    assessments: list[AssessmentResult],
    policy: dict[str, Any],
    *,
    labeled_order_ids: set[str] | None = None,
    bias_hints: dict[str, str] | None = None,
    region_code: str = "",
    city_code: str = "",
) -> list[dict[str, Any]]:
    cfg = _feedback_cfg(policy)
    learn = learning_cfg(policy)
    quota = int(cfg.get("daily_review_quota", 50))
    per_min = int(cfg.get("per_head_min", 5))
    per_max = int(cfg.get("per_head_max", 25))
    mass_frac = float(learn.get("pattern_mass_fraction", 0.7))
    day = utc_day_key()
    labeled_order_ids = labeled_order_ids or set()
    bias_hints = bias_hints or {}
    region = (region_code or "").strip().upper()
    city = (city_code or "").strip().upper()

    created: list[dict[str, Any]] = []
    # Spec: daily_review_quota ±1
    remaining = (quota + 1) - store.day_count(day)
    if remaining <= 0:
        return created

    head_counts = store.head_counts(day)
    mass_target = max(0, int(round(remaining * mass_frac)))
    mass_left = mass_target

    def _eligible() -> list[AssessmentResult]:
        out: list[AssessmentResult] = []
        for a in assessments:
            if a.order_display_id in labeled_order_ids:
                continue
            if store.has_ticket_today(a.order_display_id, day):
                continue
            if region and (a.region_code or "").strip().upper() != region:
                continue
            if city and (a.city_code or "").strip().upper() != city:
                continue
            out.append(a)
        return out

    candidates = _eligible()

    def _try_create(
        a: AssessmentResult, reason: str, head: str, strata: dict[str, Any]
    ) -> bool:
        nonlocal remaining, mass_left
        if remaining <= 0:
            return False
        if head_counts.get(head, 0) >= per_max:
            return False
        ticket = store.create(
            order_display_id=a.order_display_id,
            region_code=a.region_code or "",
            city_code=a.city_code or "",
            heads=[head],
            sampling_reason=reason,
            strata=strata,
            priority=_PRIORITY.get(reason, 10),
            day_key=day,
        )
        if ticket is None:
            return False
        created.append(ticket)
        remaining -= 1
        head_counts[head] = head_counts.get(head, 0) + 1
        if reason == "pattern_mass":
            mass_left -= 1
        return True

    # 1) Pattern-mass fill (~70% of remaining quota).
    for a in list(candidates):
        if remaining <= 0 or mass_left <= 0:
            break
        heads = pattern_heads(a, policy)
        if not heads:
            continue
        heads.sort(key=lambda h: head_counts.get(h, 0))
        head = heads[0]
        if _try_create(
            a,
            "pattern_mass",
            head,
            {"heads": heads, "primary": head, "band": "pattern"},
        ):
            candidates.remove(a)

    # 2) Boundary / disagreement / bias for the rest of the mix.
    for a in list(candidates):
        if remaining <= 0:
            break
        reason = evaluate_inline_reason(
            scores=a.scores.model_dump(),
            rule_scores=a.rule_scores.model_dump(),
            ml_scores=a.ml_scores.model_dump(),
            policy=policy,
            bias_hints=bias_hints,
            reasons=list(a.reasons or []),
        )
        if reason is None:
            continue
        sampling_reason, primary, strata = reason
        if sampling_reason == "pattern_mass":
            # Already had a mass pass; skip re-creating as mass after budget.
            continue
        if _try_create(a, sampling_reason, primary, strata):
            candidates.remove(a)

    # 3) Per-head coverage floors.
    for head in _HEADS:
        while head_counts.get(head, 0) < per_min and remaining > 0:
            delta = float(cfg.get("uncertainty_delta", 0.1))
            mid: list[AssessmentResult] = []
            other: list[AssessmentResult] = []
            thr = float(_thresholds(policy).get(head, 0.75))
            for a in candidates:
                score = float(getattr(a.scores, head))
                if score_band(score, thr, delta) == "mid":
                    mid.append(a)
                else:
                    other.append(a)
            pool = mid or other
            if not pool:
                break
            picked = pool[0]
            if _try_create(
                picked,
                "coverage",
                head,
                {"heads": [head], "primary": head, "band": "coverage"},
            ):
                candidates.remove(picked)
            else:
                break

    for a in list(candidates):
        if remaining <= 0:
            break
        heads = [h for h in _HEADS if head_counts.get(h, 0) < per_max]
        if not heads:
            break
        heads.sort(key=lambda h: head_counts.get(h, 0))
        head = heads[0]
        if _try_create(
            a, "coverage", head, {"heads": [head], "primary": head, "band": "fill"}
        ):
            candidates.remove(a)

    return created


def safe_inline_sample(
    store: LabelTicketStore | None,
    result: AssessmentResult,
    policy: dict[str, Any],
    *,
    bias_hints: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if store is None:
        return None
    try:
        return try_inline_sample(store, result, policy, bias_hints=bias_hints)
    except Exception:
        _LOG.exception(
            "Inline label sampling failed for order=%s", result.order_display_id
        )
        return None
