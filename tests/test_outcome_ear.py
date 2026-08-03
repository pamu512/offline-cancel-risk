from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.main import create_app
from offline_cancel_risk.outcomes.ewma import ewma_update, signal_for_outcome
from offline_cancel_risk.outcomes.store import OutcomeStore
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.scoring.ear import resolve_recoverability
from offline_cancel_risk.settings import Settings, load_policy

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


def _pickup_cluster(n: int = 40) -> list[GpsPoint]:
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


def _theft_req(order_id: str) -> AssessRequest:
    return AssessRequest(
        order_display_id=order_id,
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
        region_code="PH",
        city_code="MNL",
    )


def _cold_start(policy: dict) -> dict[str, float]:
    return dict(policy["ear"]["recoverability"])


def _guardrails() -> dict:
    return {
        "ear.recoverability.cancelled_offline": {"min": 0.0, "max": 1.0},
        "ear.recoverability.cancel_abuse": {"min": 0.0, "max": 1.0},
        "ear.recoverability.selective_theft": {"min": 0.0, "max": 1.0},
    }


def test_signal_and_ewma():
    assert signal_for_outcome("clawback_won") == 1.0
    assert signal_for_outcome("clawback_lost") == 0.0
    assert abs(ewma_update(0.8, 0.0, 0.05) - 0.76) < 1e-9


def test_store_persist_and_idempotent(tmp_path):
    store = OutcomeStore(tmp_path / "o.db")
    cold = {"cancelled_offline": 1.0, "cancel_abuse": 0.4, "selective_theft": 0.8}
    guard = _guardrails()
    r1 = store.record_outcome(
        order_display_id="O1",
        outcome="clawback_won",
        head="selective_theft",
        region_code="PH",
        city_code="MNL",
        alpha=0.05,
        cold_start=cold,
        guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    r2 = store.record_outcome(
        order_display_id="O1",
        outcome="clawback_won",
        head="selective_theft",
        region_code="PH",
        city_code="MNL",
        alpha=0.05,
        cold_start=cold,
        guardrails=guard,
        occurred_at="2024-01-01T00:00:00Z",
    )
    assert r1["n_updates"] == 1
    assert r2.get("duplicate") is True or r2["n_updates"] == 1
    store2 = OutcomeStore(tmp_path / "o.db")
    got = store2.get_recoverability("PH", "MNL")
    assert got["selective_theft"]["n_updates"] == 1


def test_resolve_recoverability_shadow_uses_static():
    policy = load_policy(Path("config/policy.default.yaml"))
    learned = {
        "selective_theft": {"value": 0.95, "n_updates": 10, "updated_at": "x"},
    }
    live, meta = resolve_recoverability(policy, learned)
    static = policy["ear"]["recoverability"]
    assert live == {k: float(v) for k, v in static.items()}
    assert meta["mode"] == "shadow"
    assert meta["recoverability_learned"]["selective_theft"] == 0.95


@pytest.mark.parametrize("mode", ["off", "bogus"])
def test_resolve_recoverability_non_apply_modes_use_static(mode):
    policy = load_policy(Path("config/policy.default.yaml"))
    policy = {**policy, "ear": {**policy["ear"], "mode": mode}}
    learned = {
        "selective_theft": {"value": 0.95, "n_updates": 10, "updated_at": "x"},
    }
    live, meta = resolve_recoverability(policy, learned)
    static = {k: float(v) for k, v in policy["ear"]["recoverability"].items()}
    assert live == static
    assert meta["mode"] == mode
    assert meta["recoverability_learned"]["selective_theft"] == 0.95


@pytest.mark.asyncio
async def test_shadow_ear_matches_static(tmp_path):
    policy = load_policy(Path("config/policy.default.yaml"))
    gps = FakeGpsClient(_pickup_cluster())
    stream = JsonlStreamPublisher(stream_path=str(tmp_path / "s.jsonl"))
    table = SqliteTablePublisher(sqlite_path=str(tmp_path / "a.db"))

    without_store = await assess_order(
        _theft_req("SHADOW-EAR-A"), gps, policy, stream=stream, table=table
    )
    store = OutcomeStore(tmp_path / "o.db")
    with_store = await assess_order(
        _theft_req("SHADOW-EAR-B"),
        gps,
        policy,
        stream=stream,
        table=table,
        outcomes=store,
    )

    assert (
        with_store.expected_revenue_at_risk.model_dump()
        == without_store.expected_revenue_at_risk.model_dump()
    )
    assert with_store.attention_score == without_store.attention_score
    assert with_store.ear_meta["mode"] == "shadow"
    assert "ear_learned" in with_store.ear_meta
    assert "attention_learned" in with_store.ear_meta


@pytest.mark.asyncio
async def test_apply_shifts_ear_after_enough_won_outcomes(tmp_path):
    policy = load_policy(Path("config/policy.default.yaml"))
    policy = {**policy, "ear": {**policy["ear"], "mode": "apply"}}
    cold = _cold_start(policy)
    guard = _guardrails()
    alpha = float(policy["ear"]["outcome_ewma_alpha"])
    min_apply = int(policy["ear"]["min_updates_apply"])

    store = OutcomeStore(tmp_path / "o.db")
    for i in range(min_apply):
        store.record_outcome(
            order_display_id=f"O-{i}",
            outcome="clawback_won",
            head="selective_theft",
            region_code="PH",
            city_code="MNL",
            alpha=alpha,
            cold_start=cold,
            guardrails=guard,
            occurred_at=f"2024-01-0{i + 1}T00:00:00Z",
        )

    req = _theft_req("APPLY-EAR-1")
    gps = FakeGpsClient(_pickup_cluster())
    stream = JsonlStreamPublisher(stream_path=str(tmp_path / "s.jsonl"))
    table = SqliteTablePublisher(sqlite_path=str(tmp_path / "a.db"))

    static_policy = {**policy, "ear": {**policy["ear"], "mode": "shadow"}}
    static_result = await assess_order(
        req, gps, static_policy, stream=stream, table=table, outcomes=store
    )
    apply_result = await assess_order(
        req,
        gps,
        policy,
        stream=stream,
        table=table,
        outcomes=store,
        generation=2,
    )

    learned_rec = store.get_recoverability("PH", "MNL")["selective_theft"]["value"]
    assert learned_rec > float(cold["selective_theft"])
    assert apply_result.ear_meta["mode"] == "apply"
    assert (
        apply_result.expected_revenue_at_risk.selective_theft
        > static_result.expected_revenue_at_risk.selective_theft
    )
    assert apply_result.attention_score > static_result.attention_score


def _api_settings(tmp_path: Path) -> Settings:
    return Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "assessments.db"),
        stream_path=str(tmp_path / "risk_events.jsonl"),
        outcomes_path=str(tmp_path / "outcomes.db"),
        policy_path=str(Path("config/policy.default.yaml").resolve()),
        policy_guardrails_path=str(
            Path("config/policy_guardrails.default.yaml").resolve()
        ),
    )


@pytest.mark.asyncio
async def test_outcomes_api_ingest_and_recoverability(tmp_path: Path):
    order_id = "API-OUTCOME-1"
    app = create_app(
        gps_client=FakeGpsClient(_pickup_cluster()),
        settings=_api_settings(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        assess = await ac.post("/v1/assess", json=_theft_req(order_id).model_dump())
        assert assess.status_code == 200

        ingest = await ac.post(
            "/v1/outcomes",
            json={
                "order_display_id": order_id,
                "outcome": "clawback_won",
                "occurred_at": "2024-01-02T00:00:00Z",
            },
        )
        assert ingest.status_code == 200
        body = ingest.json()
        assert body["ok"] is True
        assert body["head"] == "selective_theft"
        assert body["region_code"] == "PH"
        assert body["city_code"] == "MNL"
        assert body["n_updates"] == 1

        rec = await ac.get(
            "/v1/outcomes/recoverability",
            params={"region_code": "PH", "city_code": "MNL"},
        )
        assert rec.status_code == 200
        heads = rec.json()["heads"]
        assert heads["selective_theft"]["n_updates"] == 1

        listed = await ac.get(
            "/v1/outcomes",
            params={"order_display_id": order_id},
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "clawback_won"
        assert rows[0]["head"] == "selective_theft"
