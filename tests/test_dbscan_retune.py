"""Tests for assess GPS cache + DBSCAN market retune."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.dbscan_retune import (
    DbscanRetuneContext,
    DbscanRetuneStore,
    run_dbscan_retune,
)
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.gps_cache import AssessGpsCache
from offline_cancel_risk.main import create_app
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.settings import Settings, load_policy
from fastapi.testclient import TestClient


def _req(oid: str) -> dict[str, Any]:
    return {
        "order_display_id": oid,
        "driver_id": 7,
        "cancel_ts": "2024-01-01 11:20:00",
        "assign_ts": "2024-01-01 10:00:00",
        "latlong": "14.55|121.03,14.56|121.04",
        "path_point_num": 2,
        "order_status": "CANCELLED",
        "category": "HAUL",
        "order_value": 50.0,
        "currency": "PHP",
        "region_code": "PH",
        "city_code": "MNL",
    }


def _points(n: int = 5) -> list[GpsPoint]:
    base = datetime(2024, 1, 1, 10, 0, 0)
    return [
        GpsPoint(
            lat=14.55 + i * 1e-5,
            lon=121.03,
            ts=(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
            speed_mps=0.2,
        )
        for i in range(n)
    ]


def test_gps_cache_put_get_and_skip_empty(tmp_path: Path):
    cache = AssessGpsCache(tmp_path / "gps.db")
    assert cache.put(
        order_display_id="A1",
        assessment_generation=1,
        region_code="PH",
        city_code="MNL",
        request=_req("A1"),
        points=_points(),
    )
    got = cache.get("A1", 1)
    assert got is not None
    assert got["region_code"] == "PH"
    assert len(got["points"]) == 5
    assert cache.put(
        order_display_id="A2",
        assessment_generation=1,
        region_code="PH",
        city_code="MNL",
        request=_req("A2"),
        points=[],
    ) is False
    assert cache.get("A2", 1) is None


def test_gps_cache_prune_and_latest(tmp_path: Path):
    cache = AssessGpsCache(tmp_path / "gps.db")
    cache.put(
        order_display_id="A1",
        assessment_generation=1,
        region_code="PH",
        city_code="MNL",
        request=_req("A1"),
        points=_points(),
    )
    cache.put(
        order_display_id="A1",
        assessment_generation=2,
        region_code="PH",
        city_code="MNL",
        request=_req("A1"),
        points=_points(3),
    )
    latest = cache.latest_for_market("PH", "MNL")
    assert len(latest) == 1
    assert latest[0]["assessment_generation"] == 2
    assert len(latest[0]["points"]) == 3
    # Force old timestamp then prune
    with cache._connect() as conn:
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace(
            "+00:00", "Z"
        )
        conn.execute(
            "UPDATE assess_gps_cache SET recorded_at=? WHERE order_display_id=?",
            (old, "A1"),
        )
        conn.commit()
    assert cache.prune(30) >= 1
    assert cache.latest_for_market("PH", "MNL") == []


def _policy_for_retune() -> dict[str, Any]:
    policy = load_policy("config/policy.default.yaml")
    policy["learning"] = dict(policy.get("learning") or {})
    policy["learning"]["min_pattern_support"] = 5
    policy["learning"]["target_precision"] = 0.9
    policy["learning"]["min_pattern_recall"] = 0.3
    policy["learning"]["pattern_strata"] = {
        "cancelled_offline": {"score_min": 0.5},
        "cancel_abuse": {"score_min": 0.5},
        "selective_theft": {"score_min": 0.5},
    }
    policy["dbscan_retune"] = {
        "mode": "shadow",
        "cooldown_minutes": 0,
        "min_labeled": 5,
        "min_recall_lift": 0.01,
        "cache_retention_days": 30,
        "holdout_fraction": 0.3,
        "grid": {
            "clustering_radius_m": [50, 80],
            "min_pts": [5, 7],
        },
    }
    policy["dbscan"] = dict(policy["dbscan"])
    policy["dbscan"]["clustering_radius_m"] = 50
    policy["dbscan"]["min_pts"] = 7
    policy["thresholds"] = dict(policy["thresholds"])
    policy["thresholds"]["cancelled_offline"] = 0.8
    return policy


def _seed_labeled_cache(cache: AssessGpsCache, n_pos: int = 10, n_neg: int = 10):
    feedback = []
    for i in range(n_pos):
        oid = f"P{i}"
        cache.put(
            order_display_id=oid,
            assessment_generation=1,
            region_code="PH",
            city_code="MNL",
            request=_req(oid),
            points=_points(),
        )
        feedback.append(
            {
                "order_display_id": oid,
                "labels": {
                    "cancelled_offline": 1,
                    "cancel_abuse": 0,
                    "selective_theft": 0,
                },
            }
        )
    for i in range(n_neg):
        oid = f"N{i}"
        cache.put(
            order_display_id=oid,
            assessment_generation=1,
            region_code="PH",
            city_code="MNL",
            request=_req(oid),
            points=_points(),
        )
        feedback.append(
            {
                "order_display_id": oid,
                "labels": {
                    "cancelled_offline": 0,
                    "cancel_abuse": 0,
                    "selective_theft": 0,
                },
            }
        )
    return feedback


async def _fake_replay(rows: list[dict[str, Any]], policy: dict[str, Any]):
    eps = float(policy["dbscan"]["clustering_radius_m"])
    min_pts = int(policy["dbscan"]["min_pts"])
    good = abs(eps - 80.0) < 1e-9 and min_pts == 5
    out = []
    for row in rows:
        oid = row["order_display_id"]
        is_pos = oid.startswith("P")
        if good:
            offline = 0.95 if is_pos else 0.05
        else:
            # Current/default: positives below threshold → low recall
            offline = 0.55 if is_pos else 0.05
        scores = {
            "cancelled_offline": offline,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        }
        out.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": scores,
                "scores_raw": scores,
                "rule_scores": scores,
                "ml_scores": {
                    "cancelled_offline": None,
                    "cancel_abuse": None,
                    "selective_theft": None,
                },
                "flags": {
                    "cancelled_offline": int(offline >= 0.8),
                    "cancel_abuse": 0,
                    "selective_theft": 0,
                },
                "reasons": [],
            }
        )
    return out


def _ctx(
    tmp_path: Path,
    *,
    feedback: list[dict[str, Any]],
    mode: str = "shadow",
    cooldown_minutes: int = 0,
) -> DbscanRetuneContext:
    db = tmp_path / "cp.db"
    cache = AssessGpsCache(tmp_path / "gps.db")
    _seed_labeled_cache(cache)
    policy = _policy_for_retune()
    policy["dbscan_retune"]["mode"] = mode
    policy["dbscan_retune"]["cooldown_minutes"] = cooldown_minutes
    hardgates = EnforcementHardgateStore(db)
    hardgates.upsert(
        "PH", "MNL", window="hour", max_enforcements=10_000, actor="test"
    )
    return DbscanRetuneContext(
        base_policy=policy,
        guardrails=load_policy("config/policy_guardrails.default.yaml"),
        overlays=PolicyOverlayStore(tmp_path / "overlays.db"),
        audit=PolicyAuditLog(db),
        hardgates=hardgates,
        gps_cache=cache,
        feedback=feedback,
        region_code="PH",
        city_code="MNL",
        run_store=DbscanRetuneStore(db),
    )


def test_retune_picks_better_params_shadow(tmp_path: Path):
    feedback = _seed_labeled_cache(AssessGpsCache(tmp_path / "unused.db"))
    # re-seed via ctx
    ctx = _ctx(tmp_path, feedback=feedback, mode="shadow")
    with patch(
        "offline_cancel_risk.control_plane.dbscan_retune._replay_assessments",
        new=AsyncMock(side_effect=_fake_replay),
    ):
        report = run_dbscan_retune(ctx)
    assert report["decision"] == "shadow"
    assert report["suggested"]["dbscan"]["clustering_radius_m"] == 80
    assert report["suggested"]["dbscan"]["min_pts"] == 5
    assert ctx.overlays.get("PH", "MNL") is None


def test_retune_apply_writes_overlay(tmp_path: Path):
    feedback = _seed_labeled_cache(AssessGpsCache(tmp_path / "unused.db"))
    ctx = _ctx(tmp_path, feedback=feedback, mode="apply")
    # Preserve existing threshold overlay across dbscan apply
    ctx.overlays.upsert(
        "PH", "MNL", {"thresholds": {"cancelled_offline": 0.82}}
    )
    with patch(
        "offline_cancel_risk.control_plane.dbscan_retune._replay_assessments",
        new=AsyncMock(side_effect=_fake_replay),
    ):
        report = run_dbscan_retune(ctx)
    assert report["decision"] == "applied"
    stored = ctx.overlays.get("PH", "MNL")
    assert stored is not None
    assert stored["dbscan"]["clustering_radius_m"] == 80
    assert stored["dbscan"]["min_pts"] == 5
    assert stored["thresholds"]["cancelled_offline"] == 0.82


def test_retune_rejects_insufficient_labels(tmp_path: Path):
    ctx = _ctx(tmp_path, feedback=[], mode="shadow")
    report = run_dbscan_retune(ctx)
    assert report["decision"] == "rejected"
    assert report["reason"] == "insufficient_labeled_cache"


def test_retune_respects_cooldown(tmp_path: Path):
    feedback = _seed_labeled_cache(AssessGpsCache(tmp_path / "unused.db"))
    ctx = _ctx(tmp_path, feedback=feedback, mode="apply", cooldown_minutes=1440)
    ctx.audit.append(
        actor="dbscan_retuner",
        action="apply",
        region_code="PH",
        city_code="MNL",
        decision="accepted",
        reason="prior",
        after={"dbscan": {"clustering_radius_m": 40, "min_pts": 9}},
    )
    with patch(
        "offline_cancel_risk.control_plane.dbscan_retune._replay_assessments",
        new=AsyncMock(side_effect=_fake_replay),
    ):
        report = run_dbscan_retune(ctx)
    assert report["decision"] == "rejected"
    assert report["reason"] == "cooldown"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sqlite_path=str(tmp_path / "assess.db"),
        stream_path=str(tmp_path / "stream.jsonl"),
        policy_overlays_path=str(tmp_path / "overlays.db"),
        control_plane_sqlite_path=str(tmp_path / "cp.db"),
        assess_gps_cache_path=str(tmp_path / "gps_cache.db"),
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
        driver_chains_path=str(tmp_path / "chains.db"),
        entity_baselines_path=str(tmp_path / "baselines.db"),
        entity_cancel_stats_path=str(tmp_path / "cancel_stats.db"),
        device_integrity_path=str(tmp_path / "devices.db"),
        device_graph_path=str(tmp_path / "device_graph.db"),
        chat_signals_path=str(tmp_path / "chat.db"),
        entity_anomaly_path=str(tmp_path / "anomaly.db"),
        outcomes_path=str(tmp_path / "outcomes.db"),
        models_sqlite_path=str(tmp_path / "models.db"),
        models_root=str(tmp_path / "model_files"),
        shadow_metrics_path=str(tmp_path / "shadow.db"),
        canary_sqlite_path=str(tmp_path / "canary.db"),
        sync_assess=True,
        control_plane_tick_seconds=0,
    )


def test_assess_writes_gps_cache(tmp_path: Path):
    import asyncio

    cache = AssessGpsCache(tmp_path / "gps.db")
    policy = load_policy("config/policy.default.yaml")
    req = AssessRequest.model_validate(_req("CACHE1"))
    base = datetime(2024, 1, 1, 10, 0, 0)
    points = [
        GpsPoint(
            lat=14.55 + (i % 5) * 1e-5,
            lon=121.03 + (i % 3) * 1e-5,
            ts=(base + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"),
            speed_mps=0.3,
        )
        for i in range(40)
    ]
    asyncio.run(
        assess_order(
            req,
            FakeGpsClient(points),
            policy,
            stream=JsonlStreamPublisher(stream_path=str(tmp_path / "s.jsonl")),
            table=SqliteTablePublisher(sqlite_path=str(tmp_path / "t.db")),
            gps_cache=cache,
        )
    )
    latest = cache.latest_for_market("PH", "MNL")
    assert len(latest) == 1
    assert latest[0]["order_display_id"] == "CACHE1"
    assert len(latest[0]["points"]) > 0


def test_dbscan_retune_api(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient([]),
        settings=_settings(tmp_path),
    )
    client = TestClient(app)
    # Empty cache → reject
    r = client.post(
        "/v1/tuning/dbscan-retune",
        json={"region_code": "PH", "city_code": "MNL", "mode": "shadow"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "rejected"
    assert body["reason"] == "insufficient_labeled_cache"
    latest = client.get(
        "/v1/tuning/dbscan-retune/latest",
        params={"region_code": "PH", "city_code": "MNL"},
    )
    assert latest.status_code == 200
    assert latest.json()["reason"] == "insufficient_labeled_cache"
