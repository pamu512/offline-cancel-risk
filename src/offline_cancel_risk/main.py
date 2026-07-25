from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from offline_cancel_risk.adapters.gps import FakeGpsClient, GpsClient, HttpGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.routes import router
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
) -> FastAPI:
    settings = settings or get_settings()
    gps = gps_client if gps_client is not None else _default_gps_client(settings)
    stream_pub = stream or JsonlStreamPublisher(stream_path=settings.stream_path)
    table_pub = table or SqliteTablePublisher(sqlite_path=settings.sqlite_path)
    policy = load_policy(settings.policy_path)
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
    app.state.queue = queue
    app.include_router(router)
    return app


app = create_app()
