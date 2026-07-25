from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.dwell import dwell_stop_mask
from offline_cancel_risk.features.sequence import sequence_match_score


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
