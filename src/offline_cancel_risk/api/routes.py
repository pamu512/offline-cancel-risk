from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult


class AssessBatchRequest(BaseModel):
    orders: list[AssessRequest] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    order_display_id: str
    labels: dict[str, Any]


class JobResponse(BaseModel):
    job_id: str
    status: str
    result: AssessmentResult | None = None
    error: str | None = None


router = APIRouter(prefix="/v1")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _enqueue_one(request: Request, body: AssessRequest) -> dict[str, str]:
    queue = request.app.state.queue
    settings = request.app.state.settings
    job_id = queue.create_job(body)
    if settings.sync_assess:
        await queue.run_one(
            job_id,
            gps_client=request.app.state.gps_client,
            policy=request.app.state.policy,
            stream=request.app.state.stream,
            table=request.app.state.table,
        )
    else:
        queue.schedule(job_id)
    job = queue.get(job_id)
    assert job is not None
    return {"job_id": job_id, "status": job.status}


@router.post("/assess")
async def assess(body: AssessRequest, request: Request) -> dict[str, str]:
    return await _enqueue_one(request, body)


@router.post("/assess:batch")
async def assess_batch(body: AssessBatchRequest, request: Request) -> dict[str, list[str]]:
    job_ids: list[str] = []
    for order in body.orders:
        result = await _enqueue_one(request, order)
        job_ids.append(result["job_id"])
    return {"job_ids": job_ids}


@router.get("/assess/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, request: Request) -> JobResponse:
    job = request.app.state.queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        result=job.result,
        error=job.error,
    )


@router.get("/orders/{order_display_id}/latest", response_model=AssessmentResult)
async def get_latest(order_display_id: str, request: Request) -> AssessmentResult:
    result = request.app.state.table.latest(order_display_id)
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result


@router.get(
    "/orders/{order_display_id}/generations",
    response_model=list[AssessmentResult],
)
async def get_generations(
    order_display_id: str, request: Request
) -> list[AssessmentResult]:
    return request.app.state.table.list_generations(order_display_id)


@router.post("/feedback")
async def feedback_upsert(body: FeedbackRequest, request: Request) -> dict[str, bool]:
    request.app.state.table.upsert_feedback(body.order_display_id, body.labels)
    return {"ok": True}
