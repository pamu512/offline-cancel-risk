"""A++ ops: prod auth profile, sqlite assess queue, control-plane leader lock."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.queue_factory import make_job_queue
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.control_plane.leader import FileLeaderLock
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings, apply_profile
from offline_cancel_risk.worker.sqlite_queue import SqliteAssessJobQueue

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        control_plane_sqlite_path=str(tmp_path / "control_plane.db"),
        assess_queue_path=str(tmp_path / "assess_queue.db"),
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
    )
    base.update(kwargs)
    return Settings(**base)


def test_prod_profile_requires_api_keys():
    with pytest.raises(RuntimeError, match="OCR_API_KEYS"):
        apply_profile(Settings(profile="prod", api_keys=""))


def test_prod_profile_forces_auth_required():
    s = apply_profile(Settings(profile="prod", api_keys="k1", auth_required=False))
    assert s.auth_required is True


def test_demo_profile_leaves_auth_off():
    s = apply_profile(Settings(profile="demo", auth_required=False, api_keys=""))
    assert s.auth_required is False


@pytest.mark.asyncio
async def test_assess_requires_auth_when_enabled(tmp_path: Path):
    settings = _settings(tmp_path, auth_required=True, api_keys="secret")
    app = create_app(gps_client=FakeGpsClient([]), settings=settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        body = {
            "order_display_id": "ORD-AUTH",
            "driver_id": 1,
            "cancel_ts": "2024-01-01 11:20:00",
            "assign_ts": "2024-01-01 10:00:00",
            "latlong": f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
            "path_point_num": 2,
            "order_status": "CANCELLED",
            "category": "FOOD",
            "order_value": 100.0,
            "currency": "PHP",
        }
        denied = await ac.post("/v1/assess", json=body)
        assert denied.status_code == 401
        ok = await ac.post(
            "/v1/assess",
            headers={"x-api-key": "secret"},
            json=body,
        )
        assert ok.status_code == 200, ok.text


def test_queue_factory_sqlite(tmp_path: Path):
    q = make_job_queue(_settings(tmp_path, queue_backend="sqlite"))
    assert isinstance(q, SqliteAssessJobQueue)


@pytest.mark.asyncio
async def test_sqlite_queue_claim_and_run(tmp_path: Path):
    from offline_cancel_risk.adapters.publishers import (
        JsonlStreamPublisher,
        SqliteTablePublisher,
    )
    from offline_cancel_risk.settings import load_policy

    settings = _settings(tmp_path, queue_backend="sqlite", sync_assess=False)
    queue = make_job_queue(settings)
    assert isinstance(queue, SqliteAssessJobQueue)
    req = AssessRequest(
        order_display_id="ORD-Q1",
        driver_id=1,
        cancel_ts="2024-01-01 11:20:00",
        assign_ts="2024-01-01 10:00:00",
        latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        path_point_num=2,
        order_status="CANCELLED",
        category="FOOD",
        order_value=100.0,
        currency="PHP",
    )
    job_id = queue.create_job(req)
    claimed = queue._claim_next()
    assert claimed == job_id
    assert queue._claim_next() is None

    points = []
    base = datetime(2024, 1, 1, 10, 0, 0)
    for i in range(10):
        points.append(
            GpsPoint(
                lat=PICKUP[0],
                lon=PICKUP[1],
                ts=(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=0.2,
            )
        )
    job = await queue.run_one(
        job_id,
        gps_client=FakeGpsClient(points),
        policy=load_policy(settings.policy_path),
        stream=JsonlStreamPublisher(stream_path=settings.stream_path),
        table=SqliteTablePublisher(sqlite_path=settings.sqlite_path),
    )
    assert job.status == "done"
    assert job.result is not None


def test_file_leader_lock_exclusive(tmp_path: Path):
    path = tmp_path / "leader.lock"
    a = FileLeaderLock(path)
    b = FileLeaderLock(path)
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    a.release()
    assert b.try_acquire() is True
    b.release()
