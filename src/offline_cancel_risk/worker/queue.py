from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.adapters.publishers import StreamPublisher, TablePublisher
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.models.canary import CanaryController
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.control_plane.metrics import LabelMetricsStore
from offline_cancel_risk.feedback.sampler import bias_hints_from_metrics
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.policy.overlays import PolicyOverlayStore


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
        registry: ModelRegistry | None = None,
        shadow_metrics: ShadowMetricsStore | None = None,
        canary: CanaryController | None = None,
        overlays: PolicyOverlayStore | None = None,
        tickets: LabelTicketStore | None = None,
        bias_hints: dict[str, str] | None = None,
        label_metrics: LabelMetricsStore | None = None,
    ) -> AssessJob:
        job = self._jobs[job_id]
        job.status = "running"
        # Refresh bias hints per job so worker doesn't freeze startup metrics.
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
        registry: ModelRegistry | None = None,
        shadow_metrics: ShadowMetricsStore | None = None,
        canary: CanaryController | None = None,
        overlays: PolicyOverlayStore | None = None,
        tickets: LabelTicketStore | None = None,
        bias_hints: dict[str, str] | None = None,
        label_metrics: LabelMetricsStore | None = None,
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
                    registry=registry,
                    shadow_metrics=shadow_metrics,
                    canary=canary,
                    overlays=overlays,
                    tickets=tickets,
                    bias_hints=bias_hints,
                    label_metrics=label_metrics,
                )
            finally:
                self._queue.task_done()
