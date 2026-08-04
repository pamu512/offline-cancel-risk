import pytest

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.dwell_scale import resolve_stop_presence


def _points_every(gap_s: float, n: int = 20) -> list[GpsPoint]:
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        # ISO with fractional seconds so sub-second gaps survive parse_ts.
        ts = (t0 + timedelta(seconds=gap_s * i)).isoformat().replace("+00:00", "Z")
        out.append(GpsPoint(1.0, 2.0, ts, 0.2))
    return out


POLICY = {
    "dbscan": {
        "min_pts": 7,
        "drop_off_min_pts": 30,
        "autoscale_min_pts": True,
        "autoscale_ref_gap_seconds": 5.0,
    },
    "dwell": {
        "min_dwell_seconds": 120,
        "min_gap_samples": 3,
        "gap_seconds_min": 1.0,
        "gap_seconds_max": 30.0,
        "place_factors": {
            "unknown": 1.0,
            "curb": 0.85,
            "apartment": 1.35,
            "commercial": 1.5,
        },
        "vehicle_factors": {
            "unknown": 1.0,
            "walker": 0.55,
            "two_wheel": 0.7,
            "semi": 1.6,
        },
    },
    "gps": {"max_gap_minutes": 45},
}


def test_dense_pings_raise_min_pts():
    dense = resolve_stop_presence(_points_every(1.0), POLICY)
    sparse = resolve_stop_presence(_points_every(30.0), POLICY)
    assert dense["median_gap_s"] is not None
    assert dense["min_pts_effective"] > sparse["min_pts_effective"]
    assert sparse["min_pts_effective"] < POLICY["dbscan"]["min_pts"]


def test_ref_gap_recovers_min_pts_ref():
    r = resolve_stop_presence(_points_every(5.0), POLICY)
    assert r["autoscaled"] is True
    assert r["min_pts_effective"] == 7
    assert r["drop_off_min_pts_effective"] == 30


def test_place_vehicle_raise_dwell_target():
    base = resolve_stop_presence(
        _points_every(5.0), POLICY, place_class=None, vehicle_class=None
    )
    heavy = resolve_stop_presence(
        _points_every(5.0),
        POLICY,
        place_class="commercial",
        vehicle_class="semi",
    )
    light = resolve_stop_presence(
        _points_every(5.0),
        POLICY,
        place_class="curb",
        vehicle_class="walker",
    )
    assert heavy["dwell_target_s"] > base["dwell_target_s"] > light["dwell_target_s"]
    assert heavy["place_factor"] == 1.5
    assert heavy["vehicle_factor"] == 1.6
    assert light["vehicle_factor"] == 0.55


def test_missing_classes_neutral_factors():
    r = resolve_stop_presence(_points_every(5.0), POLICY)
    assert r["place_class"] == "unknown"
    assert r["vehicle_class"] == "unknown"
    assert r["place_factor"] == 1.0
    assert r["vehicle_factor"] == 1.0
    assert r["dwell_target_s"] == 120.0


def test_autoscale_off_keeps_policy_counts():
    pol = {
        **POLICY,
        "dbscan": {**POLICY["dbscan"], "autoscale_min_pts": False},
    }
    r = resolve_stop_presence(_points_every(1.0), pol, place_class="apartment")
    assert r["min_pts_effective"] == 7
    assert r["drop_off_min_pts_effective"] == 30
    assert r["dwell_target_s"] == 120.0 * 1.35


def test_too_few_gaps_falls_back_counts():
    r = resolve_stop_presence(_points_every(5.0, n=2), POLICY)
    assert r["min_pts_effective"] == 7
    assert r["autoscaled"] is False


def test_median_gap_clamped_for_autoscale():
    pol = {
        **POLICY,
        "dwell": {
            **POLICY["dwell"],
            "gap_seconds_min": 1.0,
            "gap_seconds_max": 30.0,
        },
    }
    # Burstier than floor → raw 0.2s but clamp to 1s (same min_pts ceiling path).
    burst = resolve_stop_presence(_points_every(0.2), pol)
    assert burst["median_gap_raw_s"] == pytest.approx(0.2)
    assert burst["median_gap_s"] == pytest.approx(1.0)
    # Sparser than ceiling → raw 60s clamped to 30s (not softer than 30s scale).
    sparse = resolve_stop_presence(_points_every(60.0), pol)
    assert sparse["median_gap_raw_s"] == pytest.approx(60.0)
    assert sparse["median_gap_s"] == pytest.approx(30.0)
    at_ceil = resolve_stop_presence(_points_every(30.0), pol)
    assert sparse["min_pts_effective"] == at_ceil["min_pts_effective"]
