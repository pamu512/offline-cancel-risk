import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def _pickup_cluster(n: int = 40) -> list[GpsPoint]:
    base = datetime(2024, 1, 1, 10, 0, 0)
    points: list[GpsPoint] = []
    for i in range(n):
        lat = PICKUP[0] + (i % 5) * 0.00001
        lon = PICKUP[1] + (i % 3) * 0.00001
        ts = base + timedelta(minutes=i * 2)
        points.append(
            GpsPoint(
                lat=lat,
                lon=lon,
                ts=ts.strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=0.3,
            )
        )
    return points


def _assess_body(order_display_id: str = "ORD-API-1") -> dict:
    return {
        "order_display_id": order_display_id,
        "driver_id": 42,
        "cancel_ts": "2024-01-01 11:20:00",
        "assign_ts": "2024-01-01 10:00:00",
        "latlong": f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        "path_point_num": 2,
        "order_status": "CANCELLED",
        "category": "FOOD",
        "order_value": 800.0,
        "currency": "PHP",
        "next_driver_no_order": True,
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
    )


@pytest.mark.asyncio
async def test_health():
    app = create_app(gps_client=FakeGpsClient([]))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_assess_enqueue_and_latest(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient(_pickup_cluster()),
        settings=_settings(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post("/v1/assess", json=_assess_body())
        assert r.status_code == 200
        body = r.json()
        assert "job_id" in body
        job_id = body["job_id"]

        job = await ac.get(f"/v1/assess/{job_id}")
        assert job.status_code == 200
        assert job.json()["status"] == "done"
        assert job.json()["result"]["order_display_id"] == "ORD-API-1"

        latest = await ac.get("/v1/orders/ORD-API-1/latest")
        assert latest.status_code == 200
        assert latest.json()["order_display_id"] == "ORD-API-1"
        assert latest.json()["assessment_generation"] == 1


@pytest.mark.asyncio
async def test_assess_batch_and_generations(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient(_pickup_cluster()),
        settings=_settings(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/assess:batch",
            json={"orders": [_assess_body("ORD-B1"), _assess_body("ORD-B2")]},
        )
        assert r.status_code == 200
        job_ids = r.json()["job_ids"]
        assert len(job_ids) == 2

        gens = await ac.get("/v1/orders/ORD-B1/generations")
        assert gens.status_code == 200
        rows = gens.json()
        assert len(rows) == 1
        assert rows[0]["order_display_id"] == "ORD-B1"
        assert rows[0]["assessment_generation"] == 1


@pytest.mark.asyncio
async def test_feedback_upsert_stores_sqlite_row(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(gps_client=FakeGpsClient([]), settings=settings)
    labels = {"cancelled_offline": 1, "cancel_abuse": 0}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/feedback",
            json={"order_display_id": "ORD-FB-1", "labels": labels},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    with sqlite3.connect(settings.sqlite_path) as conn:
        row = conn.execute(
            "SELECT order_display_id, labels, created_at FROM feedback WHERE order_display_id = ?",
            ("ORD-FB-1",),
        ).fetchone()
    assert row is not None
    assert row[0] == "ORD-FB-1"
    assert json.loads(row[1]) == labels
    assert row[2]
