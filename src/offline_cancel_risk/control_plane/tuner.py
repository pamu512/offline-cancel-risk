"""Precision-constrained threshold search on pattern cohort S."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.metrics import (
    compute_label_metrics,
    holdout_split,
)
from offline_cancel_risk.control_plane.operating_point import (
    resolve_operating_point,
    supply_ratio_from_forecast,
)
from offline_cancel_risk.control_plane.patterns import learning_cfg
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.resolve import GuardrailError
from offline_cancel_risk.policy.service import resolved_policy_for_market, save_overlay

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


@dataclass
class TunerContext:
    base_policy: dict[str, Any]
    guardrails: dict[str, Any]
    overlays: PolicyOverlayStore
    audit: PolicyAuditLog
    forecast: SupplyForecastStore
    hardgates: EnforcementHardgateStore
    op_cfg: dict[str, Any]
    assessments: list[dict[str, Any]]
    feedback: list[dict[str, Any]]
    region_code: str
    city_code: str = ""
    min_labeled: int = 30
    cooldown_minutes: int = 60
    min_f1_lift: float = 0.01  # reused as min holdout Recall_S lift
    threshold_step: float = 0.05
    holdout_fraction: float = 0.3
    search_blend: bool = True
    search_routing: bool = True
    at_ts: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _projected_flags(
    assessments: list[dict[str, Any]],
    *,
    head: str,
    threshold: float,
    region_code: str,
    city_code: str,
    blend: dict[str, Any] | None = None,
) -> int:
    from offline_cancel_risk.control_plane.metrics import resolve_scores

    region = region_code.strip().upper()
    city = (city_code or "").strip().upper()
    n = 0
    for a in assessments:
        if region and (a.get("region_code") or "").strip().upper() != region:
            continue
        if city and (a.get("city_code") or "").strip().upper() != city:
            continue
        score = float(resolve_scores(a, blend=blend).get(head, 0.0))
        if score >= threshold:
            n += 1
    return n


def _within_hardgates(
    hardgates: EnforcementHardgateStore,
    *,
    region_code: str,
    city_code: str,
    projected: int,
) -> tuple[bool, str]:
    caps = hardgates.effective_caps(region_code, city_code)
    for window in ("hour", "day", "week"):
        row = caps.get(window)
        if row is None:
            continue
        if projected > int(row["max_enforcements"]):
            return False, f"breaches_{window}_cap"
        return True, ""
    return True, ""


def _in_cooldown(audit: PolicyAuditLog, ctx: TunerContext, head: str) -> bool:
    last = audit.last_apply_at(ctx.region_code, ctx.city_code, head=head)
    if last is None:
        return False
    return _parse_ts(last) > datetime.now(timezone.utc) - timedelta(
        minutes=int(ctx.cooldown_minutes)
    )


def _metrics_for(
    ctx: TunerContext,
    feedback: list[dict[str, Any]],
    *,
    thresholds: dict[str, float],
    blend: dict[str, Any] | None,
    region: str,
    city: str,
    pattern_only: bool,
) -> dict[str, dict[str, Any]]:
    rows = compute_label_metrics(
        ctx.assessments,
        feedback,
        thresholds=thresholds,
        region_code=region,
        city_code=city,
        blend=blend,
        pattern_policy=ctx.base_policy if pattern_only else None,
    )
    return {r["head"]: r for r in rows}


def _passes_pattern_gates(
    m: dict[str, Any],
    *,
    target_precision: float,
    min_pattern_recall: float,
) -> bool:
    if float(m["precision"]) + 1e-12 < target_precision:
        return False
    if float(m["recall"]) + 1e-12 < min_pattern_recall:
        return False
    return True


def _better_candidate(
    hold_m: dict[str, Any],
    best: dict[str, Any] | None,
) -> bool:
    """Maximize Recall_S; tie-break on higher Precision_S."""
    if best is None:
        return True
    best_m = best["holdout_metrics"]
    if float(hold_m["recall"]) > float(best_m["recall"]) + 1e-12:
        return True
    if abs(float(hold_m["recall"]) - float(best_m["recall"])) < 1e-12:
        if float(hold_m["precision"]) > float(best_m["precision"]) + 1e-12:
            return True
    return False


def run_tuner(ctx: TunerContext) -> list[dict[str, Any]]:
    region = ctx.region_code.strip().upper()
    city = (ctx.city_code or "").strip().upper()
    at_ts = ctx.at_ts or _utc_now_iso()
    forecast_row = ctx.forecast.active(region, city, at_ts)
    ratio = supply_ratio_from_forecast(forecast_row)
    op = resolve_operating_point(ctx.op_cfg, ratio)
    if ctx.hardgates.clawback_active(region, city):
        op = resolve_operating_point(ctx.op_cfg, float(ctx.op_cfg["peak"]["ratio"]))
        op["regime"] = "clawback_peak"

    learn = learning_cfg(ctx.base_policy)
    target_precision = float(learn["target_precision"])
    min_pattern_recall = float(learn["min_pattern_recall"])
    min_pattern_support = int(learn["min_pattern_support"])
    blend_bar = int(learn["blend_search_min_support"])

    resolved = resolved_policy_for_market(
        ctx.base_policy, ctx.overlays, region_code=region, city_code=city or None
    )
    bounds = ctx.guardrails.get("bounds") or {}
    train_fb, holdout_fb = holdout_split(
        ctx.feedback, holdout_fraction=ctx.holdout_fraction
    )
    eval_fb = holdout_fb if holdout_fb else train_fb
    decisions: list[dict[str, Any]] = []
    constraints = {
        **op,
        "holdout": bool(holdout_fb),
        "target_precision": target_precision,
        "min_pattern_recall": min_pattern_recall,
        "objective": "precision_constrained_recall_S",
    }

    for head in _HEADS:
        bound_key = f"thresholds.{head}"
        if bound_key not in bounds:
            continue
        lo = float(bounds[bound_key]["min"])
        hi = float(bounds[bound_key]["max"])
        current_thr = float((resolved.get("thresholds") or {}).get(head, 0.75))
        current_blend = dict(resolved.get("blend") or {})
        current_routing = dict(resolved.get("routing") or {})

        current_train = _metrics_for(
            ctx,
            train_fb,
            thresholds=dict(resolved.get("thresholds") or {}),
            blend=None,
            region=region,
            city=city,
            pattern_only=True,
        ).get(head)
        current_hold = _metrics_for(
            ctx,
            eval_fb,
            thresholds=dict(resolved.get("thresholds") or {}),
            blend=None,
            region=region,
            city=city,
            pattern_only=True,
        ).get(head)
        support = 0 if current_train is None else int(current_train["support"])
        if current_train is None or support < min_pattern_support:
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                constraints=constraints,
                decision="rejected",
                reason="insufficient_pattern_labels",
                metrics_before=current_train,
            )
            decisions.append(
                {
                    "head": head,
                    "action": "reject",
                    "decision": "rejected",
                    "reason": "insufficient_pattern_labels",
                    "support": support,
                }
            )
            continue

        allow_blend = (
            ctx.search_blend
            and head == "cancelled_offline"
            and support >= blend_bar
        )
        blend_candidates: list[dict[str, Any] | None] = [None]
        if allow_blend:
            rw_key = f"blend.{head}.rule_weight"
            mw_key = f"blend.{head}.ml_weight"
            if rw_key in bounds and mw_key in bounds:
                for rw in (0.4, 0.6, 0.8):
                    if float(bounds[rw_key]["min"]) <= rw <= float(bounds[rw_key]["max"]):
                        mw = round(1.0 - rw, 4)
                        if float(bounds[mw_key]["min"]) <= mw <= float(
                            bounds[mw_key]["max"]
                        ):
                            b = dict(current_blend)
                            b[head] = {"rule_weight": rw, "ml_weight": mw}
                            blend_candidates.append(b)

        allow_routing = ctx.search_routing and support >= blend_bar
        routing_candidates: list[dict[str, Any] | None] = [None]
        if allow_routing and "routing.p1_attention_min" in bounds:
            p1_lo = float(bounds["routing.p1_attention_min"]["min"])
            p1_hi = float(bounds["routing.p1_attention_min"]["max"])
            for p1 in (50.0, 150.0, 200.0, 400.0):
                if p1_lo <= p1 <= p1_hi:
                    r = dict(current_routing)
                    r["p1_attention_min"] = p1
                    routing_candidates.append(r)

        best: dict[str, Any] | None = None
        for blend in blend_candidates:
            thr = lo
            while thr <= hi + 1e-9:
                cand_thresholds = dict(resolved.get("thresholds") or {})
                cand_thresholds[head] = round(thr, 4)
                train_m = _metrics_for(
                    ctx,
                    train_fb,
                    thresholds=cand_thresholds,
                    blend=blend,
                    region=region,
                    city=city,
                    pattern_only=True,
                ).get(head)
                if train_m is None or not _passes_pattern_gates(
                    train_m,
                    target_precision=target_precision,
                    min_pattern_recall=min_pattern_recall,
                ):
                    thr = round(thr + ctx.threshold_step, 4)
                    continue
                hold_m = _metrics_for(
                    ctx,
                    eval_fb,
                    thresholds=cand_thresholds,
                    blend=blend,
                    region=region,
                    city=city,
                    pattern_only=True,
                ).get(head)
                if hold_m is None or not _passes_pattern_gates(
                    hold_m,
                    target_precision=target_precision,
                    min_pattern_recall=min_pattern_recall,
                ):
                    thr = round(thr + ctx.threshold_step, 4)
                    continue
                projected = _projected_flags(
                    ctx.assessments,
                    head=head,
                    threshold=thr,
                    region_code=region,
                    city_code=city,
                    blend=blend,
                )
                ok_cap, _ = _within_hardgates(
                    ctx.hardgates,
                    region_code=region,
                    city_code=city,
                    projected=projected,
                )
                if not ok_cap:
                    thr = round(thr + ctx.threshold_step, 4)
                    continue
                candidate = {
                    "threshold": thr,
                    "blend": blend,
                    "routing": None,
                    "train_metrics": train_m,
                    "holdout_metrics": hold_m,
                    "projected_flags": projected,
                }
                if _better_candidate(hold_m, best):
                    best = candidate
                thr = round(thr + ctx.threshold_step, 4)

        if best is not None and allow_routing:
            for routing in routing_candidates:
                if routing is not None:
                    best["routing"] = routing
                    break

        if best is None:
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                metrics_before=current_hold,
                constraints=constraints,
                decision="rejected",
                reason="no_candidate_in_gates",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "reject",
                    "decision": "rejected",
                    "reason": "no_candidate_in_gates",
                }
            )
            continue

        cur_recall = float((current_hold or current_train or {}).get("recall", 0.0))
        lift = float(best["holdout_metrics"]["recall"]) - cur_recall
        overlay: dict[str, Any] = {
            "thresholds": {head: float(best["threshold"])}
        }
        if best.get("blend") is not None:
            overlay["blend"] = {
                head: dict(best["blend"][head])  # type: ignore[index]
            }
        if best.get("routing") is not None:
            overlay["routing"] = {
                "p1_attention_min": float(best["routing"]["p1_attention_min"])
            }

        if lift < ctx.min_f1_lift:
            ctx.audit.append(
                actor="tuner",
                action="suggest",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                after=overlay,
                metrics_before=current_hold,
                metrics_after=best["holdout_metrics"],
                constraints=constraints,
                decision="rejected",
                reason="holdout_pattern_recall_lift_below_min",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "suggest",
                    "decision": "rejected",
                    "reason": "holdout_pattern_recall_lift_below_min",
                    "suggested": overlay,
                    "recall_lift": lift,
                }
            )
            continue

        if _in_cooldown(ctx.audit, ctx, head):
            ctx.audit.append(
                actor="tuner",
                action="suggest",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                after=overlay,
                metrics_before=current_hold,
                metrics_after=best["holdout_metrics"],
                constraints=constraints,
                decision="rejected",
                reason="cooldown",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "suggest",
                    "decision": "rejected",
                    "reason": "cooldown",
                    "suggested": overlay,
                }
            )
            continue

        try:
            save_overlay(
                ctx.overlays,
                ctx.guardrails,
                region_code=region,
                city_code=city,
                overlay=overlay,
            )
        except GuardrailError as exc:
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                after=overlay,
                constraints=constraints,
                decision="rejected",
                reason=f"guardrail:{exc}",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "reject",
                    "decision": "rejected",
                    "reason": str(exc),
                }
            )
            continue

        ctx.audit.append(
            actor="tuner",
            action="apply",
            region_code=region,
            city_code=city,
            before={"thresholds": {head: current_thr}},
            after=overlay,
            metrics_before=current_hold,
            metrics_after=best["holdout_metrics"],
            constraints=constraints,
            decision="accepted",
            reason="holdout_pattern_recall_lift",
        )
        decisions.append(
            {
                "head": head,
                "action": "apply",
                "decision": "accepted",
                "reason": "holdout_pattern_recall_lift",
                "overlay": overlay,
                "recall_lift": lift,
                "holdout_metrics": best["holdout_metrics"],
                "train_metrics": best["train_metrics"],
            }
        )
    return decisions
