from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.main import create_app
from offline_cancel_risk.scoring.rules import compute_rule_scores
from offline_cancel_risk.settings import Settings, load_policy

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def test_sequence_weight_increases_offline_score():
    policy = load_policy("config/policy.default.yaml")
    features = {
        "final_stop_confidence": 0.4,
        "sequence_score": 1.0,
        "dwell_fraction": 0.4,
        "has_replacement": True,
        "replacement_valid": True,
        "abuse_score": 0.0,
        "theft_score": 0.0,
        "abuse_reasons": [],
        "theft_reasons": [],
        "replacement_reasons": [],
    }
    low = dict(policy)
    low["sequence"] = {**policy["sequence"], "offline_weight": 0.5}
    high = dict(policy)
    high["sequence"] = {**policy["sequence"], "offline_weight": 2.5}
    s_low, _ = compute_rule_scores(features, low)
    s_high, _ = compute_rule_scores(features, high)
    assert s_high["cancelled_offline"] > s_low["cancelled_offline"]


def test_driver_chain_counts_cross_order(tmp_path: Path):
    store = DriverChainStore(tmp_path / "chains.db")
    base = datetime(2024, 1, 1, 10, 0, 0)
    for i in range(3):
        ts = (base + timedelta(minutes=i * 10)).strftime("%Y-%m-%d %H:%M:%S")
        store.record(driver_id=7, order_display_id=f"O{i}", event_ts=ts)
    n = store.count_recent(
        7,
        as_of="2024-01-01 12:00:00",
        window_minutes=180,
        exclude_order_id="O3",
    )
    assert n == 3


def _cluster(n: int = 40) -> list[GpsPoint]:
    base = datetime(2024, 1, 1, 10, 0, 0)
    points: list[GpsPoint] = []
    for i in range(n):
        points.append(
            GpsPoint(
                lat=PICKUP[0] + (i % 5) * 0.00001,
                lon=PICKUP[1] + (i % 3) * 0.00001,
                ts=(base + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"),
                speed_mps=0.3,
            )
        )
    return points


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "a.db"),
        stream_path=str(tmp_path / "r.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        policy_guardrails_path=str(
            Path("config/policy_guardrails.default.yaml").resolve()
        ),
        policy_overlays_path=str(tmp_path / "o.db"),
        control_plane_sqlite_path=str(tmp_path / "cp.db"),
        label_tickets_path=str(tmp_path / "t.db"),
        label_tickets_stream_path=str(tmp_path / "t.jsonl"),
        driver_chains_path=str(tmp_path / "chains.db"),
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
        metrics_debounce_seconds=0,
        control_plane_tick_seconds=0,
    )


def _body(oid: str = "ORD-RA") -> dict:
    return {
        "order_display_id": oid,
        "driver_id": 42,
        "cancel_ts": "2024-01-01 11:20:00",
        "assign_ts": "2024-01-01 10:00:00",
        "latlong": f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        "path_point_num": 2,
        "order_status": "CANCELLED",
        "category": "FOOD",
        "order_value": 800.0,
        "currency": "PHP",
        "region_code": "PH",
        "city_code": "MNL",
    }


@pytest.mark.asyncio
async def test_force_reassess_bumps_generation(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient(_cluster()),
        settings=_settings(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r1 = await ac.post("/v1/assess", json=_body())
        job1 = await ac.get(f"/v1/assess/{r1.json()['job_id']}")
        assert job1.json()["result"]["assessment_generation"] == 1

        body2 = _body()
        body2["force_reassess"] = True
        body2["next_driver_no_order"] = True
        r2 = await ac.post("/v1/assess", json=body2)
        job2 = await ac.get(f"/v1/assess/{r2.json()['job_id']}")
        assert job2.json()["result"]["assessment_generation"] == 2

        gens = await ac.get("/v1/orders/ORD-RA/generations")
        assert len(gens.json()) == 2
        assert gens.json()[0]["assessment_generation"] == 1
        assert gens.json()[0]["provisional"] is True  # marked on reassess
        assert gens.json()[1]["assessment_generation"] == 2
