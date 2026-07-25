from pathlib import Path

from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.operating_point import resolve_operating_point
from offline_cancel_risk.settings import load_policy


def test_peak_has_higher_min_precision_than_surplus():
    cfg = load_policy("config/operating_point.default.yaml")
    peak = resolve_operating_point(cfg, 0.7)
    surplus = resolve_operating_point(cfg, 1.5)
    assert peak["min_precision"] > surplus["min_precision"]
    assert surplus["min_recall"] > peak["min_recall"]
    assert peak["regime"] == "peak"
    assert surplus["regime"] == "surplus"


def test_mid_interpolates():
    cfg = load_policy("config/operating_point.default.yaml")
    mid = resolve_operating_point(cfg, 1.0)
    assert mid["regime"] == "mid"
    assert cfg["peak"]["min_precision"] > mid["min_precision"] > cfg["surplus"]["min_precision"]


def test_forecast_and_hardgates_roundtrip(tmp_path: Path):
    fs = SupplyForecastStore(tmp_path / "cp.db")
    fs.upsert(
        [
            {
                "region_code": "PH",
                "city_code": "MNL",
                "period_start": "2026-07-25T00:00:00Z",
                "period_end": "2026-07-26T00:00:00Z",
                "forecast_supply": 80.0,
                "forecast_demand": 100.0,
                "source": "driver_ops",
            }
        ]
    )
    row = fs.active("PH", "MNL", "2026-07-25T12:00:00Z")
    assert row is not None
    assert abs(row["forecast_supply"] / row["forecast_demand"] - 0.8) < 1e-9
    hg = EnforcementHardgateStore(tmp_path / "cp.db")
    hg.upsert("PH", "MNL", window="hour", max_enforcements=10, heads=["*"], actor="ops")
    assert hg.get("PH", "MNL")["hour"]["max_enforcements"] == 10
    cb = hg.record_clawback("PH", "MNL", ttl_minutes=30, reason="supply_dip")
    assert "until_ts" in cb
    assert hg.clawback_active("PH", "MNL")
