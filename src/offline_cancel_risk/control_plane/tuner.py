"""Constrained threshold / blend / routing search with holdout F1."""

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
    min_f1_lift: float = 0.01
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
) -> dict[str, dict[str, Any]]:
    rows = compute_label_metrics(
        ctx.assessments,
        feedback,
        thresholds=thresholds,
        region_code=region,
        city_code=city,
        blend=blend,
    )
    return {r["head"]: r for r in rows}


def _passes_op(m: dict[str, Any], op: dict[str, Any]) -> bool:
    precision = float(m["precision"])
    recall = float(m["recall"])
    if precision < float(op["min_precision"]) or precision > float(op["max_precision"]):
        return False
    if recall < float(op["min_recall"]) or recall > float(op["max_recall"]):
        return False
    return True


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

    resolved = resolved_policy_for_market(
        ctx.base_policy, ctx.overlays, region_code=region, city_code=city or None
    )
    bounds = ctx.guardrails.get("bounds") or {}
    train_fb, holdout_fb = holdout_split(
        ctx.feedback, holdout_fraction=ctx.holdout_fraction
    )
    eval_fb = holdout_fb if holdout_fb else train_fb
    decisions: list[dict[str, Any]] = []

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
        ).get(head)
        current_hold = _metrics_for(
            ctx,
            eval_fb,
            thresholds=dict(resolved.get("thresholds") or {}),
            blend=None,
            region=region,
            city=city,
        ).get(head)
        support = 0 if current_train is None else int(current_train["support"])
        if current_train is None or support < ctx.min_labeled:
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                constraints={**op, "holdout": bool(holdout_fb)},
                decision="rejected",
                reason="insufficient_labels",
                metrics_before=current_train,
            )
            decisions.append(
                {
                    "head": head,
                    "action": "reject",
                    "decision": "rejected",
                    "reason": "insufficient_labels",
                    "support": support,
                }
            )
            continue

        blend_candidates: list[dict[str, Any] | None] = [None]
        if ctx.search_blend and head == "cancelled_offline":
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

        routing_candidates: list[dict[str, Any] | None] = [None]
        if ctx.search_routing and "routing.p1_attention_min" in bounds:
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
                ).get(head)
                if train_m is None or not _passes_op(train_m, op):
                    thr = round(thr + ctx.threshold_step, 4)
                    continue
                hold_m = _metrics_for(
                    ctx,
                    eval_fb,
                    thresholds=cand_thresholds,
                    blend=blend,
                    region=region,
                    city=city,
                ).get(head)
                if hold_m is None or not _passes_op(hold_m, op):
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
                if best is None or float(hold_m["f1"]) > float(
                    best["holdout_metrics"]["f1"]
                ) + 1e-12:
                    best = candidate
                elif abs(
                    float(hold_m["f1"]) - float(best["holdout_metrics"]["f1"])
                ) < 1e-12:
                    if op["regime"] in {"peak", "clawback_peak"}:
                        if float(hold_m["precision"]) > float(
                            best["holdout_metrics"]["precision"]
                        ):
                            best = candidate
                    elif float(hold_m["recall"]) > float(
                        best["holdout_metrics"]["recall"]
                    ):
                        best = candidate
                thr = round(thr + ctx.threshold_step, 4)

        # Routing does not change classification F1; attach a mid-band p1 when searching.
        if best is not None and ctx.search_routing:
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
                constraints={**op, "holdout": bool(holdout_fb)},
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

        cur_f1 = float((current_hold or current_train or {}).get("f1", 0.0))
        lift = float(best["holdout_metrics"]["f1"]) - cur_f1
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
                constraints={**op, "holdout": bool(holdout_fb)},
                decision="rejected",
                reason="holdout_f1_lift_below_min",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "suggest",
                    "decision": "rejected",
                    "reason": "holdout_f1_lift_below_min",
                    "suggested": overlay,
                    "f1_lift": lift,
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
                constraints=op,
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
                constraints=op,
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
            constraints={**op, "holdout": bool(holdout_fb)},
            decision="accepted",
            reason="holdout_f1_lift",
        )
        decisions.append(
            {
                "head": head,
                "action": "apply",
                "decision": "accepted",
                "reason": "holdout_f1_lift",
                "overlay": overlay,
                "f1_lift": lift,
                "holdout_metrics": best["holdout_metrics"],
                "train_metrics": best["train_metrics"],
            }
        )
    return decisions
