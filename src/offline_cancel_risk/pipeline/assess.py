from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.adapters.publishers import StreamPublisher, TablePublisher
from offline_cancel_risk.api.schemas import (
    AssessRequest,
    AssessmentResult,
    ExpectedRevenueAtRisk,
    ThreeHeadFlags,
    ThreeHeadMlScores,
    ThreeHeadScores,
)
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.dbscan_v5 import compute_stop_confidences
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.features.geo import haversine, parse_latlong
from offline_cancel_risk.features.lineage import build_lineage_id
from offline_cancel_risk.features.replacement import (
    compute_route_similarity,
    evaluate_replacement,
)
from offline_cancel_risk.features.sequence import sequence_match_score
from offline_cancel_risk.features.theft import theft_feature_score
from offline_cancel_risk.pipeline.idempotency import lookup_cached, make_idempotency_key
from offline_cancel_risk.pipeline.window import resolve_gps_window
from offline_cancel_risk.models.canary import CanaryController, in_canary_cohort
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.feedback.sampler import safe_inline_sample
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.routing import build_routing
from offline_cancel_risk.policy.service import resolved_policy_for_market
from offline_cancel_risk.scoring.blend import blend_scores
from offline_cancel_risk.scoring.ear import compute_ear
from offline_cancel_risk.scoring.policy import apply_thresholds, policy_hash
from offline_cancel_risk.scoring.rules import compute_rule_scores
from offline_cancel_risk.timeutil import parse_ts

_MODEL_VERSION = "none"
_DEFAULT_GENERATION = 1
_LOG = logging.getLogger(__name__)
_ML_FEATURE_KEYS = (
    "final_stop_confidence",
    "sequence_score",
    "dwell_fraction",
    "abuse_score",
    "theft_score",
)


def _order_still_active(status: str) -> bool:
    return status.strip().upper() not in {
        "CANCELLED",
        "CANCELED",
        "COMPLETED",
        "DELIVERED",
        "DONE",
    }


def _cancel_near_destination(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    near_dest_radius_m: float,
) -> bool:
    if not points or not stops:
        return False
    last = max(points, key=lambda p: parse_ts(p.ts))
    dest_lat, dest_lon = stops[-1]
    return haversine(last.lat, last.lon, dest_lat, dest_lon) <= near_dest_radius_m


def _replacement_delay_minutes(req: AssessRequest) -> float | None:
    if not req.replacement_placed_at:
        return None
    delta = parse_ts(req.replacement_placed_at) - parse_ts(req.cancel_ts)
    return delta.total_seconds() / 60.0


def _gps_sparse(window_meta: dict[str, Any], gps_policy: dict[str, Any]) -> bool:
    return (
        int(window_meta["point_count"]) < int(gps_policy["min_points"])
        or float(window_meta["max_gap_minutes"]) > float(gps_policy["max_gap_minutes"])
    )


async def assess_order(
    req: AssessRequest,
    gps_client: GpsClient,
    policy: dict[str, Any],
    *,
    stream: StreamPublisher,
    table: TablePublisher,
    model_version: str = _MODEL_VERSION,
    generation: int = _DEFAULT_GENERATION,
    registry: ModelRegistry | None = None,
    shadow_metrics: ShadowMetricsStore | None = None,
    canary: CanaryController | None = None,
    overlays: PolicyOverlayStore | None = None,
    tickets: LabelTicketStore | None = None,
    bias_hints: dict[str, str] | None = None,
    driver_chains: DriverChainStore | None = None,
) -> AssessmentResult:
    if overlays is not None:
        policy = resolved_policy_for_market(
            policy,
            overlays,
            region_code=req.region_code,
            city_code=req.city_code,
        )
    if req.force_reassess:
        next_gen = getattr(table, "next_generation", None)
        if callable(next_gen):
            generation = int(next_gen(req.order_display_id))
    phash = policy_hash(policy)
    # Resolve serving model id up front for idempotency key stability.
    champion_rec = registry.get_champion() if registry is not None else None
    serving_model_id = (
        champion_rec.model_id if champion_rec is not None else model_version
    )
    canary_state = canary.active() if canary is not None else None
    use_canary = False
    if (
        canary_state is not None
        and registry is not None
        and in_canary_cohort(req.order_display_id, canary_state.canary_pct)
    ):
        use_canary = True
        serving_model_id = canary_state.challenger_model_id
    key = make_idempotency_key(
        req.order_display_id, phash, serving_model_id, generation
    )
    cached = lookup_cached(table, key)
    if cached is not None:
        # Idempotent assess still participates in daily label quota.
        safe_inline_sample(tickets, cached, policy, bias_hints=bias_hints)
        return cached

    gps_policy = policy["gps"]
    assign_ts = parse_ts(req.assign_ts)
    cancel_ts = parse_ts(req.cancel_ts)
    gps_unavailable = False

    async def fetch(start: datetime, end: datetime) -> list[GpsPoint]:
        nonlocal gps_unavailable
        try:
            return await gps_client.fetch_track(req.driver_id, start, end)
        except Exception:
            # MVP: degrade to empty track; worker still returns an assessment.
            gps_unavailable = True
            _LOG.exception(
                "GPS fetch failed for order=%s driver=%s; continuing with empty points",
                req.order_display_id,
                req.driver_id,
            )
            return []

    window = await resolve_gps_window(
        anchor_start=assign_ts,
        anchor_end=cancel_ts,
        fetch=fetch,
        policy=gps_policy,
    )
    gps_window = {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "expanded": window.expanded,
        "point_count": window.point_count,
        "max_gap_minutes": window.max_gap_minutes,
    }
    sparse = _gps_sparse(gps_window, gps_policy)

    stops = parse_latlong(req.latlong)
    dbscan = compute_stop_confidences(window.points, stops, policy["dbscan"])
    confidence_list: list[float] = list(dbscan["confidence_list"])
    final_stop_confidence = float(dbscan["final_confidence"])

    dwell_policy = {
        **policy["dwell"],
        "radius_m": float(policy["sequence"]["stop_match_radius_m"]),
    }
    dwell_masks = [
        dwell_stop_mask(window.points, stop, dwell_policy) for stop in stops
    ]
    dwell_fraction = (
        sum(1 for m in dwell_masks if m) / len(stops) if stops else 0.0
    )
    sequence_score = sequence_match_score(
        window.points, stops, policy["sequence"]
    )

    conf_threshold = float(policy["dbscan"]["confidence_threshold"])
    last_conf = confidence_list[-1] if confidence_list else 0.0
    last_dwell = dwell_masks[-1] if dwell_masks else False
    original_reached_destination = last_conf >= conf_threshold or last_dwell

    has_replacement = bool(req.replacement_order_id)
    route_similarity: float | None = None
    if req.replacement_latlong:
        route_similarity = compute_route_similarity(
            stops,
            parse_latlong(req.replacement_latlong),
            float(policy["sequence"]["stop_match_radius_m"]),
        )
    replacement = evaluate_replacement(
        original_reached_destination=original_reached_destination,
        replacement_placed_delay_minutes=_replacement_delay_minutes(req),
        route_similarity=route_similarity,
        has_replacement=has_replacement,
        policy=policy["replacement"],
    )
    lineage_id = build_lineage_id(req.order_display_id, req.reassign_cancel_events)

    near_dest = _cancel_near_destination(
        window.points,
        stops,
        float(policy["abuse"]["near_dest_radius_m"]),
    )
    driver_ids = {req.driver_id}
    for event in req.reassign_cancel_events:
        if "driver_id" in event:
            driver_ids.add(int(event["driver_id"]))
    chain_lookback = int(policy.get("abuse", {}).get("chain_lookback_minutes", 120))
    cross_order_chain = 0
    if driver_chains is not None:
        cross_order_chain = driver_chains.count_recent(
            req.driver_id,
            as_of=req.cancel_ts,
            window_minutes=chain_lookback,
            exclude_order_id=req.order_display_id,
        )
    driver_chain_count = max(len(driver_ids), cross_order_chain + 1)

    abuse_score, abuse_reasons = abuse_feature_score(
        {
            "order_still_active": _order_still_active(req.order_status),
            "cancel_event_count": len(req.reassign_cancel_events) + 1,
            "driver_chain_count": driver_chain_count,
            "cancel_near_destination": near_dest,
        },
        policy["abuse"],
    )
    theft_score, theft_reasons = theft_feature_score(
        {
            "category": req.category,
            "order_value": req.order_value,
            "next_driver_no_order": bool(req.next_driver_no_order),
        },
        policy["theft"],
    )

    features: dict[str, Any] = {
        "final_stop_confidence": final_stop_confidence,
        "sequence_score": sequence_score,
        "dwell_fraction": dwell_fraction,
        "has_replacement": has_replacement,
        "replacement_valid": replacement.valid,
        "abuse_score": abuse_score,
        "theft_score": theft_score,
        "abuse_reasons": abuse_reasons,
        "theft_reasons": theft_reasons,
        "replacement_reasons": list(replacement.reason_codes),
    }
    rule_scores, reasons = compute_rule_scores(features, policy)
    if gps_unavailable:
        reasons = [*reasons, "gps_unavailable"]
    if sparse:
        reasons = [*reasons, "gps_sparse"]
    if float(gps_window["max_gap_minutes"]) > float(gps_policy["max_gap_minutes"]):
        reasons = [*reasons, "gps_gaps"]

    ml_feature_vec = {
        "final_stop_confidence": float(final_stop_confidence),
        "sequence_score": float(sequence_score),
        "dwell_fraction": float(dwell_fraction),
        "abuse_score": float(abuse_score),
        "theft_score": float(theft_score),
    }
    assert set(_ML_FEATURE_KEYS) <= set(ml_feature_vec)

    ml_scores: dict[str, float | None] = {
        "cancelled_offline": None,
        "cancel_abuse": None,
        "selective_theft": None,
    }
    shadow_scores: dict[str, ThreeHeadScores] = {}
    model_roles: dict[str, str] = {}

    serve_model_id = serving_model_id
    predict_id: str | None = None
    if registry is not None:
        if use_canary and canary_state is not None:
            predict_id = canary_state.challenger_model_id
        elif champion_rec is not None:
            predict_id = champion_rec.model_id
    if registry is not None and predict_id is not None:
        try:
            ml_scores = {
                k: float(v)
                for k, v in registry.predict(predict_id, ml_feature_vec).items()
            }
            if use_canary:
                model_roles[predict_id] = "canary"
                if champion_rec is not None:
                    model_roles[champion_rec.model_id] = "champion"
            else:
                model_roles[predict_id] = "champion"
            serve_model_id = predict_id
        except Exception:
            _LOG.exception(
                "Serving model predict failed for %s; falling back to rules",
                predict_id,
            )
            reasons = [*reasons, "champion_predict_failed"]

    scores = blend_scores(rule_scores, ml_scores, policy)
    flags = apply_thresholds(scores, policy)

    if registry is not None:
        # Always compute champion blended scores for shadow comparison baseline
        champion_scores_for_metrics = scores
        if champion_rec is not None and (
            use_canary or serve_model_id != champion_rec.model_id
        ):
            try:
                champ_ml = registry.predict(champion_rec.model_id, ml_feature_vec)
                champion_scores_for_metrics = blend_scores(
                    rule_scores, champ_ml, policy
                )
            except Exception:
                champion_scores_for_metrics = scores

        for shadow in registry.list_shadow():
            try:
                shadow_ml = registry.predict(shadow.model_id, ml_feature_vec)
                shadow_blended = blend_scores(rule_scores, shadow_ml, policy)
                shadow_scores[shadow.model_id] = ThreeHeadScores(**shadow_blended)
                model_roles[shadow.model_id] = "shadow"
                if shadow_metrics is not None:
                    shadow_metrics.record(
                        order_display_id=req.order_display_id,
                        champion_model_id=(
                            champion_rec.model_id if champion_rec else "none"
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
                reasons = [*reasons, f"shadow_predict_failed:{shadow.model_id}"]

        # Also record canary challenger against champion when not in cohort
        if (
            canary_state is not None
            and not use_canary
            and shadow_metrics is not None
            and champion_rec is not None
        ):
            try:
                chal_ml = registry.predict(
                    canary_state.challenger_model_id, ml_feature_vec
                )
                chal_blended = blend_scores(rule_scores, chal_ml, policy)
                shadow_scores[canary_state.challenger_model_id] = ThreeHeadScores(
                    **chal_blended
                )
                model_roles.setdefault(
                    canary_state.challenger_model_id, "canary"
                )
                shadow_metrics.record(
                    order_display_id=req.order_display_id,
                    champion_model_id=champion_rec.model_id,
                    shadow_model_id=canary_state.challenger_model_id,
                    champion_scores=champion_scores_for_metrics,
                    shadow_scores=chal_blended,
                    order_value=float(req.order_value),
                )
            except Exception:
                _LOG.exception("Canary shadow record failed")

    ear, attention = compute_ear(scores, req.order_value, policy)
    ear_total = float(sum(ear.values()))

    assessed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = AssessmentResult(
        order_display_id=req.order_display_id,
        driver_id=req.driver_id,
        scores=ThreeHeadScores(**scores),
        flags=ThreeHeadFlags(**flags),
        expected_revenue_at_risk=ExpectedRevenueAtRisk(
            cancelled_offline=float(ear.get("cancelled_offline", 0.0)),
            cancel_abuse=float(ear.get("cancel_abuse", 0.0)),
            selective_theft=float(ear.get("selective_theft", 0.0)),
            total=ear_total,
        ),
        attention_score=float(attention),
        reasons=reasons,
        rule_scores=ThreeHeadScores(**rule_scores),
        ml_scores=ThreeHeadMlScores(**ml_scores),
        gps_window=gps_window,
        lineage_id=lineage_id,
        assessment_generation=generation,
        provisional=sparse or gps_unavailable,
        policy_hash=phash,
        model_version=serve_model_id,
        twin_version="none",
        graph_version="lineage-v0",
        feature_vector_ref=f"mem:{req.order_display_id}:{generation}",
        assessed_at=assessed_at,
        shadow_scores=shadow_scores,
        model_roles=model_roles,
        city_code=(req.city_code.strip().upper() if req.city_code else None),
        region_code=(
            req.region_code.strip().upper() if req.region_code else None
        ),
        routing=build_routing(
            flags=flags, attention_score=float(attention), policy=policy
        ),
    )

    # Dual-write: table first so idempotent cache survives stream failures.
    if req.force_reassess:
        mark = getattr(table, "mark_prior_provisional", None)
        if callable(mark):
            mark(req.order_display_id, before_generation=generation)
    table.upsert(result)
    if driver_chains is not None:
        try:
            driver_chains.record_from_assess(
                driver_id=req.driver_id,
                order_display_id=req.order_display_id,
                cancel_ts=req.cancel_ts,
                reassign_cancel_events=list(req.reassign_cancel_events),
            )
        except Exception:
            _LOG.exception(
                "Driver chain record failed for order=%s", req.order_display_id
            )
    try:
        stream.publish(result)
    except Exception:
        _LOG.exception(
            "Stream publish failed after table upsert for order=%s; "
            "idempotent cache still protects recomputation",
            req.order_display_id,
        )
    safe_inline_sample(tickets, result, policy, bias_hints=bias_hints)
    return result
