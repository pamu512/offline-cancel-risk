from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.adapters.publishers import StreamPublisher, TablePublisher
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.pipeline.assess import assess_order


@dataclass
class AssessJob:
    job_id: str
    request: AssessRequest
    status: str = "queued"
    result: AssessmentResult | None = None
    error: str | None = None


@dataclass
class AssessJobQueue:
    """In-process asyncio assess queue; worker drains when sync_assess is false."""

    _queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    _jobs: dict[str, AssessJob] = field(default_factory=dict)

    def create_job(self, request: AssessRequest) -> str:
        job_id = uuid4().hex
        self._jobs[job_id] = AssessJob(job_id=job_id, request=request)
        return job_id

    def schedule(self, job_id: str) -> None:
        self._queue.put_nowait(job_id)

    def get(self, job_id: str) -> AssessJob | None:
        return self._jobs.get(job_id)

    async def run_one(
        self,
        job_id: str,
        *,
        gps_client: GpsClient,
        policy: dict[str, Any],
        stream: StreamPublisher,
        table: TablePublisher,
    ) -> AssessJob:
        job = self._jobs[job_id]
        job.status = "running"
        try:
            job.result = await assess_order(
                job.request, gps_client, policy, stream=stream, table=table
            )
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 — job boundary; surface as failed status
            job.status = "failed"
            job.error = str(exc)
        return job

    async def run_worker(
        self,
        *,
        gps_client: GpsClient,
        policy: dict[str, Any],
        stream: StreamPublisher,
        table: TablePublisher,
    ) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self.run_one(
                    job_id,
                    gps_client=gps_client,
                    policy=policy,
                    stream=stream,
                    table=table,
                )
            finally:
                self._queue.task_done()
