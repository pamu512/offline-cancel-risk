from pathlib import Path

from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.device_integrity import (
    evaluate_device_integrity,
    instant_device_risk,
    normalize_device_risk,
)
from offline_cancel_risk.features.device_store import DeviceIntegrityStore


def test_instant_risk_is_max_not_sum():
    n = normalize_device_risk(
        {"spoof_suspected": True, "rooted": True, "risk_score": 0.4}
    )
    # spoof 0.85 beats rooted 0.55 and vendor 0.4; not 0.85+0.55
    assert instant_device_risk(n) == 0.85


def test_evaluate_fires_and_scales_bonus():
    policy = {
        "device_integrity": {
            "ewma_alpha": 0.4,
            "risk_threshold": 0.7,
            "abuse_bonus_scale": 0.35,
            "gps_dampen": 0.5,
        }
    }
    cold = evaluate_device_integrity({"rooted": True}, prev_ewma=None, policy=policy)
    assert not cold["fires"]
    assert cold["abuse_bonus"] == 0.0

    hot = evaluate_device_integrity(
        {"spoof_suspected": True}, prev_ewma=None, policy=policy
    )
    assert hot["fires"]
    assert hot["instant_risk"] == 0.85
    assert abs(hot["abuse_bonus"] - 0.35 * 0.85) < 1e-9
    assert hot["gps_multiplier"] < 1.0


def test_ewma_persists_heat(tmp_path: Path):
    store = DeviceIntegrityStore(tmp_path / "d.db")
    policy = {
        "device_integrity": {
            "ewma_alpha": 0.4,
            "risk_threshold": 0.7,
            "abuse_bonus_scale": 0.35,
            "gps_dampen": 0.5,
        }
    }
    first = evaluate_device_integrity(
        {"spoof_suspected": True}, prev_ewma=None, policy=policy
    )
    store.upsert(
        device_id="dev-1",
        ewma_risk=first["ewma_risk"],
        instant_risk=first["instant_risk"],
        flags=first["normalized"],
        driver_id=9,
        user_id=None,
    )
    prev = store.get("dev-1")
    assert prev is not None
    assert prev["last_driver_id"] == 9

    # Clean sighting still carries EWMA heat but drops below τ=0.7
    second = evaluate_device_integrity(
        {}, prev_ewma=float(prev["ewma_risk"]), policy=policy
    )
    assert abs(second["ewma_risk"] - (0.4 * 0.0 + 0.6 * 0.85)) < 1e-9
    assert not second["fires"]

    # further cleans decay toward 0
    ewma = second["ewma_risk"]
    for _ in range(3):
        ewma = evaluate_device_integrity({}, prev_ewma=ewma, policy=policy)[
            "ewma_risk"
        ]
    assert ewma < second["ewma_risk"]


def test_abuse_uses_device_eval_bonus():
    score, reasons = abuse_feature_score(
        {
            "order_still_active": False,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
            "device_eval": {
                "fires": True,
                "abuse_bonus": 0.3,
                "reasons": ["device_integrity", "device_spoof_suspected"],
            },
        },
        {},
    )
    assert score >= 0.3
    assert "device_integrity" in reasons
