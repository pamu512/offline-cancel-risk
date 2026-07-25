import pytest
from pydantic import ValidationError

from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult


def test_assess_request_minimal():
    req = AssessRequest(
        order_display_id="ORD1",
        driver_id=1,
        cancel_ts="2024-01-01T12:00:00Z",
        assign_ts="2024-01-01T09:00:00Z",
        latlong="1.0|2.0,1.1|2.1",
        path_point_num=1,
        order_status="CANCELLED",
        category="FOOD",
        order_value=100.0,
        currency="SGD",
    )
    assert req.order_display_id == "ORD1"


def test_assessment_result_has_three_heads():
    result = AssessmentResult(
        order_display_id="ORD1",
        driver_id=1,
        scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2, "selective_theft": 0.3},
        flags={"cancelled_offline": 0, "cancel_abuse": 0, "selective_theft": 0},
        expected_revenue_at_risk={
            "cancelled_offline": 10.0,
            "cancel_abuse": 8.0,
            "selective_theft": 24.0,
            "total": 42.0,
        },
        attention_score=42.0,
        reasons=["gps_sparse"],
        rule_scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2, "selective_theft": 0.3},
        ml_scores={"cancelled_offline": None, "cancel_abuse": None, "selective_theft": None},
        gps_window={"start": "x", "end": "y", "expanded": False, "point_count": 0, "max_gap_minutes": 0},
        lineage_id="LIN1",
        assessment_generation=1,
        provisional=True,
        policy_hash="abc",
        model_version="none",
        twin_version="none",
        graph_version="none",
        feature_vector_ref="mem:1",
        assessed_at="2024-01-01T13:00:00Z",
    )
    assert set(result.scores.model_dump()) == {
        "cancelled_offline",
        "cancel_abuse",
        "selective_theft",
    }


def test_assessment_result_missing_head_fails_validation():
    with pytest.raises(ValidationError):
        AssessmentResult(
            order_display_id="ORD1",
            driver_id=1,
            scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2},
            flags={"cancelled_offline": 0, "cancel_abuse": 0, "selective_theft": 0},
            expected_revenue_at_risk={
                "cancelled_offline": 10.0,
                "cancel_abuse": 8.0,
                "selective_theft": 24.0,
                "total": 42.0,
            },
            attention_score=42.0,
            reasons=["gps_sparse"],
            rule_scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2, "selective_theft": 0.3},
            ml_scores={"cancelled_offline": None, "cancel_abuse": None, "selective_theft": None},
            gps_window={"start": "x", "end": "y", "expanded": False, "point_count": 0, "max_gap_minutes": 0},
            lineage_id="LIN1",
            assessment_generation=1,
            provisional=True,
            policy_hash="abc",
            model_version="none",
            twin_version="none",
            graph_version="none",
            feature_vector_ref="mem:1",
            assessed_at="2024-01-01T13:00:00Z",
        )
