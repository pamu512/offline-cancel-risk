"""Constrained threshold search + auto-apply market overlays."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.metrics import compute_label_metrics
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
    at_ts: str | None = None


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
) -> int:
    region = region_code.strip().upper()
    city = (city_code or "").strip().upper()
    n = 0
    for a in assessments:
        if region and (a.get("region_code") or "").strip().upper() != region:
            continue
        if city and (a.get("city_code") or "").strip().upper() != city:
            continue
        score = float((a.get("scores") or {}).get(head, 0.0))
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
    """Enforce the tightest configured volume cap.

    Projected flag count is a short-horizon cohort estimate — compare it to the
    most granular window present (hour → day → week), not all windows at once.
    """
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


def run_tuner(ctx: TunerContext) -> list[dict[str, Any]]:
    region = ctx.region_code.strip().upper()
    city = (ctx.city_code or "").strip().upper()
    at_ts = ctx.at_ts or _utc_now_iso()
    forecast_row = ctx.forecast.active(region, city, at_ts)
    ratio = supply_ratio_from_forecast(forecast_row)
    op = resolve_operating_point(ctx.op_cfg, ratio)
    if ctx.hardgates.clawback_active(region, city):
        # Bias toward peak band during clawback TTL
        op = resolve_operating_point(ctx.op_cfg, float(ctx.op_cfg["peak"]["ratio"]))
        op["regime"] = "clawback_peak"

    resolved = resolved_policy_for_market(
        ctx.base_policy, ctx.overlays, region_code=region, city_code=city or None
    )
    bounds = (ctx.guardrails.get("bounds") or {})
    decisions: list[dict[str, Any]] = []

    for head in _HEADS:
        bound_key = f"thresholds.{head}"
        if bound_key not in bounds:
            continue
        lo = float(bounds[bound_key]["min"])
        hi = float(bounds[bound_key]["max"])
        current_thr = float((resolved.get("thresholds") or {}).get(head, 0.75))
        current_metrics = {
            r["head"]: r
            for r in compute_label_metrics(
                ctx.assessments,
                ctx.feedback,
                thresholds=dict(resolved.get("thresholds") or {}),
                region_code=region,
                city_code=city,
            )
        }.get(head)
        if current_metrics is None or current_metrics["support"] < ctx.min_labeled:
            decision = {
                "head": head,
                "action": "reject",
                "decision": "rejected",
                "reason": "insufficient_labels",
                "support": 0 if current_metrics is None else current_metrics["support"],
            }
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                constraints=op,
                decision="rejected",
                reason="insufficient_labels",
                metrics_before=current_metrics,
            )
            decisions.append(decision)
            continue

        best: dict[str, Any] | None = None
        thr = lo
        while thr <= hi + 1e-9:
            cand_thresholds = dict(resolved.get("thresholds") or {})
            cand_thresholds[head] = round(thr, 4)
            metrics_list = compute_label_metrics(
                ctx.assessments,
                ctx.feedback,
                thresholds=cand_thresholds,
                region_code=region,
                city_code=city,
            )
            m = next(r for r in metrics_list if r["head"] == head)
            precision = float(m["precision"])
            recall = float(m["recall"])
            if precision < float(op["min_precision"]) or precision > float(
                op["max_precision"]
            ):
                thr = round(thr + ctx.threshold_step, 4)
                continue
            if recall < float(op["min_recall"]) or recall > float(op["max_recall"]):
                thr = round(thr + ctx.threshold_step, 4)
                continue
            projected = _projected_flags(
                ctx.assessments,
                head=head,
                threshold=thr,
                region_code=region,
                city_code=city,
            )
            ok_cap, cap_reason = _within_hardgates(
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
                "metrics": m,
                "projected_flags": projected,
            }
            if best is None:
                best = candidate
            else:
                better_f1 = float(m["f1"]) > float(best["metrics"]["f1"]) + 1e-12
                tie = abs(float(m["f1"]) - float(best["metrics"]["f1"])) < 1e-12
                if better_f1:
                    best = candidate
                elif tie:
                    if op["regime"] in {"peak", "clawback_peak"}:
                        if precision > float(best["metrics"]["precision"]):
                            best = candidate
                    else:
                        if recall > float(best["metrics"]["recall"]):
                            best = candidate
            thr = round(thr + ctx.threshold_step, 4)

        if best is None:
            ctx.audit.append(
                actor="tuner",
                action="reject",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                metrics_before=current_metrics,
                constraints=op,
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

        lift = float(best["metrics"]["f1"]) - float(current_metrics["f1"])
        if lift < ctx.min_f1_lift:
            # Still suggest for visibility
            ctx.audit.append(
                actor="tuner",
                action="suggest",
                region_code=region,
                city_code=city,
                before={"thresholds": {head: current_thr}},
                after={"thresholds": {head: best["threshold"]}},
                metrics_before=current_metrics,
                metrics_after=best["metrics"],
                constraints=op,
                decision="rejected",
                reason="f1_lift_below_min",
            )
            decisions.append(
                {
                    "head": head,
                    "action": "suggest",
                    "decision": "rejected",
                    "reason": "f1_lift_below_min",
                    "current_threshold": current_thr,
                    "suggested_threshold": best["threshold"],
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
                after={"thresholds": {head: best["threshold"]}},
                metrics_before=current_metrics,
                metrics_after=best["metrics"],
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
                    "suggested_threshold": best["threshold"],
                }
            )
            continue

        overlay = {"thresholds": {head: float(best["threshold"])}}
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
            metrics_before=current_metrics,
            metrics_after=best["metrics"],
            constraints=op,
            decision="accepted",
            reason="f1_lift",
        )
        decisions.append(
            {
                "head": head,
                "action": "apply",
                "decision": "accepted",
                "reason": "f1_lift",
                "threshold": best["threshold"],
                "f1_lift": lift,
                "metrics": best["metrics"],
            }
        )
    return decisions
