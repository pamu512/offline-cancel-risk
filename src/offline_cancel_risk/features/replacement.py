from dataclasses import dataclass, field
from typing import Any

from offline_cancel_risk.features.geo import haversine


@dataclass(frozen=True)
class ReplacementVerdict:
    valid: bool
    paths_passed: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)


def compute_route_similarity(
    original_stops: list[tuple[float, float]],
    replacement_stops: list[tuple[float, float]],
    radius_m: float,
) -> float:
    """Fraction of original stops matched within radius_m to any replacement stop."""
    if not original_stops or not replacement_stops:
        return 0.0
    matched = 0
    for olat, olon in original_stops:
        if any(
            haversine(olat, olon, rlat, rlon) <= radius_m
            for rlat, rlon in replacement_stops
        ):
            matched += 1
    return matched / len(original_stops)


def evaluate_replacement(
    *,
    original_reached_destination: bool,
    replacement_placed_delay_minutes: float | None,
    route_similarity: float | None,
    has_replacement: bool,
    policy: dict[str, Any],
) -> ReplacementVerdict:
    if not has_replacement:
        return ReplacementVerdict(
            valid=False,
            paths_passed=[],
            reason_codes=["no_replacement"],
        )

    max_delay = policy["max_place_delay_minutes"]
    route_min = policy["route_similarity_min"]
    paths_passed: list[str] = []

    if not original_reached_destination:
        paths_passed.append("gps")
    if (
        replacement_placed_delay_minutes is not None
        and replacement_placed_delay_minutes <= max_delay
    ):
        paths_passed.append("timing")
    if route_similarity is not None and route_similarity >= route_min:
        paths_passed.append("route")

    if paths_passed:
        return ReplacementVerdict(valid=True, paths_passed=paths_passed, reason_codes=[])

    return ReplacementVerdict(
        valid=False,
        paths_passed=[],
        reason_codes=["invalid_replacement"],
    )
