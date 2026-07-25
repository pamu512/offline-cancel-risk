from datetime import datetime, timedelta
from pathlib import Path

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.dbscan_v5 import compute_stop_confidences
from offline_cancel_risk.settings import load_policy

POLICY = load_policy(Path("config/policy.default.yaml"))["dbscan"]


def test_empty_points_zero_confidence():
    r = compute_stop_confidences([], [(1.0, 2.0), (1.1, 2.1)], POLICY)
    assert r["confidence_list"] == []
    assert r["final_confidence"] == 0.0
    assert r["unique_clusters"] == 0


def test_dwell_cluster_beats_pass_through():
    stop_a = (1.350000, 103.820000)
    stop_b = (1.370000, 103.850000)  # ~3.5km away
    t0 = datetime(2024, 1, 1, 10, 0, 0)

    points: list[GpsPoint] = []
    # ~40 points tightly clustered within ~30m of stop A (jitter 0.0001°)
    # → DBSCAN core cluster; immediate tier stop-in-cluster ratio ≈ 1.0
    for i in range(40):
        jitter = ((i % 5) - 2) * 0.0001
        points.append(
            GpsPoint(
                lat=stop_a[0] + jitter,
                lon=stop_a[1] + jitter,
                ts=(t0 + timedelta(seconds=10 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=0.1,
            )
        )

    # Driving line through stop B: >drop_off_min_pts (30) fall inside extended
    # radius (800m), but spacing (~51m) > clustering_radius_m (50m) so DBSCAN
    # labels every point noise (cluster==-1) → stop-in-cluster ratio 0.
    base = t0 + timedelta(hours=1)
    step_deg = 0.00046  # ~51m in latitude
    n_line = 45
    for i in range(n_line):
        offset = (i - n_line // 2) * step_deg
        points.append(
            GpsPoint(
                lat=stop_b[0] + offset,
                lon=stop_b[1],
                ts=(base + timedelta(seconds=5 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=12.0,
            )
        )

    r = compute_stop_confidences(points, [stop_a, stop_b], POLICY)
    assert r["confidence_list"][0] > 0.75
    assert r["confidence_list"][1] < 0.3


def test_round_trip_single_visit_halves_last_confidence():
    stop = (1.350000, 103.820000)
    t0 = datetime(2024, 1, 1, 10, 0, 0)
    points = [
        GpsPoint(
            lat=stop[0] + ((i % 5) - 2) * 0.0001,
            lon=stop[1] + ((i % 5) - 2) * 0.0001,
            ts=(t0 + timedelta(seconds=10 * i)).strftime("%Y-%m-%d %H:%M:%S"),
            speed_mps=0.1,
        )
        for i in range(40)
    ]
    r = compute_stop_confidences(points, [stop, stop], POLICY)
    assert r["is_round_trip"] is True
    assert r["pk_visited_times"] == 1
    assert r["confidence_list"][0] > 0.75
    assert r["confidence_list"][1] == r["confidence_list"][0] / 2
