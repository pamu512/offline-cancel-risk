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

    # Line of moving points that pass within 200m of stop B once
    base = t0 + timedelta(hours=1)
    for i in range(8):
        # Approach then leave: closest pass at i=3 (~0.001° ≈ 110m)
        offset = (i - 3) * 0.002
        points.append(
            GpsPoint(
                lat=stop_b[0] + offset,
                lon=stop_b[1],
                ts=(base + timedelta(seconds=15 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=12.0,
            )
        )

    r = compute_stop_confidences(points, [stop_a, stop_b], POLICY)
    assert r["confidence_list"][0] > 0.75
    assert r["confidence_list"][1] < 0.3
