from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts


def dwell_stop_mask(
    points: list[GpsPoint],
    stop: tuple[float, float],
    policy: dict[str, Any],
) -> bool:
    """True if any contiguous in-radius low-speed run lasts >= min_dwell_seconds.

    Iterates time-sorted points without pre-filtering: leaving the stop radius,
    exceeding max speed, or crawling farther than max_run_displacement_m from the
    run anchor breaks the current run (away gaps do not merge).
    """
    radius_m = policy["radius_m"]
    max_speed_mps = policy["max_speed_mps"]
    min_dwell_seconds = policy["min_dwell_seconds"]
    max_disp = float(policy.get("max_run_displacement_m") or 0.0)
    stop_lat, stop_lon = stop

    if not points:
        return False

    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
    run_start = None
    run_anchor: GpsPoint | None = None
    prev_ts = None
    for p in ordered:
        ts = parse_ts(p.ts)
        in_radius = haversine(p.lat, p.lon, stop_lat, stop_lon) <= radius_m
        # Missing speed inside the pin: treat as stationary (0). Outside pin, still
        # require an explicit low speed so drive-bys without speed don't invent dwell.
        if p.speed_mps is None:
            low_speed = in_radius
        else:
            low_speed = p.speed_mps <= max_speed_mps
        crawling = False
        if max_disp > 0 and run_anchor is not None:
            crawling = (
                haversine(run_anchor.lat, run_anchor.lon, p.lat, p.lon) > max_disp
            )
        if in_radius and low_speed and not crawling:
            if run_start is None:
                run_start = ts
                run_anchor = p
            prev_ts = ts
            if (prev_ts - run_start).total_seconds() >= min_dwell_seconds:
                return True
        else:
            run_start = None
            run_anchor = None
            prev_ts = None
    return False
