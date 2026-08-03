"""Cancel-stage classification from GPS vs merchant/dropoff stops."""

from __future__ import annotations

from typing import Any, Literal

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts

CancelStage = Literal[
    "pre_pickup", "at_merchant", "en_route", "near_dropoff", "unknown"
]


def resolve_cancel_stage(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    *,
    policy: dict[str, Any],
) -> tuple[CancelStage, dict[str, Any]]:
    """Classify cancel context. Merchant ≈ first stop; dropoff ≈ last stop."""
    cfg = dict(policy.get("stages") or {})
    merchant_r = float(cfg.get("merchant_radius_m", 150))
    dropoff_r = float(cfg.get("dropoff_radius_m", 400))
    if not points or len(stops) < 1:
        return "unknown", {"merchant_dist_m": None, "dropoff_dist_m": None}

    last = max(points, key=lambda p: parse_ts(p.ts))
    merchant = stops[0]
    dropoff = stops[-1]
    m_dist = haversine(last.lat, last.lon, merchant[0], merchant[1])
    d_dist = haversine(last.lat, last.lon, dropoff[0], dropoff[1])
    meta = {
        "merchant_dist_m": m_dist,
        "dropoff_dist_m": d_dist,
        "merchant_radius_m": merchant_r,
        "dropoff_radius_m": dropoff_r,
    }

    ever_at_merchant = any(
        haversine(p.lat, p.lon, merchant[0], merchant[1]) <= merchant_r for p in points
    )

    if d_dist <= dropoff_r:
        return "near_dropoff", meta
    if m_dist <= merchant_r:
        return "at_merchant", meta
    if ever_at_merchant:
        return "en_route", meta
    return "pre_pickup", meta


def cancel_after_pickup(stage: CancelStage) -> bool:
    return stage in {"at_merchant", "en_route", "near_dropoff"}
