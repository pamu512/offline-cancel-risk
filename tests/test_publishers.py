import json
import sqlite3

import pytest

from offline_cancel_risk.api.schemas import AssessmentResult
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher


def _sample_result(**overrides) -> AssessmentResult:
    base = dict(
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
        policy_hash="abc123",
        model_version="none",
        twin_version="none",
        graph_version="lineage-v0",
        feature_vector_ref="mem:1",
        assessed_at="2024-01-01T13:00:00Z",
    )
    base.update(overrides)
    return AssessmentResult(**base)


def test_stream_publish_appends_jsonl_line(tmp_path):
    stream_path = tmp_path / "data" / "risk_events.jsonl"
    publisher = JsonlStreamPublisher(stream_path=str(stream_path))
    result = _sample_result()

    publisher.publish(result)

    assert stream_path.is_file()
    lines = stream_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["order_display_id"] == "ORD1"
    assert payload["policy_hash"] == "abc123"
    assert payload["assessment_generation"] == 1


def test_sqlite_upsert_get_and_ledger_row(tmp_path):
    db_path = tmp_path / "assessments.db"
    publisher = SqliteTablePublisher(sqlite_path=str(db_path))
    result = _sample_result()

    publisher.upsert(result)
    got = publisher.get("ORD1", "abc123", "none", 1)

    assert got is not None
    assert got.order_display_id == result.order_display_id
    assert got.assessment_generation == 1
    assert got.scores.selective_theft == pytest.approx(0.3)

    with sqlite3.connect(db_path) as conn:
        assessment_rows = conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
        ledger_rows = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert assessment_rows == 1
    assert ledger_rows == 1


def test_sqlite_upsert_same_key_keeps_one_assessment_appends_ledger(tmp_path):
    db_path = tmp_path / "assessments.db"
    publisher = SqliteTablePublisher(sqlite_path=str(db_path))
    first = _sample_result(attention_score=40.0)
    second = _sample_result(attention_score=55.0, assessed_at="2024-01-01T14:00:00Z")

    publisher.upsert(first)
    publisher.upsert(second)

    got = publisher.get("ORD1", "abc123", "none", 1)
    assert got is not None
    assert got.attention_score == pytest.approx(55.0)

    with sqlite3.connect(db_path) as conn:
        assessment_rows = conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
        ledger_rows = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert assessment_rows == 1
    assert ledger_rows == 2
