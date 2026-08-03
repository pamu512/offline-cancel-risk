"""Progress toward pickup/dropoff with heading A→B (not B→A) checks."""

from __future__ import annotations

from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import bearing_deg, haversine, heading_error_deg
from offline_cancel_risk.timeutil import parse_ts


def analyze_progress(
    points: list[GpsPoint],
    target: tuple[float, float],
    *,
    assign_ts: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Measure closing distance + heading alignment toward target.

    Progress credit requires distance shrinking **and** heading toward target
    when heading is available. Wrong-way heading (B→A) denies progress credit.
    """
    cfg = dict(policy.get("progress") or {})
    window_min = float(cfg.get("window_minutes", 8))
    max_err = float(cfg.get("max_heading_error_deg", 60))
    away_err = float(cfg.get("away_heading_error_deg", 120))
    min_points = int(cfg.get("min_points", 3))
    no_progress_ratio = float(cfg.get("no_progress_ratio", 0.15))

    empty = {
        "progress_ratio": 0.0,
        "distance_closed_m": 0.0,
        "toward_heading_fraction": None,
        "away_heading_fraction": None,
        "heading_available": False,
        "no_progress": False,
        "wrong_direction": False,
        "reasons": [],
    }
    if not points or len(points) < min_points:
        return empty

    assign = parse_ts(assign_ts)
    end = assign.timestamp() + window_min * 60.0
    window = [
        p
        for p in points
        if assign <= parse_ts(p.ts) and parse_ts(p.ts).timestamp() <= end
    ]
    if len(window) < min_points:
        window = sorted(points, key=lambda p: parse_ts(p.ts))[: max(min_points, 5)]
    window = sorted(window, key=lambda p: parse_ts(p.ts))
    if len(window) < 2:
        return empty

    tlat, tlon = target
    d0 = haversine(window[0].lat, window[0].lon, tlat, tlon)
    d1 = haversine(window[-1].lat, window[-1].lon, tlat, tlon)
    closed = d0 - d1
    progress_ratio = closed / d0 if d0 > 1.0 else (1.0 if closed > 0 else 0.0)

    headed = [p for p in window if p.heading_deg is not None]
    toward = away = 0
    heading_available = len(headed) >= min_points
    if heading_available:
        for p in headed:
            brg = bearing_deg(p.lat, p.lon, tlat, tlon)
            err = heading_error_deg(float(p.heading_deg), brg)  # type: ignore[arg-type]
            if err <= max_err:
                toward += 1
            elif err >= away_err:
                away += 1
    n_h = max(len(headed), 1)
    toward_frac = toward / n_h if heading_available else None
    away_frac = away / n_h if heading_available else None

    reasons: list[str] = []
    wrong_direction = bool(
        heading_available and away_frac is not None and away_frac >= 0.5
    )
    # Path closing without toward heading (or with away) does not count as good progress.
    effective_progress = progress_ratio
    if heading_available:
        if wrong_direction:
            effective_progress = min(effective_progress, 0.0)
            reasons.append("wrong_direction")
        elif toward_frac is not None and toward_frac < 0.4 and progress_ratio > 0:
            # Moving somehow but not heading A→B
            effective_progress = min(effective_progress, progress_ratio * 0.25)
            reasons.append("heading_misaligned")
    else:
        reasons.append("heading_unavailable")

    no_progress = effective_progress < no_progress_ratio and d0 > float(
        cfg.get("min_start_distance_m", 200)
    )
    if no_progress:
        reasons.append("no_progress_to_pickup")

    return {
        "progress_ratio": float(effective_progress),
        "raw_progress_ratio": float(progress_ratio),
        "distance_closed_m": float(closed),
        "toward_heading_fraction": toward_frac,
        "away_heading_fraction": away_frac,
        "heading_available": heading_available,
        "no_progress": no_progress,
        "wrong_direction": wrong_direction,
        "reasons": reasons,
    }
