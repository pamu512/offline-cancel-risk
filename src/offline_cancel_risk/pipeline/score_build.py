"""Rules / ML blend / baselines / EAR → AssessmentResult."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from offline_cancel_risk.api.schemas import (
    AssessmentResult,
    ExpectedRevenueAtRisk,
    ThreeHeadFlags,
    ThreeHeadMlScores,
    ThreeHeadScores,
)
from offline_cancel_risk.baselines.gate import apply_baselines
from offline_cancel_risk.features.evidence import build_evidence
from offline_cancel_risk.pipeline.context import AssessContext
from offline_cancel_risk.policy.routing import build_routing
from offline_cancel_risk.scoring.blend import blend_scores
from offline_cancel_risk.scoring.calibration import calibration_cfg, predict_calibrated
from offline_cancel_risk.scoring.ear import compute_ear, resolve_recoverability
from offline_cancel_risk.scoring.policy import apply_thresholds
from offline_cancel_risk.scoring.rules import compute_rule_scores

_LOG = logging.getLogger(__name__)
ML_FEATURE_KEYS = (
    "final_stop_confidence",
    "sequence_score",
    "dwell_fraction",
    "abuse_score",
    "theft_score",
)


def run_score_stage(ctx: AssessContext) -> AssessmentResult:
    req = ctx.req
    policy = ctx.policy
    gps_policy = policy["gps"]

    has_replacement = bool(req.replacement_order_id)
    ctx.features = {
        "final_stop_confidence": ctx.final_stop_confidence,
        "sequence_score": ctx.sequence_score,
        "dwell_fraction": ctx.dwell_fraction,
        "has_replacement": has_replacement,
        "replacement_valid": ctx.replacement.valid,
        "abuse_score": ctx.abuse_score,
        "theft_score": ctx.theft_score,
        "abuse_reasons": ctx.abuse_reasons,
        "theft_reasons": ctx.theft_reasons,
        "replacement_reasons": list(ctx.replacement.reason_codes),
    }
    ctx.rule_scores, reasons = compute_rule_scores(ctx.features, policy)
    reasons = [
        *reasons,
        *ctx.sequence_reasons,
        *list(ctx.integrity.get("reasons") or []),
        *list(ctx.progress.get("reasons") or []),
        f"stage:{ctx.stage}",
    ]
    if ctx.gps_unavailable:
        reasons = [*reasons, "gps_unavailable"]
    if ctx.sparse:
        reasons = [*reasons, "gps_sparse"]
    if float(ctx.gps_window["max_gap_minutes"]) > float(gps_policy["max_gap_minutes"]):
        reasons = [*reasons, "gps_gaps"]
    ctx.reasons = reasons

    presence_keys = (
        "dwell_target_s",
        "median_gap_s",
        "median_gap_raw_s",
        "min_pts_effective",
        "drop_off_min_pts_effective",
        "place_class",
        "vehicle_class",
        "place_factor",
        "vehicle_factor",
        "autoscaled",
    )
    stop_presence = {
        k: ctx.gps_window[f"presence_{k}"]
        for k in presence_keys
        if f"presence_{k}" in ctx.gps_window
    }
    ctx.evidence = build_evidence(
        stage=ctx.stage,
        stage_meta=ctx.stage_meta,
        progress=ctx.progress,
        integrity=ctx.integrity,
        cancel_rate=ctx.cancel_rate,
        pair_cancel_count=ctx.pair_cancel_count,
        driver_chain_count=ctx.driver_chain_count,
        abuse_reasons=ctx.abuse_reasons,
        theft_reasons=ctx.theft_reasons,
        final_stop_confidence=ctx.final_stop_confidence,
        marketplace=ctx.marketplace_meta or None,
        device_eval=ctx.device_eval if ctx.device_eval.get("fires") else None,
        device_graph=ctx.graph_meta if ctx.device_graph_signals else None,
        chat_eval=ctx.chat_eval if ctx.chat_eval.get("abuse_bonus") else None,
        anomaly_eval=ctx.anomaly_eval if ctx.anomaly_eval.get("fires") else None,
        stop_presence=stop_presence or None,
    )

    ctx.ml_feature_vec = {
        "final_stop_confidence": float(ctx.final_stop_confidence),
        "sequence_score": float(ctx.sequence_score),
        "dwell_fraction": float(ctx.dwell_fraction),
        "abuse_score": float(ctx.abuse_score),
        "theft_score": float(ctx.theft_score),
    }
    assert set(ML_FEATURE_KEYS) <= set(ctx.ml_feature_vec)

    ctx.ml_scores = {
        "cancelled_offline": None,
        "cancel_abuse": None,
        "selective_theft": None,
    }
    ctx.shadow_scores = {}
    ctx.model_roles = {}

    ctx.serve_model_id = ctx.serving_model_id
    predict_id: str | None = None
    if ctx.registry is not None:
        if ctx.use_canary and ctx.canary_state is not None:
            predict_id = ctx.canary_state.challenger_model_id
        elif ctx.champion_rec is not None:
            predict_id = ctx.champion_rec.model_id
    if ctx.registry is not None and predict_id is not None:
        try:
            ctx.ml_scores = {
                k: float(v)
                for k, v in ctx.registry.predict(predict_id, ctx.ml_feature_vec).items()
            }
            if ctx.use_canary:
                ctx.model_roles[predict_id] = "canary"
                if ctx.champion_rec is not None:
                    ctx.model_roles[ctx.champion_rec.model_id] = "champion"
            else:
                ctx.model_roles[predict_id] = "champion"
            ctx.serve_model_id = predict_id
        except Exception:
            _LOG.exception(
                "Serving model predict failed for %s; falling back to rules",
                predict_id,
            )
            ctx.reasons = [*ctx.reasons, "champion_predict_failed"]

    ctx.scores = blend_scores(ctx.rule_scores, ctx.ml_scores, policy)
    ctx.scores_raw = dict(ctx.scores)
    ctx.baseline_meta = {}
    if ctx.baselines is not None:
        try:
            ctx.scores, baseline_reasons, ctx.baseline_meta = apply_baselines(
                ctx.baselines,
                scores=ctx.scores,
                thresholds={
                    k: float(v) for k, v in (policy.get("thresholds") or {}).items()
                },
                policy=policy,
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                region_code=(req.region_code or ""),
                city_code=(req.city_code or ""),
            )
            ctx.reasons = [*ctx.reasons, *baseline_reasons]
        except Exception:
            _LOG.exception(
                "Entity baseline gate failed for order=%s", req.order_display_id
            )
    ctx.calibration_meta = {}
    cfg = calibration_cfg(policy)
    mode = cfg["mode"]
    if ctx.calibrators is not None and mode != "off":
        region = (req.region_code or "").strip().upper()
        city = (req.city_code or "").strip().upper()
        for head in ("cancelled_offline", "cancel_abuse", "selective_theft"):
            row = ctx.calibrators.get(region, city, head)
            if row is None:
                ctx.calibration_meta[head] = {
                    "applied": False,
                    "skip_reason": "missing",
                }
                continue
            p = predict_calibrated(
                {"method": row["method"], "params": row["params"]},
                float(ctx.scores_raw[head]),
            )
            discounted = abs(float(ctx.scores[head]) - float(ctx.scores_raw[head])) > 1e-12
            meta = {
                "p": p,
                "method": row["method"],
                "mode": mode,
                "ece": row.get("ece"),
                "support": row.get("support"),
                "applied": False,
                "baseline_discounted": discounted,
            }
            if mode == "apply":
                ctx.scores[head] = p
                meta["applied"] = True
            ctx.calibration_meta[head] = meta
    ctx.flags = apply_thresholds(ctx.scores, policy)

    if ctx.registry is not None:
        champion_scores_for_metrics = ctx.scores
        if ctx.champion_rec is not None and (
            ctx.use_canary or ctx.serve_model_id != ctx.champion_rec.model_id
        ):
            try:
                champ_ml = ctx.registry.predict(
                    ctx.champion_rec.model_id, ctx.ml_feature_vec
                )
                champion_scores_for_metrics = blend_scores(
                    ctx.rule_scores, champ_ml, policy
                )
            except Exception:
                champion_scores_for_metrics = ctx.scores

        for shadow in ctx.registry.list_shadow():
            try:
                shadow_ml = ctx.registry.predict(shadow.model_id, ctx.ml_feature_vec)
                shadow_blended = blend_scores(ctx.rule_scores, shadow_ml, policy)
                ctx.shadow_scores[shadow.model_id] = ThreeHeadScores(**shadow_blended)
                ctx.model_roles[shadow.model_id] = "shadow"
                if ctx.shadow_metrics is not None:
                    ctx.shadow_metrics.record(
                        order_display_id=req.order_display_id,
                        champion_model_id=(
                            ctx.champion_rec.model_id if ctx.champion_rec else "none"
                        ),
                        shadow_model_id=shadow.model_id,
                        champion_scores=champion_scores_for_metrics,
                        shadow_scores=shadow_blended,
                        order_value=float(req.order_value),
                    )
            except Exception:
                _LOG.exception(
                    "Shadow model predict failed for %s; skipping",
                    shadow.model_id,
                )
                ctx.reasons = [
                    *ctx.reasons,
                    f"shadow_predict_failed:{shadow.model_id}",
                ]

        if (
            ctx.canary_state is not None
            and not ctx.use_canary
            and ctx.shadow_metrics is not None
            and ctx.champion_rec is not None
        ):
            try:
                chal_ml = ctx.registry.predict(
                    ctx.canary_state.challenger_model_id, ctx.ml_feature_vec
                )
                chal_blended = blend_scores(ctx.rule_scores, chal_ml, policy)
                ctx.shadow_scores[ctx.canary_state.challenger_model_id] = (
                    ThreeHeadScores(**chal_blended)
                )
                ctx.model_roles.setdefault(
                    ctx.canary_state.challenger_model_id, "canary"
                )
                ctx.shadow_metrics.record(
                    order_display_id=req.order_display_id,
                    champion_model_id=ctx.champion_rec.model_id,
                    shadow_model_id=ctx.canary_state.challenger_model_id,
                    champion_scores=champion_scores_for_metrics,
                    shadow_scores=chal_blended,
                    order_value=float(req.order_value),
                )
            except Exception:
                _LOG.exception("Canary shadow record failed")

    learned = None
    if ctx.outcomes is not None:
        region = (req.region_code or "").strip().upper()
        city = (req.city_code or "").strip().upper()
        if region and city:
            learned = ctx.outcomes.get_recoverability(region, city)

    live_rec, meta = resolve_recoverability(policy, learned)
    ctx.ear, ctx.attention = compute_ear(
        ctx.scores, req.order_value, policy, recoverability=live_rec
    )
    ear_learned, attention_learned = compute_ear(
        ctx.scores,
        req.order_value,
        policy,
        recoverability=meta["recoverability_learned"],
    )
    meta["ear_learned"] = ear_learned
    meta["attention_learned"] = attention_learned
    ctx.ear_meta = meta
    ear_total = float(sum(ctx.ear.values()))

    # Downstream optional intel fill-rate (ops /v1/ops/downstream-fill).
    ctx.gps_window["downstream_device_risk"] = bool(req.device_risk)
    ctx.gps_window["downstream_chat_signals"] = bool(req.chat_signals)
    assessed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = AssessmentResult(
        order_display_id=req.order_display_id,
        driver_id=req.driver_id,
        scores=ThreeHeadScores(**ctx.scores),
        flags=ThreeHeadFlags(**ctx.flags),
        expected_revenue_at_risk=ExpectedRevenueAtRisk(
            cancelled_offline=float(ctx.ear.get("cancelled_offline", 0.0)),
            cancel_abuse=float(ctx.ear.get("cancel_abuse", 0.0)),
            selective_theft=float(ctx.ear.get("selective_theft", 0.0)),
            total=ear_total,
        ),
        attention_score=float(ctx.attention),
        reasons=ctx.reasons,
        rule_scores=ThreeHeadScores(**ctx.rule_scores),
        ml_scores=ThreeHeadMlScores(**ctx.ml_scores),
        gps_window=ctx.gps_window,
        lineage_id=ctx.lineage_id,
        assessment_generation=ctx.generation,
        provisional=ctx.sparse or ctx.gps_unavailable,
        policy_hash=ctx.phash,
        model_version=ctx.serve_model_id,
        twin_version="none",
        graph_version="lineage-v0",
        feature_vector_ref=f"mem:{req.order_display_id}:{ctx.generation}",
        assessed_at=assessed_at,
        shadow_scores=ctx.shadow_scores,
        model_roles=ctx.model_roles,
        city_code=(req.city_code.strip().upper() if req.city_code else None),
        region_code=(
            req.region_code.strip().upper() if req.region_code else None
        ),
        routing=build_routing(
            flags=ctx.flags, attention_score=float(ctx.attention), policy=policy
        ),
        scores_raw=ThreeHeadScores(**ctx.scores_raw),
        baseline_meta=ctx.baseline_meta,
        cancel_stage=ctx.stage,
        evidence=ctx.evidence,
        ear_meta=ctx.ear_meta,
        calibration_meta=ctx.calibration_meta,
    )
    ctx.result = result
    return result
