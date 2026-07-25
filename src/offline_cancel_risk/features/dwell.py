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

    Iterates time-sorted points without pre-filtering: leaving the stop radius
    or exceeding max speed breaks the current run (away gaps do not merge).
    """
    radius_m = policy["radius_m"]
    max_speed_mps = policy["max_speed_mps"]
    min_dwell_seconds = policy["min_dwell_seconds"]
    stop_lat, stop_lon = stop

    if not points:
        return False

    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
    run_start = None
    prev_ts = None
    for p in ordered:
        ts = parse_ts(p.ts)
        in_radius = haversine(p.lat, p.lon, stop_lat, stop_lon) <= radius_m
        low_speed = p.speed_mps is not None and p.speed_mps <= max_speed_mps
        if in_radius and low_speed:
            if run_start is None:
                run_start = ts
            prev_ts = ts
            if (prev_ts - run_start).total_seconds() >= min_dwell_seconds:
                return True
        else:
            run_start = None
            prev_ts = None
    return False
