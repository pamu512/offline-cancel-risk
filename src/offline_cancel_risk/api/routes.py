from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult, OutcomeIngestRequest
from offline_cancel_risk.control_plane.cycle import run_metrics_and_tune
from offline_cancel_risk.features.chat_signals import (
    instant_chat_risk,
    normalize_chat_signals,
)
from offline_cancel_risk.feedback.sampler import (
    bias_hints_from_metrics,
    run_batch_sample,
)
from offline_cancel_risk.policy.resolve import GuardrailError
from offline_cancel_risk.policy.service import (
    resolved_policy_for_market,
    save_overlay,
)


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


class PolicyOverlayIngestRequest(BaseModel):
    """Product FE / control plane posts tunable params for a market."""

    region_code: str
    city_code: str = ""
    overlay: dict[str, Any] = Field(default_factory=dict)


class ForecastIngestRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class HardgateIngestRequest(BaseModel):
    region_code: str
    city_code: str = ""
    window: str
    max_enforcements: int
    heads: list[str] = Field(default_factory=lambda: ["*"])
    actor: str = "ops"


class ClawbackRequest(BaseModel):
    region_code: str
    city_code: str = ""
    ttl_minutes: int = 60
    reason: str = "clawback"


class TuningRunRequest(BaseModel):
    region_code: str
    city_code: str = ""


class MarketplaceEventIngest(BaseModel):
    driver_id: int
    user_id: int | None = None
    order_display_id: str
    event_type: str  # accept | cancel | complete
    event_ts: str
    cancel_with_cause: bool | None = None
    cancel_reason_code: str | None = None


class MarketplaceEventsRequest(BaseModel):
    events: list[MarketplaceEventIngest]


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
            registry=getattr(request.app.state, "registry", None),
            shadow_metrics=getattr(request.app.state, "shadow_metrics", None),
            canary=getattr(request.app.state, "canary", None),
            overlays=getattr(request.app.state, "overlays", None),
            tickets=getattr(request.app.state, "tickets", None),
            label_metrics=getattr(request.app.state, "label_metrics", None),
            driver_chains=getattr(request.app.state, "driver_chains", None),
            baselines=getattr(request.app.state, "baselines", None),
            cancel_stats=getattr(request.app.state, "cancel_stats", None),
            devices=getattr(request.app.state, "devices", None),
            device_graph=getattr(request.app.state, "device_graph", None),
            chat_store=getattr(request.app.state, "chat_store", None),
            anomalies=getattr(request.app.state, "anomalies", None),
            outcomes=getattr(request.app.state, "outcomes", None),
        )
    else:
        queue.schedule(job_id)
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=500, detail="job missing after enqueue")
    return {"job_id": job_id, "status": job.status}


@router.post("/assess")
async def assess(body: AssessRequest, request: Request) -> dict[str, str]:
    _require_auth(request)
    return await _enqueue_one(request, body)


@router.post("/assess:batch")
async def assess_batch(body: AssessBatchRequest, request: Request) -> dict[str, list[str]]:
    _require_auth(request)
    job_ids: list[str] = []
    for order in body.orders:
        result = await _enqueue_one(request, order)
        job_ids.append(result["job_id"])
    return {"job_ids": job_ids}


@router.get("/assess/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, request: Request) -> JobResponse:
    _require_auth(request)
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
    _require_auth(request)
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
    _require_auth(request)
    return request.app.state.table.list_generations(order_display_id)


@router.post("/feedback")
async def feedback_upsert(body: FeedbackRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    request.app.state.table.upsert_feedback(body.order_display_id, body.labels)
    closed = 0
    tickets = getattr(request.app.state, "tickets", None)
    if tickets is not None:
        closed = tickets.mark_labeled(body.order_display_id)
    loop = getattr(request.app.state, "control_loop", None)
    if loop is not None:
        loop.notify_feedback(body.order_display_id)
    return {"ok": True, "tickets_closed": closed}


class SampleFeedbackRequest(BaseModel):
    region_code: str = ""
    city_code: str = ""
    lookback_limit: int = 500


@router.get("/feedback/tickets")
async def list_feedback_tickets(
    request: Request,
    status: str | None = None,
    day_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    _require_auth(request)
    return request.app.state.tickets.list_tickets(
        status=status, day_key=day_key, limit=limit
    )


@router.post("/feedback/sample")
async def sample_feedback_tickets(
    body: SampleFeedbackRequest, request: Request
) -> dict[str, Any]:
    _require_auth(request)
    table = request.app.state.table
    labeled = {f["order_display_id"] for f in table.list_feedback()}
    assessments = table.list_latest_assessments()
    if body.lookback_limit > 0:
        assessments = assessments[: int(body.lookback_limit)]
    hints = bias_hints_from_metrics(
        request.app.state.label_metrics.latest(limit=50)
    )
    created = run_batch_sample(
        request.app.state.tickets,
        assessments,
        request.app.state.policy,
        labeled_order_ids=labeled,
        bias_hints=hints,
        region_code=body.region_code,
        city_code=body.city_code,
    )
    return {
        "created": len(created),
        "ticket_ids": [t["ticket_id"] for t in created],
        "day_count": request.app.state.tickets.day_count(),
    }


class SideloadRequest(BaseModel):
    bundle_path: str
    role: str = "shadow"


def _require_auth(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.auth_required:
        return
    keys = {k.strip() for k in settings.api_keys.split(",") if k.strip()}
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = request.headers.get("x-api-key", "").strip()
    if not keys or token not in keys:
        raise HTTPException(status_code=401, detail="unauthorized")


_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _infer_head_from_assessment(result: AssessmentResult) -> str:
    flags = result.flags.model_dump()
    scores = result.scores.model_dump()
    flagged = [h for h in _HEADS if flags.get(h) == 1]
    pool = flagged if flagged else list(_HEADS)
    return max(pool, key=lambda h: float(scores[h]))


@router.post("/outcomes")
async def ingest_outcome(
    body: OutcomeIngestRequest, request: Request
) -> dict[str, Any]:
    _require_auth(request)
    store = getattr(request.app.state, "outcomes", None)
    if store is None:
        raise HTTPException(status_code=503, detail="outcomes unavailable")

    policy = request.app.state.policy
    ear = policy.get("ear") or {}
    cold_start = {
        k: float(v) for k, v in (ear.get("recoverability") or {}).items()
    }
    alpha = float(ear.get("outcome_ewma_alpha", 0.05))
    guardrails = request.app.state.guardrails.get(
        "bounds", request.app.state.guardrails
    )

    region = body.region_code
    city = body.city_code
    head = body.head

    if head is None or region is None or city is None:
        latest = request.app.state.table.latest(body.order_display_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="order not found")
        if head is None:
            head = _infer_head_from_assessment(latest)
        if region is None:
            region = latest.region_code or ""
        if city is None:
            city = latest.city_code or ""

    region = region.strip().upper()
    city = city.strip().upper()

    try:
        return store.record_outcome(
            order_display_id=body.order_display_id,
            outcome=body.outcome,
            head=head,
            region_code=region,
            city_code=city,
            amount=body.amount,
            occurred_at=body.occurred_at,
            alpha=alpha,
            cold_start=cold_start,
            guardrails=guardrails,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/outcomes/recoverability")
async def get_outcome_recoverability(
    request: Request,
    region_code: str = "",
    city_code: str = "",
) -> dict[str, Any]:
    _require_auth(request)
    store = getattr(request.app.state, "outcomes", None)
    if store is None:
        raise HTTPException(status_code=503, detail="outcomes unavailable")
    region = region_code.strip().upper()
    city = city_code.strip().upper()
    return {
        "region_code": region,
        "city_code": city,
        "heads": store.get_recoverability(region, city),
    }


@router.get("/outcomes")
async def list_outcomes(
    request: Request,
    order_display_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    _require_auth(request)
    store = getattr(request.app.state, "outcomes", None)
    if store is None:
        raise HTTPException(status_code=503, detail="outcomes unavailable")
    return store.list_outcomes(order_display_id=order_display_id, limit=limit)


@router.get("/models")
async def list_models(request: Request) -> list[dict[str, str]]:
    _require_auth(request)
    return [
        {
            "model_id": m.model_id,
            "format": m.format,
            "role": m.role,
            "feature_schema_version": m.feature_schema_version,
            "created_at": m.created_at,
        }
        for m in request.app.state.registry.list_models()
    ]


@router.post("/models")
async def sideload_model(body: SideloadRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    role = body.role if body.role in {
        "champion", "shadow", "canary", "retired", "failed_canary"
    } else "shadow"
    rec = request.app.state.registry.sideload(body.bundle_path, role=role)  # type: ignore[arg-type]
    return {
        "model_id": rec.model_id,
        "role": rec.role,
        "format": rec.format,
        "bundle_path": rec.bundle_path,
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        rec = request.app.state.registry.get(model_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    promo = request.app.state.canary.latest_promotion_status(model_id)
    return {
        "model_id": rec.model_id,
        "format": rec.format,
        "role": rec.role,
        "feature_schema_version": rec.feature_schema_version,
        "created_at": rec.created_at,
        "promotion": promo,
        "canary_active": (
            request.app.state.canary.active().challenger_model_id == model_id
            if request.app.state.canary.active()
            else False
        ),
    }


@router.post("/models/{model_id}/evaluate")
async def evaluate_model(model_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    status = request.app.state.canary.evaluate_and_maybe_start_canary(model_id)
    return {
        "challenger_model_id": status.challenger_model_id,
        "champion_model_id": status.champion_model_id,
        "promotion_ready": status.promotion_ready,
        "promotion_blockers": status.promotion_blockers,
        "metrics": status.metrics,
        "recommended_action": status.recommended_action,
    }


@router.post("/models/{model_id}/canary/start")
async def canary_start(model_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    state = request.app.state.canary.start_canary(model_id)
    return {
        "challenger_model_id": state.challenger_model_id,
        "status": state.status,
        "canary_pct": state.canary_pct,
        "canary_hours": state.canary_hours,
        "started_at": state.started_at,
    }


@router.post("/models/{model_id}/canary/abort")
async def canary_abort(model_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    del model_id  # abort active canary regardless of path id
    request.app.state.canary.abort()
    return {"ok": True}


@router.post("/models/{model_id}/promote")
async def force_promote(model_id: str, request: Request) -> dict[str, str]:
    _require_auth(request)
    champ = request.app.state.registry.get_champion()
    if champ is not None:
        request.app.state.registry.set_role(champ.model_id, "retired")
    request.app.state.registry.set_role(model_id, "champion")
    return {"model_id": model_id, "role": "champion"}


@router.get("/policy/guardrails")
async def get_policy_guardrails(request: Request) -> dict[str, Any]:
    """Bounds Product FE uses to constrain tunable params before ingest."""
    _require_auth(request)
    return request.app.state.guardrails


@router.get("/policy/overlays")
async def list_policy_overlays(request: Request) -> list[dict[str, str]]:
    _require_auth(request)
    return request.app.state.overlays.list_keys()


@router.get("/policy/overlays/{region_code}")
async def get_region_overlay(
    region_code: str, request: Request, city_code: str = ""
) -> dict[str, Any]:
    _require_auth(request)
    overlay = request.app.state.overlays.get(region_code, city_code)
    if overlay is None:
        raise HTTPException(status_code=404, detail="overlay not found")
    return {
        "region_code": region_code.strip().upper(),
        "city_code": city_code.strip().upper(),
        "overlay": overlay,
    }


@router.get("/policy/resolved")
async def get_resolved_policy(
    request: Request,
    region_code: str = "",
    city_code: str = "",
) -> dict[str, Any]:
    """Default ← region ← city merge used at assess time for a market."""
    _require_auth(request)
    return resolved_policy_for_market(
        request.app.state.policy,
        request.app.state.overlays,
        region_code=region_code or None,
        city_code=city_code or None,
    )


@router.put("/policy/overlays")
async def ingest_policy_overlay(
    body: PolicyOverlayIngestRequest, request: Request
) -> dict[str, Any]:
    """Ingest ops-tuned params for a region/city within risk guardrails.

    Product owns the FE; this endpoint is the control-plane write path.
    Empty city_code = region-wide overlay. City overlay wins over region.
    """
    _require_auth(request)
    before = request.app.state.overlays.get(body.region_code, body.city_code)
    try:
        saved = save_overlay(
            request.app.state.overlays,
            request.app.state.guardrails,
            region_code=body.region_code,
            city_code=body.city_code,
            overlay=body.overlay,
        )
    except GuardrailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.app.state.audit.append(
        actor="manual_overlay",
        action="apply",
        region_code=body.region_code,
        city_code=body.city_code,
        before=before,
        after=body.overlay,
        decision="accepted",
        reason="manual_ingest",
    )
    return saved


@router.delete("/policy/overlays/{region_code}")
async def delete_policy_overlay(
    region_code: str, request: Request, city_code: str = ""
) -> dict[str, bool]:
    _require_auth(request)
    deleted = request.app.state.overlays.delete(region_code, city_code)
    if not deleted:
        raise HTTPException(status_code=404, detail="overlay not found")
    return {"ok": True}


@router.put("/supply/forecast")
async def put_supply_forecast(
    body: ForecastIngestRequest, request: Request
) -> dict[str, int]:
    _require_auth(request)
    n = request.app.state.forecast.upsert(body.rows)
    request.app.state.audit.append(
        actor="ops_ingest",
        action="forecast_ingest",
        after={"count": n},
        decision="recorded",
        reason="forecast_upsert",
    )
    return {"upserted": n}


@router.get("/supply/forecast")
async def get_supply_forecast(
    request: Request, limit: int = 100
) -> list[dict[str, Any]]:
    _require_auth(request)
    return request.app.state.forecast.list_all(limit=limit)


@router.put("/enforcement/hardgates")
async def put_hardgates(
    body: HardgateIngestRequest, request: Request
) -> dict[str, Any]:
    _require_auth(request)
    try:
        request.app.state.hardgates.upsert(
            body.region_code,
            body.city_code,
            window=body.window,
            max_enforcements=body.max_enforcements,
            heads=body.heads,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.app.state.audit.append(
        actor=body.actor,
        action="hardgate_ingest",
        region_code=body.region_code,
        city_code=body.city_code,
        after=body.model_dump(),
        decision="recorded",
        reason="hardgate_upsert",
    )
    return {
        "region_code": body.region_code.strip().upper(),
        "city_code": body.city_code.strip().upper(),
        "windows": request.app.state.hardgates.get(body.region_code, body.city_code),
    }


@router.get("/enforcement/hardgates")
async def get_hardgates(
    request: Request, region_code: str, city_code: str = ""
) -> dict[str, Any]:
    _require_auth(request)
    return {
        "region_code": region_code.strip().upper(),
        "city_code": city_code.strip().upper(),
        "windows": request.app.state.hardgates.get(region_code, city_code),
    }


@router.post("/enforcement/clawback")
async def post_clawback(body: ClawbackRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    state = request.app.state.hardgates.record_clawback(
        body.region_code,
        body.city_code,
        ttl_minutes=body.ttl_minutes,
        reason=body.reason,
    )
    request.app.state.audit.append(
        actor="ops_ingest",
        action="clawback_signal",
        region_code=body.region_code,
        city_code=body.city_code,
        after=state,
        decision="recorded",
        reason=body.reason,
    )
    return state


@router.post("/marketplace/events")
async def ingest_marketplace_events(
    body: MarketplaceEventsRequest, request: Request
) -> dict[str, int]:
    """Downstream posts accept/complete/cancel funnel events for marketplace metrics."""
    _require_auth(request)
    store = getattr(request.app.state, "cancel_stats", None)
    if store is None:
        raise HTTPException(status_code=503, detail="marketplace stats unavailable")
    n = 0
    for ev in body.events:
        store.record_market_event(
            driver_id=ev.driver_id,
            user_id=ev.user_id,
            order_display_id=ev.order_display_id,
            event_type=ev.event_type,
            event_ts=ev.event_ts,
            cancel_with_cause=ev.cancel_with_cause,
            cancel_reason_code=ev.cancel_reason_code,
        )
        n += 1
    return {"recorded": n}


@router.get("/devices/{device_id}")
async def get_device_integrity(device_id: str, request: Request) -> dict[str, Any]:
    """Last EWMA / flags for a Downstream device_id (slice 2)."""
    _require_auth(request)
    store = getattr(request.app.state, "devices", None)
    if store is None:
        raise HTTPException(status_code=503, detail="device integrity unavailable")
    row = store.get(device_id)
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    return row


class DeviceGraphEdge(BaseModel):
    device_id: str
    driver_id: int | None = None
    user_id: int | None = None
    event_ts: str | None = None


class DeviceGraphEdgesRequest(BaseModel):
    edges: list[DeviceGraphEdge] = Field(default_factory=list)


@router.post("/device-graph/edges")
async def ingest_device_graph_edges(
    body: DeviceGraphEdgesRequest, request: Request
) -> dict[str, int]:
    """Downstream posts device↔driver/user identity edges (slice 3)."""
    _require_auth(request)
    store = getattr(request.app.state, "device_graph", None)
    if store is None:
        raise HTTPException(status_code=503, detail="device graph unavailable")
    n = 0
    for edge in body.edges:
        if not edge.device_id or (edge.driver_id is None and edge.user_id is None):
            continue
        store.observe(
            device_id=edge.device_id,
            driver_id=edge.driver_id,
            user_id=edge.user_id,
            event_ts=edge.event_ts,
        )
        n += 1
    return {"recorded": n}


@router.get("/device-graph/{device_id}")
async def get_device_graph(device_id: str, request: Request) -> dict[str, Any]:
    """Rolling device graph counts / signals for a device_id."""
    _require_auth(request)
    store = getattr(request.app.state, "device_graph", None)
    if store is None:
        raise HTTPException(status_code=503, detail="device graph unavailable")
    policy = getattr(request.app.state, "policy", {}) or {}
    return store.evaluate(
        device_id=device_id,
        driver_id=None,
        user_id=None,
        as_of=None,
        policy=policy,
    )


class ChatSignalIngest(BaseModel):
    order_display_id: str
    driver_id: int | None = None
    user_id: int | None = None
    event_ts: str | None = None
    persuasion_suspected: bool | None = None
    cash_offline_suggested: bool | None = None
    rider_forced_cancel: bool | None = None
    signal_score: float | None = None
    source: str | None = None


class ChatSignalsRequest(BaseModel):
    signals: list[ChatSignalIngest] = Field(default_factory=list)


@router.post("/chat-signals")
async def ingest_chat_signals(
    body: ChatSignalsRequest, request: Request
) -> dict[str, int]:
    """Downstream posts structured force-cancel / persuasion flags (no NLP)."""
    _require_auth(request)
    store = getattr(request.app.state, "chat_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="chat signals unavailable")
    n = 0
    for sig in body.signals:
        flags = normalize_chat_signals(
            {
                "persuasion_suspected": sig.persuasion_suspected,
                "cash_offline_suggested": sig.cash_offline_suggested,
                "rider_forced_cancel": sig.rider_forced_cancel,
                "signal_score": sig.signal_score,
                "source": sig.source,
            }
        )
        if not any(
            flags.get(k)
            for k in (
                "persuasion_suspected",
                "cash_offline_suggested",
                "rider_forced_cancel",
            )
        ) and flags.get("signal_score") is None:
            continue
        store.upsert(
            order_display_id=sig.order_display_id,
            driver_id=sig.driver_id,
            user_id=sig.user_id,
            flags=flags,
            risk=instant_chat_risk(flags),
            event_ts=sig.event_ts,
        )
        n += 1
    return {"recorded": n}


@router.get("/chat-signals/{order_display_id}")
async def get_chat_signals(order_display_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    store = getattr(request.app.state, "chat_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="chat signals unavailable")
    row = store.get(order_display_id)
    if row is None:
        raise HTTPException(status_code=404, detail="signals not found")
    return row


@router.get("/anomalies/{entity_key:path}")
async def get_entity_anomaly(entity_key: str, request: Request) -> dict[str, Any]:
    """Recent feature samples for an entity key (`driver:1`)."""
    _require_auth(request)
    store = getattr(request.app.state, "anomalies", None)
    if store is None:
        raise HTTPException(status_code=503, detail="anomaly store unavailable")
    cfg = (getattr(request.app.state, "policy", {}) or {}).get("anomaly") or {}
    features = list(cfg.get("features") or ["accept_cancel_rate", "cancel_rate", "cancel_abuse"])
    window_n = int(cfg.get("window_n", 20))
    out: dict[str, Any] = {"entity_key": entity_key, "features": {}}
    for feat in features:
        out["features"][feat] = store.recent_entity_values(
            entity_key, feat, limit=window_n
        )
    return out


@router.get("/baselines")
async def list_baselines(
    request: Request,
    entity_kind: str = "",
    driver_id: int | None = None,
    user_id: int | None = None,
    head: str = "",
    discount_active: bool | None = None,
    updated_since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    _require_auth(request)
    store = getattr(request.app.state, "baselines", None)
    if store is None:
        return []
    return store.query(
        entity_kind=entity_kind,
        driver_id=driver_id,
        user_id=user_id,
        head=head,
        discount_active=discount_active,
        updated_since=updated_since,
        limit=limit,
    )


@router.get("/baselines/{entity_key:path}")
async def get_baseline_entity(
    entity_key: str, request: Request
) -> list[dict[str, Any]]:
    _require_auth(request)
    store = getattr(request.app.state, "baselines", None)
    if store is None:
        raise HTTPException(status_code=404, detail="baselines unavailable")
    rows = store.list_entity(entity_key)
    if not rows:
        raise HTTPException(status_code=404, detail="entity not found")
    return rows


@router.get("/metrics/labels")
async def get_label_metrics(
    request: Request,
    region_code: str = "",
    city_code: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    _require_auth(request)
    return request.app.state.label_metrics.latest(
        region_code=region_code, city_code=city_code, limit=limit
    )


@router.post("/tuning/run")
async def run_tuning(body: TuningRunRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    return run_metrics_and_tune(
        settings=request.app.state.settings,
        policy=request.app.state.policy,
        guardrails=request.app.state.guardrails,
        overlays=request.app.state.overlays,
        audit=request.app.state.audit,
        forecast=request.app.state.forecast,
        hardgates=request.app.state.hardgates,
        label_metrics=request.app.state.label_metrics,
        op_cfg=request.app.state.operating_point_cfg,
        table=request.app.state.table,
        region_code=body.region_code,
        city_code=body.city_code,
        reason="tuning_run",
    )


@router.get("/tuning/suggestions")
async def get_tuning_suggestions(
    request: Request, limit: int = 50
) -> list[dict[str, Any]]:
    _require_auth(request)
    rows = request.app.state.audit.list_entries(limit=limit * 3)
    return [
        r
        for r in rows
        if r["action"] in {"suggest", "apply", "reject"}
    ][:limit]


@router.get("/audit/policy")
async def get_policy_audit(
    request: Request, limit: int = 100, action: str | None = None
) -> list[dict[str, Any]]:
    _require_auth(request)
    return request.app.state.audit.list_entries(limit=limit, action=action)
