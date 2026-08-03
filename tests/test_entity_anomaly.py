from pathlib import Path

from offline_cancel_risk.features.anomaly import (
    EntityAnomalyStore,
    evaluate_entity_anomaly,
    mad_zscore,
)


def test_mad_zscore_basic():
    sample = [1.0] * 10
    z = mad_zscore(4.0, sample, epsilon=0.001)
    assert z is not None
    assert z > 3.0


def test_anomaly_self_shadow(tmp_path: Path):
    store = EntityAnomalyStore(tmp_path / "a.db")
    policy = {
        "anomaly": {
            "mode": "shadow",
            "window_n": 20,
            "peer_window_n": 200,
            "min_support": 8,
            "z_threshold": 3.0,
            "epsilon": 0.001,
            "abuse_bonus": 0.15,
            "features": ["cancel_abuse"],
        }
    }
    for i in range(10):
        evaluate_entity_anomaly(
            store=store,
            entity_key="driver:1",
            cohort="city:X",
            features={"cancel_abuse": 0.1},
            order_display_id=f"base-{i}",
            event_ts=f"2026-08-01T10:{i:02d}:00Z",
            policy=policy,
        )
    hot = evaluate_entity_anomaly(
        store=store,
        entity_key="driver:1",
        cohort="city:X",
        features={"cancel_abuse": 0.95},
        order_display_id="spike",
        event_ts="2026-08-01T11:00:00Z",
        policy=policy,
    )
    assert hot["fires"]
    assert "anomaly_self" in hot["signals"]
    assert hot["abuse_bonus"] == 0.0  # shadow
    assert any(r.startswith("anomaly_shadow:") for r in hot["reasons"])


def test_anomaly_peer_apply(tmp_path: Path):
    store = EntityAnomalyStore(tmp_path / "a.db")
    policy = {
        "anomaly": {
            "mode": "apply",
            "window_n": 20,
            "peer_window_n": 200,
            "min_support": 8,
            "z_threshold": 3.0,
            "epsilon": 0.001,
            "abuse_bonus": 0.15,
            "features": ["accept_cancel_rate"],
        }
    }
    for i in range(12):
        evaluate_entity_anomaly(
            store=store,
            entity_key=f"driver:{i+10}",
            cohort="city:Y",
            features={"accept_cancel_rate": 0.2},
            order_display_id=f"peer-{i}",
            event_ts=f"2026-08-01T10:{i:02d}:00Z",
            policy=policy,
        )
    hot = evaluate_entity_anomaly(
        store=store,
        entity_key="driver:99",
        cohort="city:Y",
        features={"accept_cancel_rate": 0.95},
        order_display_id="outlier",
        event_ts="2026-08-01T12:00:00Z",
        policy=policy,
    )
    assert "anomaly_peer" in hot["signals"]
    assert hot["abuse_bonus"] == 0.15
