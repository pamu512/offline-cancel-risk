from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.main import create_app
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.resolve import GuardrailError, resolve_policy, validate_overlay
from offline_cancel_risk.policy.routing import build_routing
from offline_cancel_risk.settings import Settings, load_policy

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def _guardrails() -> dict:
    return load_policy(Path("config/policy_guardrails.default.yaml").resolve())


def _base_policy() -> dict:
    return load_policy(Path("config/policy.default.yaml").resolve())


def test_validate_overlay_rejects_out_of_bounds():
    with pytest.raises(GuardrailError, match="outside guardrail"):
        validate_overlay(
            {"thresholds": {"cancelled_offline": 0.99}},
            _guardrails(),
        )


def test_validate_overlay_rejects_unknown_param():
    with pytest.raises(GuardrailError, match="not tunable"):
        validate_overlay({"thresholds": {"unknown_head": 0.8}}, _guardrails())


def test_resolve_city_wins_over_region():
    base = _base_policy()
    resolved = resolve_policy(
        base,
        region_overlay={"thresholds": {"cancelled_offline": 0.8}},
        city_overlay={"thresholds": {"cancelled_offline": 0.7}},
    )
    assert resolved["thresholds"]["cancelled_offline"] == 0.7


def test_routing_priority_from_attention():
    policy = _base_policy()
    r = build_routing(
        flags={
            "cancelled_offline": 1,
            "cancel_abuse": 0,
            "selective_theft": 0,
        },
        attention_score=250.0,
        policy=policy,
    )
    assert r["priority"] == "P1"
    assert r["queue"] == "offline"


def test_overlay_store_roundtrip(tmp_path: Path):
    store = PolicyOverlayStore(tmp_path / "overlays.db")
    store.upsert("PH", "MNL", {"thresholds": {"cancelled_offline": 0.8}})
    assert store.get("ph", "mnl")["thresholds"]["cancelled_offline"] == 0.8
    assert store.get("PH", "") is None
    store.upsert("PH", "", {"thresholds": {"cancel_abuse": 0.7}})
    assert store.get("PH", "")["thresholds"]["cancel_abuse"] == 0.7


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        policy_guardrails_path=str(
            Path("config/policy_guardrails.default.yaml").resolve()
        ),
        policy_overlays_path=str(tmp_path / "policy_overlays.db"),
    )


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


@pytest.mark.asyncio
async def test_assess_applies_city_overlay_and_routing(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient(_pickup_cluster()),
        settings=_settings(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        await ac.put(
            "/v1/policy/overlays",
            json={
                "region_code": "PH",
                "city_code": "MNL",
                "overlay": {"routing": {"p1_attention_min": 1, "p2_attention_min": 0}},
            },
        )
        r = await ac.post(
            "/v1/assess",
            json={
                "order_display_id": "ORD-MKT-1",
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
            },
        )
        assert r.status_code == 200
        job = await ac.get(f"/v1/assess/{r.json()['job_id']}")
        result = job.json()["result"]
        assert result["region_code"] == "PH"
        assert result["city_code"] == "MNL"
        assert "priority" in result["routing"]
        assert "queue" in result["routing"]


@pytest.mark.asyncio
async def test_ingest_overlay_within_guardrails(tmp_path: Path):
    app = create_app(gps_client=FakeGpsClient([]), settings=_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.put(
            "/v1/policy/overlays",
            json={
                "region_code": "PH",
                "city_code": "MNL",
                "overlay": {
                    "thresholds": {"cancelled_offline": 0.82},
                    "routing": {"p1_attention_min": 150},
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["region_code"] == "PH"
        assert body["city_code"] == "MNL"
        assert body["overlay"]["thresholds"]["cancelled_offline"] == 0.82

        bad = await ac.put(
            "/v1/policy/overlays",
            json={
                "region_code": "PH",
                "city_code": "MNL",
                "overlay": {"thresholds": {"cancelled_offline": 0.99}},
            },
        )
        assert bad.status_code == 400

        g = await ac.get("/v1/policy/guardrails")
        assert g.status_code == 200
        assert "bounds" in g.json()

        resolved = await ac.get(
            "/v1/policy/resolved",
            params={"region_code": "PH", "city_code": "MNL"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["thresholds"]["cancelled_offline"] == 0.82
        assert resolved.json()["routing"]["p1_attention_min"] == 150
