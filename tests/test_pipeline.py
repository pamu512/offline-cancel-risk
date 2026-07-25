import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import load_policy

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def _pickup_cluster(n: int = 40) -> list[GpsPoint]:
    """Dense GPS at pickup only (not destination), spanning assign→cancel window."""
    base = datetime(2024, 1, 1, 10, 0, 0)
    points: list[GpsPoint] = []
    for i in range(n):
        # tiny jitter stays well inside pickup radius / DBSCAN eps
        lat = PICKUP[0] + (i % 5) * 0.00001
        lon = PICKUP[1] + (i % 3) * 0.00001
        ts = base + timedelta(minutes=i * 2)
        points.append(GpsPoint(lat=lat, lon=lon, ts=ts.strftime("%Y-%m-%d %H:%M:%S"), speed_mps=0.3))
    return points


@pytest.mark.asyncio
async def test_assess_selective_theft_and_idempotent_rerun(tmp_path: Path):
    policy = load_policy(Path("config/policy.default.yaml"))
    req = AssessRequest(
        order_display_id="ORD-THEFT-1",
        driver_id=42,
        cancel_ts="2024-01-01 11:20:00",
        assign_ts="2024-01-01 10:00:00",
        latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        path_point_num=2,
        order_status="CANCELLED",
        category="FOOD",
        order_value=800.0,
        currency="PHP",
        next_driver_no_order=True,
    )
    gps = FakeGpsClient(_pickup_cluster())
    stream_path = tmp_path / "data" / "risk_events.jsonl"
    db_path = tmp_path / "assessments.db"
    stream = JsonlStreamPublisher(stream_path=str(stream_path))
    table = SqliteTablePublisher(sqlite_path=str(db_path))

    first = await assess_order(req, gps, policy, stream=stream, table=table)

    assert first.flags.selective_theft == 1
    assert first.scores.cancelled_offline < 0.75
    assert first.model_version == "none"
    assert first.policy_hash
    assert first.assessment_generation == 1

    second = await assess_order(req, gps, policy, stream=stream, table=table)

    assert second.assessment_generation == 1
    assert second.policy_hash == first.policy_hash
    assert second.model_dump() == first.model_dump()

    lines = stream_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["order_display_id"] == "ORD-THEFT-1"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
