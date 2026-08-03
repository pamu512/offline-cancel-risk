"""Cancel-stage classification from GPS vs merchant/dropoff stops."""

from __future__ import annotations

from typing import Any, Literal

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.features.sequence import pickup_drop_order
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
    """Classify cancel context. Merchant ≈ first stop; dropoff ≈ last stop.

    Drop must come after pickup. If drop is visited before pickup, do not
    treat as near_dropoff / en_route completion.
    """
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
    order = (
        pickup_drop_order(
            points,
            stops,
            pickup_radius_m=merchant_r,
            dropoff_radius_m=dropoff_r,
        )
        if len(stops) >= 2
        else {
            "pickup_ts": None,
            "drop_ts": None,
            "drop_after_pickup": None,
            "drop_before_pickup": False,
            "both_visited": False,
        }
    )
    meta: dict[str, Any] = {
        "merchant_dist_m": m_dist,
        "dropoff_dist_m": d_dist,
        "merchant_radius_m": merchant_r,
        "dropoff_radius_m": dropoff_r,
        **order,
    }

    ever_at_merchant = any(
        haversine(p.lat, p.lon, merchant[0], merchant[1]) <= merchant_r for p in points
    )
    pickup_ok = bool(order.get("pickup_ts")) or ever_at_merchant

    if order.get("drop_before_pickup"):
        meta["invalid_order"] = True
        if m_dist <= merchant_r:
            return "at_merchant", meta
        return "unknown", meta

    if d_dist <= dropoff_r:
        # Near drop only counts after pickup evidence (path or first visit).
        if pickup_ok and order.get("drop_before_pickup") is not True:
            if order.get("both_visited") and order.get("drop_after_pickup") is False:
                return "unknown", meta
            return "near_dropoff", meta
        return "unknown", meta
    if m_dist <= merchant_r:
        return "at_merchant", meta
    if ever_at_merchant or order.get("pickup_ts"):
        return "en_route", meta
    return "pre_pickup", meta


def cancel_after_pickup(stage: CancelStage) -> bool:
    return stage in {"at_merchant", "en_route", "near_dropoff"}
