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
from offline_cancel_risk.baselines.gate import apply_baselines
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.dbscan_v5 import compute_stop_confidences
from offline_cancel_risk.features.anomaly import (
    EntityAnomalyStore,
    cohort_key,
    evaluate_entity_anomaly,
)
from offline_cancel_risk.features.chat_signals import (
    ChatSignalStore,
    evaluate_chat_signals,
    merge_chat_signals,
    normalize_chat_signals,
)
from offline_cancel_risk.features.device_graph import DeviceGraphStore
from offline_cancel_risk.features.device_integrity import evaluate_device_integrity
from offline_cancel_risk.features.device_store import DeviceIntegrityStore
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.features.evidence import build_evidence
from offline_cancel_risk.features.geo import haversine, parse_latlong
from offline_cancel_risk.features.gps_integrity import (
    analyze_gps_integrity,
    dampen_stop_confidence,
)
from offline_cancel_risk.features.lineage import build_lineage_id
from offline_cancel_risk.features.progress import analyze_progress
from offline_cancel_risk.features.replacement import (
    compute_route_similarity,
    evaluate_replacement,
)
from offline_cancel_risk.features.sequence import resolve_sequence_with_pickup_drop
from offline_cancel_risk.features.stages import cancel_after_pickup, resolve_cancel_stage
from offline_cancel_risk.features.theft import theft_feature_score
from offline_cancel_risk.feedback.sampler import safe_inline_sample
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.models.canary import CanaryController, in_canary_cohort
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.pipeline.idempotency import lookup_cached, make_idempotency_key
from offline_cancel_risk.pipeline.window import resolve_gps_window
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
    baselines: EntityBaselineStore | None = None,
    cancel_stats: EntityCancelStatsStore | None = None,
    devices: DeviceIntegrityStore | None = None,
    device_graph: DeviceGraphStore | None = None,
    chat_store: ChatSignalStore | None = None,
    anomalies: EntityAnomalyStore | None = None,
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
    sequence_score, sequence_reasons, pickup_drop_meta = (
        resolve_sequence_with_pickup_drop(
            window.points,
            stops,
            policy["sequence"],
            stages_policy=policy.get("stages") or {},
        )
    )

    integrity = analyze_gps_integrity(window.points, policy=policy)
    final_stop_confidence = dampen_stop_confidence(final_stop_confidence, integrity)

    prev_device_ewma = None
    if devices is not None and req.device_id:
        prev_row = devices.get(str(req.device_id))
        if prev_row is not None:
            prev_device_ewma = float(prev_row["ewma_risk"])
    device_eval = evaluate_device_integrity(
        req.device_risk,
        prev_ewma=prev_device_ewma,
        policy=policy,
    )
    if device_eval["gps_multiplier"] < 1.0:
        final_stop_confidence = max(
            0.0,
            min(
                1.0,
                float(final_stop_confidence) * float(device_eval["gps_multiplier"]),
            ),
        )
    if devices is not None and req.device_id and (
        req.device_risk or device_eval["instant_risk"] > 0 or prev_device_ewma is not None
    ):
        try:
            devices.upsert(
                device_id=str(req.device_id),
                ewma_risk=float(device_eval["ewma_risk"]),
                instant_risk=float(device_eval["instant_risk"]),
                flags=dict(device_eval.get("normalized") or {}),
                driver_id=int(req.driver_id),
                user_id=req.user_id,
            )
        except Exception:
            _LOG.exception(
                "Device integrity persist failed device=%s", req.device_id
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

    stage, stage_meta = resolve_cancel_stage(window.points, stops, policy=policy)
    stage_meta = {**pickup_drop_meta, **stage_meta}
    after_pickup = cancel_after_pickup(stage)
    if pickup_drop_meta.get("drop_before_pickup"):
        after_pickup = False
    pickup_target = stops[0] if stops else (0.0, 0.0)
    progress = (
        analyze_progress(
            window.points,
            pickup_target,
            assign_ts=req.assign_ts,
            policy=policy,
        )
        if stops
        else {
            "progress_ratio": 0.0,
            "no_progress": False,
            "wrong_direction": False,
            "reasons": [],
            "heading_available": False,
        }
    )

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

    stats_window = int(
        policy.get("abuse", {}).get("cancel_stats_window_minutes", 120)
    )
    cancel_rate = None
    driver_cancel_count = 0
    pair_cancel_count = 0
    marketplace_signals: list[str] = []
    marketplace_meta: dict[str, Any] = {}
    if cancel_stats is not None:
        try:
            accept_ts = req.accepted_at or req.assign_ts
            cancel_stats.record_assess_funnel(
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                order_display_id=req.order_display_id,
                accept_ts=accept_ts,
                cancel_ts=req.cancel_ts,
                cancel_with_cause=req.cancel_with_cause,
                cancel_reason_code=req.cancel_reason_code,
                extra_events=list(req.marketplace_events or []),
            )
            st = cancel_stats.stats(
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                as_of=req.cancel_ts,
                window_minutes=stats_window,
                exclude_order_id=req.order_display_id,
                abuse_policy=policy.get("abuse") or {},
            )
            driver_cancel_count = int(st["driver_cancel_count"])
            cancel_rate = float(st["driver_cancel_rate"])
            pair_cancel_count = int(st["pair_cancel_count"])
            marketplace_signals = list(st.get("signals") or [])
            marketplace_meta = {
                "accept_cancel_rate": st.get("accept_cancel_rate"),
                "completion_rate": st.get("completion_rate"),
                "with_cause_fraction": st.get("with_cause_fraction"),
                "accepts": st.get("accepts"),
                "cancels": st.get("cancels"),
                "completes": st.get("completes"),
            }
        except Exception:
            _LOG.exception(
                "Entity cancel stats failed for order=%s", req.order_display_id
            )

    graph_meta: dict[str, Any] = {}
    device_graph_signals: list[str] = []
    if device_graph is not None and req.device_id:
        try:
            # Assess device_id is the courier device; user↔device edges come from
            # POST /v1/device-graph/edges (avoids shared_device_pair on every trip).
            device_graph.observe(
                device_id=str(req.device_id),
                driver_id=int(req.driver_id),
                user_id=None,
                event_ts=req.cancel_ts,
            )
            graph_meta = device_graph.evaluate(
                device_id=str(req.device_id),
                driver_id=int(req.driver_id),
                user_id=int(req.user_id) if req.user_id is not None else None,
                as_of=req.cancel_ts,
                policy=policy,
            )
            device_graph_signals = list(graph_meta.get("signals") or [])
        except Exception:
            _LOG.exception(
                "Device graph failed for order=%s device=%s",
                req.order_display_id,
                req.device_id,
            )

    chat_cfg = policy.get("chat_signals") or {}
    stored_chat = None
    if chat_store is not None:
        try:
            stored_chat = chat_store.get(req.order_display_id)
        except Exception:
            _LOG.exception(
                "Chat signal lookup failed order=%s", req.order_display_id
            )
    merged_chat = merge_chat_signals(
        stored_chat.get("flags") if stored_chat else None,
        req.chat_signals,
    )
    driver_chat_n = 0
    if chat_store is not None:
        try:
            driver_chat_n = chat_store.driver_signal_count(
                int(req.driver_id),
                as_of=req.cancel_ts,
                window_minutes=int(chat_cfg.get("window_minutes", 10080)),
                min_risk=float(chat_cfg.get("risk_threshold", 0.55)),
            )
        except Exception:
            _LOG.exception(
                "Chat driver count failed driver=%s", req.driver_id
            )
    chat_eval = evaluate_chat_signals(
        merged_chat,
        driver_signal_count=driver_chat_n,
        no_progress=bool(progress.get("no_progress")),
        wrong_direction=bool(progress.get("wrong_direction")),
        policy=policy,
    )
    if chat_store is not None and (
        req.chat_signals
        or chat_eval["fires"]
        or any(
            merged_chat.get(k)
            for k in (
                "persuasion_suspected",
                "cash_offline_suggested",
                "rider_forced_cancel",
            )
        )
        or merged_chat.get("signal_score") is not None
    ):
        try:
            chat_store.upsert(
                order_display_id=req.order_display_id,
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                flags=normalize_chat_signals(merged_chat),
                risk=float(chat_eval["risk"]),
                event_ts=req.cancel_ts,
            )
        except Exception:
            _LOG.exception(
                "Chat signal persist failed order=%s", req.order_display_id
            )

    abuse_score, abuse_reasons = abuse_feature_score(
        {
            "order_still_active": _order_still_active(req.order_status),
            "cancel_event_count": len(req.reassign_cancel_events) + 1,
            "driver_chain_count": driver_chain_count,
            "cancel_near_destination": near_dest,
            "cancel_after_pickup": after_pickup,
            "no_progress": bool(progress.get("no_progress")),
            "wrong_direction": bool(progress.get("wrong_direction")),
            "driver_cancel_count": driver_cancel_count,
            "driver_cancel_rate": cancel_rate or 0.0,
            "pair_cancel_count": pair_cancel_count,
            "marketplace_signals": marketplace_signals,
            "device_eval": device_eval,
            "device_graph_signals": device_graph_signals,
            "chat_eval": chat_eval,
        },
        policy["abuse"],
    )

    anomaly_eval: dict[str, Any] = {
        "mode": "off",
        "fires": False,
        "signals": [],
        "details": [],
        "abuse_bonus": 0.0,
        "reasons": [],
    }
    if anomalies is not None:
        try:
            feat_vals: dict[str, float] = {"cancel_abuse": float(abuse_score)}
            if cancel_rate is not None:
                feat_vals["cancel_rate"] = float(cancel_rate)
            acr = marketplace_meta.get("accept_cancel_rate")
            if acr is not None and int(marketplace_meta.get("accepts") or 0) >= 1:
                feat_vals["accept_cancel_rate"] = float(acr)
            anomaly_eval = evaluate_entity_anomaly(
                store=anomalies,
                entity_key=f"driver:{int(req.driver_id)}",
                cohort=cohort_key(
                    city_code=req.city_code, region_code=req.region_code
                ),
                features=feat_vals,
                order_display_id=req.order_display_id,
                event_ts=req.cancel_ts,
                policy=policy,
            )
            if anomaly_eval.get("abuse_bonus"):
                abuse_score = min(
                    1.0, float(abuse_score) + float(anomaly_eval["abuse_bonus"])
                )
                for r in anomaly_eval.get("signals") or []:
                    if r not in abuse_reasons:
                        abuse_reasons.append(str(r))
            elif anomaly_eval.get("reasons"):
                for r in anomaly_eval["reasons"]:
                    if r not in abuse_reasons:
                        abuse_reasons.append(str(r))
        except Exception:
            _LOG.exception(
                "Entity anomaly failed for order=%s", req.order_display_id
            )

    theft_score, theft_reasons = theft_feature_score(
        {
            "category": req.category,
            "order_value": req.order_value,
            "next_driver_no_order": bool(req.next_driver_no_order),
            "cancel_after_pickup": after_pickup,
            "stage": stage,
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
    reasons = [
        *reasons,
        *sequence_reasons,
        *list(integrity.get("reasons") or []),
        *list(progress.get("reasons") or []),
        f"stage:{stage}",
    ]
    if gps_unavailable:
        reasons = [*reasons, "gps_unavailable"]
    if sparse:
        reasons = [*reasons, "gps_sparse"]
    if float(gps_window["max_gap_minutes"]) > float(gps_policy["max_gap_minutes"]):
        reasons = [*reasons, "gps_gaps"]

    evidence = build_evidence(
        stage=stage,
        stage_meta=stage_meta,
        progress=progress,
        integrity=integrity,
        cancel_rate=cancel_rate,
        pair_cancel_count=pair_cancel_count,
        driver_chain_count=driver_chain_count,
        abuse_reasons=abuse_reasons,
        theft_reasons=theft_reasons,
        final_stop_confidence=final_stop_confidence,
        marketplace=marketplace_meta or None,
        device_eval=device_eval if device_eval.get("fires") else None,
        device_graph=graph_meta if device_graph_signals else None,
        chat_eval=chat_eval if chat_eval.get("abuse_bonus") else None,
        anomaly_eval=anomaly_eval if anomaly_eval.get("fires") else None,
    )

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
    scores_raw = dict(scores)
    baseline_meta: dict[str, dict[str, object]] = {}
    if baselines is not None:
        try:
            scores, baseline_reasons, baseline_meta = apply_baselines(
                baselines,
                scores=scores,
                thresholds={
                    k: float(v) for k, v in (policy.get("thresholds") or {}).items()
                },
                policy=policy,
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                region_code=(req.region_code or ""),
                city_code=(req.city_code or ""),
            )
            reasons = [*reasons, *baseline_reasons]
        except Exception:
            _LOG.exception(
                "Entity baseline gate failed for order=%s", req.order_display_id
            )
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
        scores_raw=ThreeHeadScores(**scores_raw),
        baseline_meta=baseline_meta,
        cancel_stage=stage,
        evidence=evidence,
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
