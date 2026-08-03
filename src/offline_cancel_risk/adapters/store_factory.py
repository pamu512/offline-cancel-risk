"""Build AssessmentStore from settings (SQLite default, Postgres via URL)."""

from __future__ import annotations

from offline_cancel_risk.adapters.postgres_publishers import PostgresTablePublisher
from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.ports import AssessmentStore
from offline_cancel_risk.settings import Settings


def make_assessment_store(settings: Settings) -> AssessmentStore:
    url = (settings.database_url or "").strip()
    if url:
        return PostgresTablePublisher(url)
    return SqliteTablePublisher(sqlite_path=settings.sqlite_path)
