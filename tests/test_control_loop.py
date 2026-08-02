import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.api.schemas import (
    AssessmentResult,
    ExpectedRevenueAtRisk,
    ThreeHeadFlags,
    ThreeHeadMlScores,
    ThreeHeadScores,
)
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.loop import ControlPlaneLoop
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings


def test_clawback_halves_effective_caps(tmp_path: Path):
    hg = EnforcementHardgateStore(tmp_path / "cp.db")
    hg.upsert("PH", "MNL", window="hour", max_enforcements=40, actor="ops")
    assert hg.effective_caps("PH", "MNL")["hour"]["max_enforcements"] == 40
    hg.record_clawback("PH", "MNL", ttl_minutes=30, reason="dip")
    caps = hg.effective_caps("PH", "MNL")
    assert caps["hour"]["max_enforcements"] == 20
    assert caps["hour"]["clawback_scaled"] is True


def _result(oid: str) -> AssessmentResult:
    scores = {
        "cancelled_offline": 0.2,
        "cancel_abuse": 0.2,
        "selective_theft": 0.2,
    }
    return AssessmentResult(
        order_display_id=oid,
        driver_id=1,
        scores=ThreeHeadScores(**scores),
        flags=ThreeHeadFlags(
            cancelled_offline=0, cancel_abuse=0, selective_theft=0
        ),
        expected_revenue_at_risk=ExpectedRevenueAtRisk(
            cancelled_offline=0, cancel_abuse=0, selective_theft=0, total=0
        ),
        attention_score=0,
        reasons=[],
        rule_scores=ThreeHeadScores(**scores),
        ml_scores=ThreeHeadMlScores(
            cancelled_offline=None, cancel_abuse=None, selective_theft=None
        ),
        gps_window={},
        lineage_id="x",
        assessment_generation=1,
        provisional=False,
        policy_hash="p",
        model_version="none",
        twin_version="none",
        graph_version="none",
        feature_vector_ref="x",
        assessed_at="2026-08-03T00:00:00Z",
        region_code="PH",
        city_code="MNL",
    )


@pytest.mark.asyncio
async def test_debounce_flush_runs_cycle(tmp_path: Path):
    settings = Settings(
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
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
        metrics_debounce_seconds=0.05,
        control_plane_tick_seconds=0,
        tuner_min_labeled=1000,  # force reject path, still writes metrics
    )
    app = create_app(gps_client=FakeGpsClient([]), settings=settings)
    table: SqliteTablePublisher = app.state.table
    table.upsert(_result("ORD-DB"))
    table.upsert_feedback(
        "ORD-DB",
        {"cancelled_offline": 0, "cancel_abuse": 0, "selective_theft": 0},
    )

    loop: ControlPlaneLoop = app.state.control_loop
    loop.notify_feedback("ORD-DB")
    await asyncio.sleep(0.15)
    # Force flush in case timing jitter
    await loop.flush_pending(reason="test")

    metrics = app.state.label_metrics.latest(region_code="PH", city_code="MNL")
    assert metrics
    audit = app.state.audit.list_entries(limit=20)
    assert any(a["action"] == "metrics_snapshot" for a in audit)


@pytest.mark.asyncio
async def test_feedback_notifies_control_loop(tmp_path: Path):
    settings = Settings(
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
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
        metrics_debounce_seconds=60,
        control_plane_tick_seconds=0,
    )
    app = create_app(gps_client=FakeGpsClient([]), settings=settings)
    app.state.table.upsert(_result("ORD-FB"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/feedback",
            json={
                "order_display_id": "ORD-FB",
                "labels": {"cancelled_offline": 1},
            },
        )
        assert r.status_code == 200
    assert ("PH", "MNL") in app.state.control_loop._pending
