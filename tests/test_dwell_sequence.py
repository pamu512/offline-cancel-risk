from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.features.sequence import (
    resolve_sequence_with_pickup_drop,
    sequence_match_score,
)
from offline_cancel_risk.features.stages import resolve_cancel_stage
from offline_cancel_risk.settings import load_policy


def test_dwell_requires_low_speed_duration():
    stop = (1.0, 2.0)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:01:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:02:30", 0.2),
    ]
    policy = {"min_dwell_seconds": 120, "max_speed_mps": 1.5, "radius_m": 150}
    assert dwell_stop_mask(points, stop, policy) is True


def test_pass_through_not_dwell():
    stop = (1.0, 2.0)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 12.0),
        GpsPoint(1.001, 2.0, "2024-01-01 10:00:20", 12.0),
    ]
    policy = {"min_dwell_seconds": 120, "max_speed_mps": 1.5, "radius_m": 150}
    assert dwell_stop_mask(points, stop, policy) is False


def test_two_short_dwells_separated_by_away_do_not_merge():
    """Leaving radius must break the run; two 60s visits ≠ one 120s dwell."""
    stop = (1.0, 2.0)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:01:00", 0.2),
        GpsPoint(1.05, 2.0, "2024-01-01 10:05:00", 0.2),  # far away
        GpsPoint(1.05, 2.0, "2024-01-01 10:10:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:20:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:21:00", 0.2),
    ]
    policy = {"min_dwell_seconds": 120, "max_speed_mps": 1.5, "radius_m": 150}
    assert dwell_stop_mask(points, stop, policy) is False


def test_sequence_score_full_order():
    # points visit stop0 then stop1 in time order inside radius
    stops = [(1.0, 2.0), (1.01, 2.0)]
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.0),
        GpsPoint(1.01, 2.0, "2024-01-01 10:05:00", 0.0),
    ]
    assert sequence_match_score(points, stops, {"stop_match_radius_m": 150}) == 1.0


def test_drop_before_pickup_zeros_sequence_and_blocks_near_dropoff():
    # Visits drop first, then pickup — invalid chronology when path incomplete
    pickup = (1.0, 2.0)
    drop = (1.02, 2.0)
    stops = [pickup, drop]
    points = [
        GpsPoint(1.02, 2.0, "2024-01-01 10:00:00", 0.0),  # drop first
        GpsPoint(1.0, 2.0, "2024-01-01 10:10:00", 0.0),  # pickup later
        GpsPoint(1.02, 2.0, "2024-01-01 10:20:00", 0.0),
    ]
    seq_policy = {"stop_match_radius_m": 150}
    score, reasons, order = resolve_sequence_with_pickup_drop(
        points, stops, seq_policy, stages_policy={"merchant_radius_m": 150, "dropoff_radius_m": 400}
    )
    assert score == 0.0
    assert "drop_before_pickup" in reasons
    assert order["drop_before_pickup"] is True

    policy = load_policy("config/policy.default.yaml")
    stage, meta = resolve_cancel_stage(points, stops, policy=policy)
    assert meta["drop_before_pickup"] is True
    assert stage != "near_dropoff"


def test_multi_stop_fallback_requires_middle_stops():
    pickup = (1.0, 2.0)
    mid = (1.01, 2.0)
    drop = (1.02, 2.0)
    stops = [pickup, mid, drop]
    # Pickup→drop only — must NOT get full sequence credit
    ends_only = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.0),
        GpsPoint(1.02, 2.0, "2024-01-01 10:15:00", 0.0),
    ]
    score, reasons, order = resolve_sequence_with_pickup_drop(
        ends_only,
        stops,
        {"stop_match_radius_m": 150, "min_middle_fraction": 1.0},
        stages_policy={"merchant_radius_m": 150, "dropoff_radius_m": 400},
    )
    assert sequence_match_score(ends_only, stops, {"stop_match_radius_m": 150}) < 1.0
    assert score < 1.0
    assert "middle_stops_missing" in reasons
    assert order["middles_hit"] == 0

    # Pickup→mid→drop in time — full fallback credit
    with_mid = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.0),
        GpsPoint(1.01, 2.0, "2024-01-01 10:08:00", 0.0),
        GpsPoint(1.02, 2.0, "2024-01-01 10:15:00", 0.0),
    ]
    score2, reasons2, order2 = resolve_sequence_with_pickup_drop(
        with_mid,
        stops,
        {"stop_match_radius_m": 150, "min_middle_fraction": 1.0},
        stages_policy={"merchant_radius_m": 150, "dropoff_radius_m": 400},
    )
    assert score2 == 1.0
    assert "pickup_drop_order" in reasons2 or order2["middles_hit"] == 1
    assert order2["middles_hit"] == 1


def test_two_stop_pickup_drop_fallback_still_works():
    stops = [(1.0, 2.0), (1.02, 2.0)]
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.0),
        GpsPoint(1.02, 2.0, "2024-01-01 10:15:00", 0.0),
    ]
    score, reasons, _ = resolve_sequence_with_pickup_drop(
        points,
        stops,
        {"stop_match_radius_m": 150},
        stages_policy={"merchant_radius_m": 150, "dropoff_radius_m": 400},
    )
    assert score == 1.0
    assert "pickup_drop_order" in reasons or score == 1.0
