"""Impossible motion / teleport heuristics."""

from __future__ import annotations

from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts


def analyze_gps_integrity(
    points: list[GpsPoint],
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    cfg = dict(policy.get("gps_integrity") or {})
    max_speed = float(cfg.get("max_speed_mps", 55))  # ~198 km/h
    teleport_m = float(cfg.get("teleport_jump_m", 2000))
    dampen = float(cfg.get("stop_confidence_dampen", 0.5))

    reasons: list[str] = []
    max_seg_speed = 0.0
    max_jump = 0.0
    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
    for a, b in zip(ordered, ordered[1:]):
        dt = (parse_ts(b.ts) - parse_ts(a.ts)).total_seconds()
        dist = haversine(a.lat, a.lon, b.lat, b.lon)
        max_jump = max(max_jump, dist)
        if dt > 0.5:
            max_seg_speed = max(max_seg_speed, dist / dt)

    teleport = max_jump >= teleport_m
    impossible = max_seg_speed >= max_speed
    if teleport:
        reasons.append("gps_teleport")
    if impossible:
        reasons.append("gps_impossible_speed")

    multiplier = dampen if (teleport or impossible) else 1.0
    return {
        "teleport": teleport,
        "impossible_speed": impossible,
        "max_jump_m": max_jump,
        "max_segment_speed_mps": max_seg_speed,
        "stop_confidence_multiplier": multiplier,
        "reasons": reasons,
    }


def dampen_stop_confidence(confidence: float, integrity: dict[str, Any]) -> float:
    return max(
        0.0,
        min(1.0, float(confidence) * float(integrity.get("stop_confidence_multiplier", 1.0))),
    )
