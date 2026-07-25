from datetime import datetime
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.fromisoformat(ts)


def sequence_match_score(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    policy: dict[str, Any],
) -> float:
    if not stops:
        return 0.0

    radius_m = policy["stop_match_radius_m"]
    ordered = sorted(points, key=lambda p: _parse_ts(p.ts))
    matched = 0
    cursor = 0
    for stop_lat, stop_lon in stops:
        found = False
        while cursor < len(ordered):
            p = ordered[cursor]
            cursor += 1
            if haversine(p.lat, p.lon, stop_lat, stop_lon) <= radius_m:
                matched += 1
                found = True
                break
        if not found:
            break
    return matched / len(stops)
