from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts


def sequence_match_score(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    policy: dict[str, Any],
) -> float:
    if not stops:
        return 0.0

    radius_m = policy["stop_match_radius_m"]
    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
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
