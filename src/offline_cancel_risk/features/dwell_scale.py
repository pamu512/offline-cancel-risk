"""Resolve target dwell D and ping-gap–scaled DBSCAN counts."""

from __future__ import annotations

from statistics import median
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.timeutil import parse_ts

_MIN_PTS_LO, _MIN_PTS_HI = 3, 30
_DROP_LO, _DROP_HI = 5, 80
_D_LO, _D_HI = 30.0, 600.0


def _factor(table: dict[str, Any] | None, key: str | None) -> tuple[str, float]:
    cls = (key or "unknown").strip().lower() or "unknown"
    factors = table or {}
    if cls not in factors:
        cls = "unknown"
    return cls, float(factors.get(cls, factors.get("unknown", 1.0)))


def _median_gap_seconds(
    points: list[GpsPoint], max_gap_minutes: float
) -> float | None:
    if len(points) < 2:
        return None
    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
    max_gap_s = max_gap_minutes * 60.0
    gaps: list[float] = []
    for i in range(1, len(ordered)):
        dt = (parse_ts(ordered[i].ts) - parse_ts(ordered[i - 1].ts)).total_seconds()
        if 0.0 < dt <= max_gap_s:
            gaps.append(dt)
    if not gaps:
        return None
    return float(median(gaps))


def resolve_stop_presence(
    points: list[GpsPoint],
    policy: dict[str, Any],
    *,
    place_class: str | None = None,
    vehicle_class: str | None = None,
) -> dict[str, Any]:
    """Return dwell target + effective DBSCAN counts for this assess window."""
    dwell = dict(policy.get("dwell") or {})
    dbscan = dict(policy.get("dbscan") or {})
    gps = dict(policy.get("gps") or {})

    d_base = float(dwell.get("min_dwell_seconds", 120))
    place_name, place_f = _factor(dwell.get("place_factors"), place_class)
    vehicle_name, vehicle_f = _factor(dwell.get("vehicle_factors"), vehicle_class)
    dwell_target = max(_D_LO, min(_D_HI, d_base * place_f * vehicle_f))

    min_pts_ref = int(dbscan.get("min_pts", 7))
    drop_ref = int(dbscan.get("drop_off_min_pts", 30))
    autoscale = bool(dbscan.get("autoscale_min_pts", False))
    tau_ref = float(dbscan.get("autoscale_ref_gap_seconds", 5.0))
    min_gap_samples = int(dwell.get("min_gap_samples", 3))
    max_gap_minutes = float(gps.get("max_gap_minutes", 45))

    tau_raw = _median_gap_seconds(points, max_gap_minutes)
    tau_lo = float(dwell.get("gap_seconds_min", 1.0))
    tau_hi = float(dwell.get("gap_seconds_max", 30.0))
    if tau_hi < tau_lo:
        tau_lo, tau_hi = tau_hi, tau_lo
    tau: float | None = None
    if tau_raw is not None:
        tau = max(tau_lo, min(tau_hi, float(tau_raw)))

    # Need enough deltas: n points → n-1 gaps; require min_gap_samples gaps.
    usable = 0
    if len(points) >= 2:
        ordered = sorted(points, key=lambda p: parse_ts(p.ts))
        max_gap_s = max_gap_minutes * 60.0
        for i in range(1, len(ordered)):
            dt = (
                parse_ts(ordered[i].ts) - parse_ts(ordered[i - 1].ts)
            ).total_seconds()
            if 0.0 < dt <= max_gap_s:
                usable += 1

    autoscaled = False
    min_pts_eff = min_pts_ref
    drop_eff = drop_ref
    if autoscale and tau is not None and usable >= min_gap_samples and tau > 0:
        # Preserve min_pts_ref at (τ_ref, D_base); scale with gap and target dwell.
        d_scale = (dwell_target / d_base) if d_base > 0 else 1.0
        gap_scale = tau_ref / tau
        scale = d_scale * gap_scale
        min_pts_eff = int(round(min_pts_ref * scale))
        drop_eff = int(round(drop_ref * scale))
        min_pts_eff = max(_MIN_PTS_LO, min(_MIN_PTS_HI, min_pts_eff))
        drop_eff = max(_DROP_LO, min(_DROP_HI, drop_eff))
        autoscaled = True

    return {
        "dwell_target_s": float(dwell_target),
        "dwell_base_s": d_base,
        "median_gap_s": tau,
        "median_gap_raw_s": tau_raw,
        "min_pts_effective": min_pts_eff,
        "drop_off_min_pts_effective": drop_eff,
        "place_class": place_name,
        "vehicle_class": vehicle_name,
        "place_factor": place_f,
        "vehicle_factor": vehicle_f,
        "autoscaled": autoscaled,
    }
