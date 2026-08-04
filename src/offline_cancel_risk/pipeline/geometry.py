"""GPS window + geometry / trip features stage."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.dbscan_v5 import compute_stop_confidences
from offline_cancel_risk.features.device_integrity import evaluate_device_integrity
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.features.dwell_scale import resolve_stop_presence
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
from offline_cancel_risk.pipeline.context import AssessContext
from offline_cancel_risk.pipeline.window import resolve_gps_window
from offline_cancel_risk.timeutil import parse_ts

_LOG = logging.getLogger(__name__)


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


def _replacement_delay_minutes(req: Any) -> float | None:
    if not req.replacement_placed_at:
        return None
    delta = parse_ts(req.replacement_placed_at) - parse_ts(req.cancel_ts)
    return delta.total_seconds() / 60.0


def _gps_sparse(window_meta: dict[str, Any], gps_policy: dict[str, Any]) -> bool:
    return (
        int(window_meta["point_count"]) < int(gps_policy["min_points"])
        or float(window_meta["max_gap_minutes"]) > float(gps_policy["max_gap_minutes"])
    )


async def run_geometry_stage(ctx: AssessContext) -> None:
    req = ctx.req
    policy = ctx.policy
    gps_policy = policy["gps"]
    assign_ts = parse_ts(req.assign_ts)
    cancel_ts = parse_ts(req.cancel_ts)

    async def fetch(start: datetime, end: datetime) -> list[GpsPoint]:
        try:
            return await ctx.gps_client.fetch_track(req.driver_id, start, end)
        except Exception:
            ctx.gps_unavailable = True
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
    ctx.window = window
    ctx.gps_window = {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "expanded": window.expanded,
        "point_count": window.point_count,
        "max_gap_minutes": window.max_gap_minutes,
    }
    ctx.sparse = _gps_sparse(ctx.gps_window, gps_policy)

    stops = parse_latlong(req.latlong)
    ctx.stops = stops
    presence = resolve_stop_presence(
        window.points,
        policy,
        place_class=getattr(req, "place_class", None),
        vehicle_class=getattr(req, "vehicle_class", None),
    )
    # Flatten into gps_window (AssessmentResult allows only scalar values).
    for key, val in presence.items():
        if val is None:
            continue
        ctx.gps_window[f"presence_{key}"] = val
    dbscan_policy = {
        **policy["dbscan"],
        "min_pts": int(presence["min_pts_effective"]),
        "drop_off_min_pts": int(presence["drop_off_min_pts_effective"]),
    }
    dbscan = compute_stop_confidences(window.points, stops, dbscan_policy)
    ctx.confidence_list = list(dbscan["confidence_list"])
    ctx.final_stop_confidence = float(dbscan["final_confidence"])

    dwell_cfg = policy["dwell"]
    dwell_radius = float(
        dwell_cfg.get("radius_m", policy["sequence"]["stop_match_radius_m"])
    )
    dwell_policy = {
        **dwell_cfg,
        "min_dwell_seconds": float(presence["dwell_target_s"]),
        "radius_m": dwell_radius,
    }
    ctx.dwell_masks = [
        dwell_stop_mask(window.points, stop, dwell_policy) for stop in stops
    ]
    ctx.dwell_fraction = (
        sum(1 for m in ctx.dwell_masks if m) / len(stops) if stops else 0.0
    )
    ctx.sequence_score, ctx.sequence_reasons, ctx.pickup_drop_meta = (
        resolve_sequence_with_pickup_drop(
            window.points,
            stops,
            policy["sequence"],
            stages_policy=policy.get("stages") or {},
        )
    )

    ctx.integrity = analyze_gps_integrity(window.points, policy=policy)
    ctx.final_stop_confidence = dampen_stop_confidence(
        ctx.final_stop_confidence, ctx.integrity
    )

    prev_device_ewma = None
    if ctx.devices is not None and req.device_id:
        prev_row = ctx.devices.get(str(req.device_id))
        if prev_row is not None:
            prev_device_ewma = float(prev_row["ewma_risk"])
    ctx.device_eval = evaluate_device_integrity(
        req.device_risk,
        prev_ewma=prev_device_ewma,
        policy=policy,
    )
    if ctx.device_eval["gps_multiplier"] < 1.0:
        ctx.final_stop_confidence = max(
            0.0,
            min(
                1.0,
                float(ctx.final_stop_confidence)
                * float(ctx.device_eval["gps_multiplier"]),
            ),
        )
    if ctx.devices is not None and req.device_id and (
        req.device_risk
        or ctx.device_eval["instant_risk"] > 0
        or prev_device_ewma is not None
    ):
        try:
            ctx.devices.upsert(
                device_id=str(req.device_id),
                ewma_risk=float(ctx.device_eval["ewma_risk"]),
                instant_risk=float(ctx.device_eval["instant_risk"]),
                flags=dict(ctx.device_eval.get("normalized") or {}),
                driver_id=int(req.driver_id),
                user_id=req.user_id,
            )
        except Exception:
            _LOG.exception(
                "Device integrity persist failed device=%s", req.device_id
            )

    conf_threshold = float(policy["dbscan"]["confidence_threshold"])
    last_conf = ctx.confidence_list[-1] if ctx.confidence_list else 0.0
    last_dwell = ctx.dwell_masks[-1] if ctx.dwell_masks else False
    original_reached_destination = last_conf >= conf_threshold or last_dwell

    has_replacement = bool(req.replacement_order_id)
    route_similarity: float | None = None
    if req.replacement_latlong:
        route_similarity = compute_route_similarity(
            stops,
            parse_latlong(req.replacement_latlong),
            float(policy["sequence"]["stop_match_radius_m"]),
        )
    ctx.replacement = evaluate_replacement(
        original_reached_destination=original_reached_destination,
        replacement_placed_delay_minutes=_replacement_delay_minutes(req),
        route_similarity=route_similarity,
        has_replacement=has_replacement,
        policy=policy["replacement"],
    )
    ctx.lineage_id = build_lineage_id(
        req.order_display_id, req.reassign_cancel_events
    )

    stage, stage_meta = resolve_cancel_stage(window.points, stops, policy=policy)
    ctx.stage = stage
    ctx.stage_meta = {**ctx.pickup_drop_meta, **stage_meta}
    ctx.after_pickup = cancel_after_pickup(stage)
    if ctx.pickup_drop_meta.get("drop_before_pickup"):
        ctx.after_pickup = False
    pickup_target = stops[0] if stops else (0.0, 0.0)
    ctx.progress = (
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

    ctx.near_dest = _cancel_near_destination(
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
    if ctx.driver_chains is not None:
        cross_order_chain = ctx.driver_chains.count_recent(
            req.driver_id,
            as_of=req.cancel_ts,
            window_minutes=chain_lookback,
            exclude_order_id=req.order_display_id,
        )
    ctx.driver_chain_count = max(len(driver_ids), cross_order_chain + 1)
