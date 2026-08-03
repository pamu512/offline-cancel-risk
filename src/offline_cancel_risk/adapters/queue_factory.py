"""Build assess job queue from settings (memory default, sqlite for multi-worker)."""

from __future__ import annotations

from offline_cancel_risk.settings import Settings
from offline_cancel_risk.worker.queue import AssessJobQueue
from offline_cancel_risk.worker.sqlite_queue import SqliteAssessJobQueue


def make_job_queue(
    settings: Settings,
) -> AssessJobQueue | SqliteAssessJobQueue:
    backend = (settings.queue_backend or "memory").strip().lower()
    if backend == "sqlite":
        return SqliteAssessJobQueue(settings.assess_queue_path)
    if backend != "memory":
        raise ValueError(f"unsupported OCR_QUEUE_BACKEND: {settings.queue_backend!r}")
    return AssessJobQueue()
