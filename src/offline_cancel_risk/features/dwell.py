from datetime import datetime
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.fromisoformat(ts)


def dwell_stop_mask(
    points: list[GpsPoint],
    stop: tuple[float, float],
    policy: dict[str, Any],
) -> bool:
    radius_m = policy["radius_m"]
    max_speed_mps = policy["max_speed_mps"]
    min_dwell_seconds = policy["min_dwell_seconds"]
    stop_lat, stop_lon = stop

    near = [
        p
        for p in points
        if haversine(p.lat, p.lon, stop_lat, stop_lon) <= radius_m
    ]
    if not near:
        return False

    run_start: datetime | None = None
    prev_ts: datetime | None = None
    for p in near:
        ts = _parse_ts(p.ts)
        low_speed = p.speed_mps is not None and p.speed_mps <= max_speed_mps
        if low_speed:
            if run_start is None:
                run_start = ts
            prev_ts = ts
            if (prev_ts - run_start).total_seconds() >= min_dwell_seconds:
                return True
        else:
            run_start = None
            prev_ts = None
    return False
