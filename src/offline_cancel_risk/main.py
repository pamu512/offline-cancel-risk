from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from offline_cancel_risk.adapters.gps import FakeGpsClient, GpsClient, HttpGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.routes import router
from offline_cancel_risk.models.canary import CanaryController
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.settings import Settings, get_settings, load_policy
from offline_cancel_risk.worker.queue import AssessJobQueue


def _default_gps_client(settings: Settings) -> GpsClient:
    if settings.gps_base_url:
        return HttpGpsClient(
            base_url=settings.gps_base_url, api_key=settings.gps_api_key
        )
    # ponytail: empty FakeGpsClient when no GPS URL — tenants inject or set OCR_GPS_BASE_URL
    return FakeGpsClient([])


def create_app(
    *,
    gps_client: GpsClient | None = None,
    settings: Settings | None = None,
    stream: JsonlStreamPublisher | None = None,
    table: SqliteTablePublisher | None = None,
    registry: ModelRegistry | None = None,
    shadow_metrics: ShadowMetricsStore | None = None,
    canary: CanaryController | None = None,
    overlays: PolicyOverlayStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    gps = gps_client if gps_client is not None else _default_gps_client(settings)
    stream_pub = stream or JsonlStreamPublisher(stream_path=settings.stream_path)
    table_pub = table or SqliteTablePublisher(sqlite_path=settings.sqlite_path)
    policy = load_policy(settings.policy_path)
    guardrails = load_policy(settings.policy_guardrails_path)
    gates = load_policy(settings.promote_gates_path)
    overlay_store = overlays or PolicyOverlayStore(settings.policy_overlays_path)
    reg = registry or ModelRegistry(settings.models_sqlite_path, settings.models_root)
    metrics = shadow_metrics or ShadowMetricsStore(settings.shadow_metrics_path)
    canary_ctrl = canary or CanaryController(
        settings.canary_sqlite_path,
        reg,
        metrics,
        gates=gates,
        thresholds={k: float(v) for k, v in policy["thresholds"].items()},
    )
    queue = AssessJobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker_task: asyncio.Task[None] | None = None
        if not settings.sync_assess:
            worker_task = asyncio.create_task(
                queue.run_worker(
                    gps_client=gps,
                    policy=policy,
                    stream=stream_pub,
                    table=table_pub,
                    registry=reg,
                    shadow_metrics=metrics,
                    canary=canary_ctrl,
                    overlays=overlay_store,
                )
            )
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="offline-cancel-risk", lifespan=lifespan)
    app.state.settings = settings
    app.state.gps_client = gps
    app.state.stream = stream_pub
    app.state.table = table_pub
    app.state.policy = policy
    app.state.guardrails = guardrails
    app.state.overlays = overlay_store
    app.state.gates = gates
    app.state.queue = queue
    app.state.registry = reg
    app.state.shadow_metrics = metrics
    app.state.canary = canary_ctrl
    app.include_router(router)
    return app


app = create_app()
