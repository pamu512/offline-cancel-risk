"""SQLite-backed assess job queue for multi-process workers."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.adapters.publishers import StreamPublisher, TablePublisher
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.control_plane.metrics import LabelMetricsStore
from offline_cancel_risk.features.anomaly import EntityAnomalyStore
from offline_cancel_risk.features.chat_signals import ChatSignalStore
from offline_cancel_risk.features.device_graph import DeviceGraphStore
from offline_cancel_risk.features.device_store import DeviceIntegrityStore
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.feedback.sampler import bias_hints_from_metrics
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.models.canary import CanaryController
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.worker.queue import AssessJob

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assess_jobs (
  job_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assess_jobs_status_created
  ON assess_jobs(status, created_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SqliteAssessJobQueue:
    """Durable job queue; workers claim with BEGIN IMMEDIATE."""

    def __init__(self, sqlite_path: Path | str, *, poll_seconds: float = 0.25) -> None:
        self._path = Path(sqlite_path)
        self._poll_s = max(0.05, float(poll_seconds))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def create_job(self, request: AssessRequest) -> str:
        job_id = uuid4().hex
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assess_jobs(
                  job_id, status, request_json, result_json, error, created_at, updated_at
                ) VALUES (?, 'queued', ?, NULL, NULL, ?, ?)
                """,
                (job_id, request.model_dump_json(), now, now),
            )
            conn.commit()
        return job_id

    def schedule(self, job_id: str) -> None:
        # create_job already queues; ensure status for re-schedule
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE assess_jobs SET status='queued', updated_at=?
                WHERE job_id=? AND status IN ('queued', 'failed')
                """,
                (now, job_id),
            )
            conn.commit()

    def get(self, job_id: str) -> AssessJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assess_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        result = None
        if row["result_json"]:
            result = AssessmentResult.model_validate_json(row["result_json"])
        return AssessJob(
            job_id=row["job_id"],
            request=AssessRequest.model_validate_json(row["request_json"]),
            status=row["status"],
            result=result,
            error=row["error"],
        )

    def _claim_next(self) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id FROM assess_jobs
                WHERE status='queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            job_id = str(row["job_id"])
            conn.execute(
                """
                UPDATE assess_jobs SET status='running', updated_at=?
                WHERE job_id=? AND status='queued'
                """,
                (_utc_now(), job_id),
            )
            conn.execute("COMMIT")
            return job_id

    def _save(self, job: AssessJob) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE assess_jobs
                SET status=?, result_json=?, error=?, updated_at=?
                WHERE job_id=?
                """,
                (
                    job.status,
                    job.result.model_dump_json() if job.result is not None else None,
                    job.error,
                    _utc_now(),
                    job.job_id,
                ),
            )
            conn.commit()

    async def run_one(
        self,
        job_id: str,
        *,
        gps_client: GpsClient,
        policy: dict[str, Any],
        stream: StreamPublisher,
        table: TablePublisher,
        registry: ModelRegistry | None = None,
        shadow_metrics: ShadowMetricsStore | None = None,
        canary: CanaryController | None = None,
        overlays: PolicyOverlayStore | None = None,
        tickets: LabelTicketStore | None = None,
        bias_hints: dict[str, str] | None = None,
        label_metrics: LabelMetricsStore | None = None,
        driver_chains: DriverChainStore | None = None,
        baselines: EntityBaselineStore | None = None,
        cancel_stats: EntityCancelStatsStore | None = None,
        devices: DeviceIntegrityStore | None = None,
        device_graph: DeviceGraphStore | None = None,
        chat_store: ChatSignalStore | None = None,
        anomalies: EntityAnomalyStore | None = None,
    ) -> AssessJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"job missing: {job_id}")
        job.status = "running"
        self._save(job)
        hints = bias_hints
        if label_metrics is not None:
            hints = bias_hints_from_metrics(label_metrics.latest(limit=50))
        try:
            job.result = await assess_order(
                job.request,
                gps_client,
                policy,
                stream=stream,
                table=table,
                registry=registry,
                shadow_metrics=shadow_metrics,
                canary=canary,
                overlays=overlays,
                tickets=tickets,
                bias_hints=hints,
                driver_chains=driver_chains,
                baselines=baselines,
                cancel_stats=cancel_stats,
                devices=devices,
                device_graph=device_graph,
                chat_store=chat_store,
                anomalies=anomalies,
            )
            job.status = "done"
            job.error = None
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
        self._save(job)
        return job

    async def run_worker(
        self,
        *,
        gps_client: GpsClient,
        policy: dict[str, Any],
        stream: StreamPublisher,
        table: TablePublisher,
        registry: ModelRegistry | None = None,
        shadow_metrics: ShadowMetricsStore | None = None,
        canary: CanaryController | None = None,
        overlays: PolicyOverlayStore | None = None,
        tickets: LabelTicketStore | None = None,
        bias_hints: dict[str, str] | None = None,
        label_metrics: LabelMetricsStore | None = None,
        driver_chains: DriverChainStore | None = None,
        baselines: EntityBaselineStore | None = None,
        cancel_stats: EntityCancelStatsStore | None = None,
        devices: DeviceIntegrityStore | None = None,
        device_graph: DeviceGraphStore | None = None,
        chat_store: ChatSignalStore | None = None,
        anomalies: EntityAnomalyStore | None = None,
    ) -> None:
        while True:
            job_id = self._claim_next()
            if job_id is None:
                await asyncio.sleep(self._poll_s)
                continue
            await self.run_one(
                job_id,
                gps_client=gps_client,
                policy=policy,
                stream=stream,
                table=table,
                registry=registry,
                shadow_metrics=shadow_metrics,
                canary=canary,
                overlays=overlays,
                tickets=tickets,
                bias_hints=bias_hints,
                label_metrics=label_metrics,
                driver_chains=driver_chains,
                baselines=baselines,
                cancel_stats=cancel_stats,
                devices=devices,
                device_graph=device_graph,
                chat_store=chat_store,
                anomalies=anomalies,
            )
