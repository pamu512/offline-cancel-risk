"""AssessmentStore port contract (SQLite always; Postgres if OCR_DATABASE_URL)."""

from __future__ import annotations

import os

import pytest

from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.adapters.store_factory import make_assessment_store
from offline_cancel_risk.api.schemas import AssessmentResult
from offline_cancel_risk.ports import AssessmentStore
from offline_cancel_risk.settings import Settings


def _sample_result(**overrides) -> AssessmentResult:
    base = dict(
        order_display_id="ORD-PORT-1",
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
        rule_scores={
            "cancelled_offline": 0.1,
            "cancel_abuse": 0.2,
            "selective_theft": 0.3,
        },
        ml_scores={
            "cancelled_offline": None,
            "cancel_abuse": None,
            "selective_theft": None,
        },
        gps_window={
            "start": "x",
            "end": "y",
            "expanded": False,
            "point_count": 0,
            "max_gap_minutes": 0,
        },
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


def _exercise_store(store: AssessmentStore, *, order_id: str = "ORD-PORT-1") -> None:
    first = _sample_result(order_display_id=order_id, attention_score=40.0)
    store.upsert(first)
    got = store.get(order_id, "abc123", "none", 1)
    assert got is not None
    assert got.attention_score == pytest.approx(40.0)
    assert store.next_generation(order_id) == 2

    second = _sample_result(
        order_display_id=order_id, assessment_generation=2, attention_score=55.0
    )
    store.upsert(second)
    latest = store.latest(order_id)
    assert latest is not None
    assert latest.assessment_generation == 2
    assert len(store.list_generations(order_id)) == 2

    store.mark_prior_provisional(order_id, before_generation=2)
    prior = store.get(order_id, "abc123", "none", 1)
    assert prior is not None and prior.provisional is True

    store.upsert_feedback(order_id, {"selective_theft": 1})
    fb = store.list_feedback()
    assert any(r["order_display_id"] == order_id for r in fb)
    assert any(r.order_display_id == order_id for r in store.list_latest_assessments())


def test_sqlite_satisfies_assessment_store_port(tmp_path):
    store: AssessmentStore = SqliteTablePublisher(sqlite_path=tmp_path / "a.db")
    _exercise_store(store)


def test_factory_defaults_to_sqlite(tmp_path):
    settings = Settings(
        sqlite_path=str(tmp_path / "assess.db"),
        database_url="",
    )
    store = make_assessment_store(settings)
    assert isinstance(store, SqliteTablePublisher)
    _exercise_store(store)


def test_factory_selects_postgres_when_url_set():
    pytest.importorskip("psycopg")
    from offline_cancel_risk.adapters.postgres_publishers import PostgresTablePublisher

    settings = Settings(database_url="postgresql://user:pass@localhost/db")
    # Don't connect — only verify factory class selection by stubbing ensure
    original_init = PostgresTablePublisher.__init__

    def _init(self, database_url: str) -> None:  # noqa: ANN001
        self._url = database_url
        # skip schema / connect

    PostgresTablePublisher.__init__ = _init  # type: ignore[method-assign]
    try:
        store = make_assessment_store(settings)
        assert isinstance(store, PostgresTablePublisher)
    finally:
        PostgresTablePublisher.__init__ = original_init  # type: ignore[method-assign]


@pytest.mark.skipif(
    not os.environ.get("OCR_DATABASE_URL"),
    reason="Set OCR_DATABASE_URL to exercise Postgres AssessmentStore",
)
def test_postgres_satisfies_assessment_store_port():
    pytest.importorskip("psycopg")
    from offline_cancel_risk.adapters.postgres_publishers import PostgresTablePublisher

    store: AssessmentStore = PostgresTablePublisher(os.environ["OCR_DATABASE_URL"])
    _exercise_store(store, order_id=f"ORD-PG-{os.getpid()}")
