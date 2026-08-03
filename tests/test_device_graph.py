from pathlib import Path

from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.device_graph import DeviceGraphStore


def _policy() -> dict:
    return {
        "device_graph": {
            "window_days": 30,
            "min_support": 3,
            "max_drivers_per_device": 3,
            "max_users_per_device": 3,
            "max_devices_per_driver": 4,
            "shared_pair_min_sightings": 1,
        },
        "abuse": {
            "multi_account_device_bonus": 0.25,
            "multi_user_device_bonus": 0.2,
            "device_hopping_bonus": 0.2,
            "shared_device_pair_bonus": 0.25,
        },
    }


def test_multi_account_device_signal(tmp_path: Path):
    g = DeviceGraphStore(tmp_path / "g.db")
    for i, did in enumerate((1, 2, 3)):
        g.observe(
            device_id="phone-a",
            driver_id=did,
            user_id=None,
            event_ts=f"2026-08-01T10:0{i}:00Z",
        )
    ev = g.evaluate(
        device_id="phone-a",
        driver_id=3,
        user_id=None,
        as_of="2026-08-01T12:00:00Z",
        policy=_policy(),
    )
    assert ev["drivers_on_device"] == 3
    assert "multi_account_device" in ev["signals"]


def test_device_hopping_signal(tmp_path: Path):
    g = DeviceGraphStore(tmp_path / "g.db")
    for i in range(4):
        g.observe(
            device_id=f"dev-{i}",
            driver_id=9,
            user_id=None,
            event_ts=f"2026-08-01T11:0{i}:00Z",
        )
    ev = g.evaluate(
        device_id="dev-0",
        driver_id=9,
        user_id=None,
        as_of="2026-08-01T12:00:00Z",
        policy=_policy(),
    )
    assert ev["devices_for_driver"] == 4
    assert "device_hopping" in ev["signals"]


def test_shared_device_pair_from_user_edge(tmp_path: Path):
    g = DeviceGraphStore(tmp_path / "g.db")
    g.observe(
        device_id="shared",
        driver_id=1,
        user_id=None,
        event_ts="2026-08-01T10:00:00Z",
    )
    g.observe(
        device_id="shared",
        driver_id=None,
        user_id=77,
        event_ts="2026-08-01T11:00:00Z",
    )
    ev = g.evaluate(
        device_id="shared",
        driver_id=1,
        user_id=77,
        as_of="2026-08-01T12:00:00Z",
        policy=_policy(),
    )
    assert ev["shared_device_pair"] is True
    assert "shared_device_pair" in ev["signals"]


def test_support_gate_blocks_small_sample(tmp_path: Path):
    g = DeviceGraphStore(tmp_path / "g.db")
    g.observe(device_id="x", driver_id=1, user_id=None, event_ts="2026-08-01T10:00:00Z")
    g.observe(device_id="x", driver_id=2, user_id=None, event_ts="2026-08-01T10:01:00Z")
    # only 2 sightings, thr drivers=3 anyway
    ev = g.evaluate(
        device_id="x",
        driver_id=2,
        user_id=None,
        as_of="2026-08-01T12:00:00Z",
        policy=_policy(),
    )
    assert "multi_account_device" not in ev["signals"]


def test_abuse_applies_graph_bonuses():
    score, reasons = abuse_feature_score(
        {
            "order_still_active": False,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
            "device_graph_signals": ["multi_account_device", "device_hopping"],
        },
        {
            "multi_account_device_bonus": 0.25,
            "device_hopping_bonus": 0.2,
        },
    )
    assert score >= 0.45
    assert "multi_account_device" in reasons
    assert "device_hopping" in reasons
