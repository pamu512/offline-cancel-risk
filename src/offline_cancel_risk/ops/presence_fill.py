"""Downstream place/vehicle fill-rate from latest assessment payloads."""

from __future__ import annotations

from collections import Counter
from typing import Any

from offline_cancel_risk.api.schemas import AssessmentResult


def presence_fill_report(
    results: list[AssessmentResult],
) -> dict[str, Any]:
    """Summarize how often Downstream sent non-unknown place/vehicle classes."""
    n = len(results)
    place_known = 0
    vehicle_known = 0
    place_counts: Counter[str] = Counter()
    vehicle_counts: Counter[str] = Counter()
    for r in results:
        gw = r.gps_window or {}
        place = str(gw.get("presence_place_class") or "unknown")
        vehicle = str(gw.get("presence_vehicle_class") or "unknown")
        place_counts[place] += 1
        vehicle_counts[vehicle] += 1
        if place != "unknown":
            place_known += 1
        if vehicle != "unknown":
            vehicle_known += 1
    return {
        "n": n,
        "place_known": place_known,
        "vehicle_known": vehicle_known,
        "place_fill_rate": (place_known / n) if n else 0.0,
        "vehicle_fill_rate": (vehicle_known / n) if n else 0.0,
        "place_counts": dict(place_counts),
        "vehicle_counts": dict(vehicle_counts),
        "contract": {
            "place_class": [
                "unknown",
                "curb",
                "residential",
                "apartment",
                "commercial",
            ],
            "vehicle_class": [
                "unknown",
                "walker",
                "cycle",
                "two_wheel",
                "van_pickup",
                "large_4w",
                "box_truck",
                "semi",
            ],
            "skip_behavior": "omitted or unrecognized → unknown (factor 1.0)",
        },
    }
