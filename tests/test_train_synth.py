import pytest
from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.pipeline.score_build import ML_FEATURE_KEYS
from offline_cancel_risk.settings import load_policy
from pathlib import Path
from datetime import datetime, timedelta

PICKUP = (14.55, 121.02)

def _points(n=20):
    base = datetime(2024, 1, 1, 10, 0, 0)
    return [
        GpsPoint(lat=PICKUP[0], lon=PICKUP[1],
                 ts=(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                 speed_mps=0.2)
        for i in range(n)
    ]

@pytest.mark.asyncio
async def test_assess_feature_sink(tmp_path: Path):
    assert ML_FEATURE_KEYS == (
        "final_stop_confidence", "sequence_score", "dwell_fraction",
        "abuse_score", "theft_score",
    )
    sink: dict[str, float] = {}
    req = AssessRequest(
        order_display_id="SINK-1", driver_id=1,
        cancel_ts="2024-01-01 11:20:00", assign_ts="2024-01-01 10:00:00",
        latlong=f"{PICKUP[0]}|{PICKUP[1]},14.65|121.08",
        path_point_num=2, order_status="CANCELLED", category="FOOD",
        order_value=100.0, currency="PHP",
    )
    await assess_order(
        req, FakeGpsClient(_points()),
        load_policy("config/policy.default.yaml"),
        stream=JsonlStreamPublisher(stream_path=tmp_path / "e.jsonl"),
        table=SqliteTablePublisher(sqlite_path=tmp_path / "a.db"),
        feature_sink=sink,
    )
    assert set(ML_FEATURE_KEYS) <= set(sink)
