from __future__ import annotations

import math
from typing import Any

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.geo import haversine
from offline_cancel_risk.timeutil import parse_ts


def sequence_match_score(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    policy: dict[str, Any],
) -> float:
    """Score how much of the stop path exists in time order on the GPS track."""
    if not stops:
        return 0.0

    radius_m = float(policy["stop_match_radius_m"])
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


def first_visit_ts(
    points: list[GpsPoint],
    stop: tuple[float, float],
    radius_m: float,
) -> str | None:
    ordered = sorted(points, key=lambda p: parse_ts(p.ts))
    lat, lon = stop
    for p in ordered:
        if haversine(p.lat, p.lon, lat, lon) <= radius_m:
            return p.ts
    return None


def pickup_drop_order(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    *,
    pickup_radius_m: float,
    dropoff_radius_m: float,
) -> dict[str, Any]:
    """First visits to pickup (stops[0]) and drop (stops[-1])."""
    empty = {
        "pickup_ts": None,
        "drop_ts": None,
        "drop_after_pickup": None,
        "drop_before_pickup": False,
        "both_visited": False,
    }
    if not points or len(stops) < 2:
        return empty
    pickup = stops[0]
    dropoff = stops[-1]
    pickup_ts = first_visit_ts(points, pickup, pickup_radius_m)
    drop_ts = first_visit_ts(points, dropoff, dropoff_radius_m)
    out: dict[str, Any] = {
        "pickup_ts": pickup_ts,
        "drop_ts": drop_ts,
        "drop_after_pickup": None,
        "drop_before_pickup": False,
        "both_visited": bool(pickup_ts and drop_ts),
    }
    if pickup_ts and drop_ts:
        after = parse_ts(drop_ts) > parse_ts(pickup_ts)
        out["drop_after_pickup"] = after
        out["drop_before_pickup"] = not after
    return out


def _stop_radius(i: int, n: int, pickup_r: float, drop_r: float, mid_r: float) -> float:
    if i == 0:
        return pickup_r
    if i == n - 1:
        return drop_r
    return mid_r


def ordered_stop_visits(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    *,
    pickup_radius_m: float,
    dropoff_radius_m: float,
    middle_radius_m: float,
) -> dict[str, Any]:
    """First-visit timestamps per stop; enforce pickup → middles → drop chronology."""
    n = len(stops)
    visit_ts: list[str | None] = [
        first_visit_ts(
            points,
            stops[i],
            _stop_radius(i, n, pickup_radius_m, dropoff_radius_m, middle_radius_m),
        )
        for i in range(n)
    ]
    middle_n = max(0, n - 2)
    middles_hit = sum(1 for i in range(1, n - 1) if visit_ts[i] is not None)
    order_ok = True
    last: str | None = None
    for ts in visit_ts:
        if ts is None:
            continue
        if last is not None and parse_ts(ts) <= parse_ts(last):
            order_ok = False
            break
        last = ts

    pickup_ts = visit_ts[0] if n else None
    drop_ts = visit_ts[-1] if n >= 2 else None
    drop_before = bool(
        pickup_ts and drop_ts and parse_ts(drop_ts) <= parse_ts(pickup_ts)
    )
    # Middles must fall strictly between pickup and drop when all three exist.
    middles_in_window = True
    if pickup_ts and drop_ts and not drop_before:
        p0, p1 = parse_ts(pickup_ts), parse_ts(drop_ts)
        for i in range(1, n - 1):
            ts = visit_ts[i]
            if ts is None:
                continue
            t = parse_ts(ts)
            if not (p0 < t < p1):
                middles_in_window = False
                break

    return {
        "visit_ts": visit_ts,
        "pickup_ts": pickup_ts,
        "drop_ts": drop_ts,
        "drop_after_pickup": (
            None
            if not (pickup_ts and drop_ts)
            else (not drop_before)
        ),
        "drop_before_pickup": drop_before,
        "both_visited": bool(pickup_ts and drop_ts),
        "order_ok": order_ok and middles_in_window and not drop_before,
        "middle_n": middle_n,
        "middles_hit": middles_hit,
        "stops_visited": sum(1 for t in visit_ts if t is not None),
    }


def resolve_sequence_with_pickup_drop(
    points: list[GpsPoint],
    stops: list[tuple[float, float]],
    policy: dict[str, Any],
    *,
    stages_policy: dict[str, Any] | None = None,
) -> tuple[float, list[str], dict[str, Any]]:
    """Prefer ordered path match; if incomplete, require pickup→middles→drop."""
    path_score = sequence_match_score(points, stops, policy)
    stages = stages_policy or {}
    mid_r = float(policy.get("stop_match_radius_m", 150))
    pickup_r = float(stages.get("merchant_radius_m", mid_r))
    drop_r = float(stages.get("dropoff_radius_m", mid_r))
    # Fraction of middle stops that must be visited (default 1.0 = all).
    min_middle_frac = float(policy.get("min_middle_fraction", 1.0))
    min_middle_frac = max(0.0, min(1.0, min_middle_frac))

    order = ordered_stop_visits(
        points,
        stops,
        pickup_radius_m=pickup_r,
        dropoff_radius_m=drop_r,
        middle_radius_m=mid_r,
    )
    # Keep pickup_drop_order keys for stage meta compatibility.
    order = {
        **pickup_drop_order(
            points, stops, pickup_radius_m=pickup_r, dropoff_radius_m=drop_r
        ),
        **order,
    }
    reasons: list[str] = []

    if order["drop_before_pickup"]:
        return 0.0, ["drop_before_pickup"], order
    if not order["order_ok"] and order["both_visited"]:
        reasons.append("stop_order_violation")
        return 0.0, reasons, order

    if path_score >= 1.0 - 1e-9:
        return path_score, reasons, order

    # Path incomplete → fallback only if pickup→(middles)→drop chronology holds.
    if not order["both_visited"] or not order["drop_after_pickup"]:
        if order.get("pickup_ts") and not order.get("drop_ts"):
            reasons.append("pickup_only")
            return max(path_score, 0.5), reasons, order
        return path_score, reasons, order

    middle_n = int(order["middle_n"])
    middles_hit = int(order["middles_hit"])
    if middle_n > 0:
        need = int(math.ceil(middle_n * min_middle_frac))
        if middles_hit < need:
            reasons.append("middle_stops_missing")
            # Partial credit only from path match / visited fraction — no full bump.
            visited_frac = float(order["stops_visited"]) / float(len(stops))
            return max(path_score, visited_frac * 0.5), reasons, order

    if not order["order_ok"]:
        reasons.append("stop_order_violation")
        return 0.0, reasons, order

    reasons.append("pickup_drop_order")
    return 1.0, reasons, order
