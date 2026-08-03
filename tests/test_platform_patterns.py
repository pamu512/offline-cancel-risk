from datetime import datetime, timedelta, timezone
from pathlib import Path

from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.features.evidence import build_evidence
from offline_cancel_risk.features.geo import bearing_deg, heading_error_deg
from offline_cancel_risk.features.gps_integrity import analyze_gps_integrity
from offline_cancel_risk.features.progress import analyze_progress
from offline_cancel_risk.features.stages import cancel_after_pickup, resolve_cancel_stage
from offline_cancel_risk.settings import load_policy


def _ts(minutes: int) -> str:
    base = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def test_bearing_and_heading_error():
    # Roughly north of origin
    brg = bearing_deg(0.0, 0.0, 0.1, 0.0)
    assert brg < 10 or brg > 350
    assert heading_error_deg(0.0, brg) < 15
    assert heading_error_deg(180.0, brg) > 150


def test_progress_toward_a_to_b_not_b_to_a():
    policy = load_policy("config/policy.default.yaml")
    # Target ~1.1km north of 1.0,2.0
    target = (1.01, 2.0)
    # Heading ~0 (north) while moving toward target = A→B
    toward = [
        GpsPoint(1.0, 2.0, _ts(0), 5.0, heading_deg=0.0),
        GpsPoint(1.003, 2.0, _ts(2), 5.0, heading_deg=5.0),
        GpsPoint(1.006, 2.0, _ts(4), 5.0, heading_deg=0.0),
        GpsPoint(1.008, 2.0, _ts(6), 5.0, heading_deg=10.0),
    ]
    good = analyze_progress(toward, target, assign_ts=_ts(0), policy=policy)
    assert good["wrong_direction"] is False
    assert good["progress_ratio"] > 0.1

    # Heading ~180 (south) while south of target = B→A / away
    away = [
        GpsPoint(1.0, 2.0, _ts(0), 5.0, heading_deg=180.0),
        GpsPoint(0.997, 2.0, _ts(2), 5.0, heading_deg=175.0),
        GpsPoint(0.994, 2.0, _ts(4), 5.0, heading_deg=180.0),
        GpsPoint(0.991, 2.0, _ts(6), 5.0, heading_deg=170.0),
    ]
    bad = analyze_progress(away, target, assign_ts=_ts(0), policy=policy)
    assert bad["wrong_direction"] is True
    assert "wrong_direction" in bad["reasons"]
    assert bad["progress_ratio"] <= 0.0


def test_cancel_stage_near_dropoff():
    policy = load_policy("config/policy.default.yaml")
    stops = [(1.0, 2.0), (1.02, 2.0)]
    points = [
        GpsPoint(1.0, 2.0, _ts(0), 1.0, heading_deg=0.0),
        GpsPoint(1.019, 2.0, _ts(10), 1.0, heading_deg=0.0),
    ]
    stage, meta = resolve_cancel_stage(points, stops, policy=policy)
    assert stage == "near_dropoff"
    assert cancel_after_pickup(stage)
    assert meta["dropoff_dist_m"] is not None


def test_teleport_damps():
    policy = load_policy("config/policy.default.yaml")
    points = [
        GpsPoint(1.0, 2.0, _ts(0), 1.0),
        GpsPoint(1.5, 2.0, _ts(1), 1.0),  # huge jump in 1 minute
    ]
    integrity = analyze_gps_integrity(points, policy=policy)
    assert integrity["teleport"] or integrity["impossible_speed"]
    assert integrity["stop_confidence_multiplier"] < 1.0


def test_entity_cancel_stats_pair(tmp_path: Path):
    store = EntityCancelStatsStore(tmp_path / "c.db")
    for i in range(3):
        store.record_cancel(
            driver_id=7,
            user_id=9,
            order_display_id=f"O{i}",
            event_ts=_ts(i * 10),
        )
    st = store.stats(
        driver_id=7,
        user_id=9,
        as_of=_ts(40),
        window_minutes=120,
        exclude_order_id="",
    )
    assert st["driver_cancel_count"] == 3
    assert st["pair_cancel_count"] == 3


def test_evidence_pack_includes_wrong_direction():
    ev = build_evidence(
        stage="pre_pickup",
        stage_meta={"dropoff_dist_m": 900.0},
        progress={
            "wrong_direction": True,
            "no_progress": True,
            "away_heading_fraction": 0.8,
            "progress_ratio": 0.0,
        },
        integrity={"teleport": False, "impossible_speed": False},
        cancel_rate=2.5,
        pair_cancel_count=4,
        driver_chain_count=3,
        abuse_reasons=["wrong_direction"],
        theft_reasons=[],
        final_stop_confidence=0.2,
    )
    names = {e["feature"] for e in ev}
    assert "wrong_direction" in names
    assert "entity_cancel_rate" in names
