from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.api.schemas import (
    AssessmentResult,
    ExpectedRevenueAtRisk,
    ThreeHeadFlags,
    ThreeHeadMlScores,
    ThreeHeadScores,
)
from offline_cancel_risk.feedback.sampler import (
    evaluate_inline_reason,
    run_batch_sample,
    try_inline_sample,
)
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.main import create_app
from offline_cancel_risk.settings import Settings, load_policy


def _policy() -> dict:
    p = load_policy("config/policy.default.yaml")
    p["feedback"] = {
        "daily_review_quota": 10,
        "inline_soft_cap_fraction": 0.6,
        "per_head_min": 2,
        "per_head_max": 8,
        "uncertainty_delta": 0.1,
    }
    return p


def _result(
    oid: str,
    *,
    scores: dict[str, float],
    rule: dict[str, float] | None = None,
    ml: dict[str, float | None] | None = None,
) -> AssessmentResult:
    rule = rule or scores
    ml = ml or {k: None for k in scores}
    return AssessmentResult(
        order_display_id=oid,
        driver_id=1,
        scores=ThreeHeadScores(**scores),
        flags=ThreeHeadFlags(
            cancelled_offline=1 if scores["cancelled_offline"] >= 0.75 else 0,
            cancel_abuse=0,
            selective_theft=0,
        ),
        expected_revenue_at_risk=ExpectedRevenueAtRisk(
            cancelled_offline=0, cancel_abuse=0, selective_theft=0, total=0
        ),
        attention_score=0,
        reasons=[],
        rule_scores=ThreeHeadScores(**rule),
        ml_scores=ThreeHeadMlScores(**ml),
        gps_window={},
        lineage_id="x",
        assessment_generation=1,
        provisional=False,
        policy_hash="p",
        model_version="none",
        twin_version="none",
        graph_version="none",
        feature_vector_ref="x",
        assessed_at="2026-07-26T00:00:00Z",
        region_code="PH",
        city_code="MNL",
    )


def test_uncertainty_reason():
    reason = evaluate_inline_reason(
        scores={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        rule_scores={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        ml_scores={
            "cancelled_offline": None,
            "cancel_abuse": None,
            "selective_theft": None,
        },
        policy=_policy(),
    )
    assert reason is not None
    assert reason[0] == "uncertainty"
    assert reason[1] == "cancelled_offline"


def test_disagreement_beats_uncertainty():
    reason = evaluate_inline_reason(
        scores={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        rule_scores={
            "cancelled_offline": 0.9,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        ml_scores={
            "cancelled_offline": 0.1,
            "cancel_abuse": None,
            "selective_theft": None,
        },
        policy=_policy(),
    )
    assert reason is not None
    assert reason[0] == "disagreement"
    assert reason[1] == "cancelled_offline"


def test_bias_fp_only_on_flagged_orders():
    policy = _policy()
    policy["feedback"]["uncertainty_delta"] = 0.05
    hints = {"cancelled_offline": "bias_fp"}
    flagged = evaluate_inline_reason(
        scores={
            "cancelled_offline": 0.95,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        rule_scores={
            "cancelled_offline": 0.95,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        ml_scores={
            "cancelled_offline": None,
            "cancel_abuse": None,
            "selective_theft": None,
        },
        policy=policy,
        bias_hints=hints,
    )
    assert flagged is not None
    assert flagged[0] == "bias_fp"

    not_flagged = evaluate_inline_reason(
        scores={
            "cancelled_offline": 0.2,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        rule_scores={
            "cancelled_offline": 0.2,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        ml_scores={
            "cancelled_offline": None,
            "cancel_abuse": None,
            "selective_theft": None,
        },
        policy=policy,
        bias_hints=hints,
    )
    assert not_flagged is None


def test_unique_order_day_constraint(tmp_path: Path):
    store = LabelTicketStore(tmp_path / "t.db")
    first = store.create(
        order_display_id="X",
        heads=["cancelled_offline"],
        sampling_reason="coverage",
    )
    second = store.create(
        order_display_id="X",
        heads=["cancel_abuse"],
        sampling_reason="coverage",
    )
    assert first is not None
    assert second is None


def test_inline_respects_soft_cap_and_dedupe(tmp_path: Path):
    store = LabelTicketStore(tmp_path / "t.db", stream_path=tmp_path / "t.jsonl")
    policy = _policy()
    policy["feedback"]["daily_review_quota"] = 5
    policy["feedback"]["inline_soft_cap_fraction"] = 0.4  # soft cap = 2
    scores = {
        "cancelled_offline": 0.75,
        "cancel_abuse": 0.1,
        "selective_theft": 0.1,
    }
    assert try_inline_sample(store, _result("A", scores=scores), policy) is not None
    assert try_inline_sample(store, _result("A", scores=scores), policy) is None
    assert try_inline_sample(store, _result("B", scores=scores), policy) is not None
    # soft cap reached
    assert try_inline_sample(store, _result("C", scores=scores), policy) is None
    assert store.day_count() == 2
    assert (tmp_path / "t.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_batch_fills_quota(tmp_path: Path):
    store = LabelTicketStore(tmp_path / "t.db", stream_path=tmp_path / "t.jsonl")
    policy = _policy()
    assessments = [
        _result(
            f"O{i}",
            scores={
                "cancelled_offline": 0.2,
                "cancel_abuse": 0.2,
                "selective_theft": 0.2,
            },
        )
        for i in range(20)
    ]
    created = run_batch_sample(store, assessments, policy)
    # daily_review_quota ±1
    assert 10 <= len(created) <= 11
    assert store.day_count() == len(created)


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
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
        operating_point_path=str(
            Path("config/operating_point.default.yaml").resolve()
        ),
    )


@pytest.mark.asyncio
async def test_feedback_closes_ticket(tmp_path: Path):
    app = create_app(gps_client=FakeGpsClient([]), settings=_settings(tmp_path))
    store: LabelTicketStore = app.state.tickets
    store.create(
        order_display_id="ORD-1",
        heads=["cancelled_offline"],
        sampling_reason="coverage",
        priority=10,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/feedback",
            json={
                "order_display_id": "ORD-1",
                "labels": {"cancelled_offline": 1},
            },
        )
        assert r.status_code == 200
        assert r.json()["tickets_closed"] == 1
        tickets = await ac.get("/v1/feedback/tickets", params={"status": "labeled"})
        assert tickets.status_code == 200
        assert len(tickets.json()) == 1
