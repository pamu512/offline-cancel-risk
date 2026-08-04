from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from offline_cancel_risk.adapters.gps import FakeGpsClient, GpsClient, HttpGpsClient
from offline_cancel_risk.adapters.stream_factory import make_stream_publisher
from offline_cancel_risk.ports import StreamPublisher
from offline_cancel_risk.adapters.queue_factory import make_job_queue
from offline_cancel_risk.adapters.store_factory import make_assessment_store
from offline_cancel_risk.ports import AssessmentStore
from offline_cancel_risk.api.routes import router
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.features.anomaly import EntityAnomalyStore
from offline_cancel_risk.features.chat_signals import ChatSignalStore
from offline_cancel_risk.features.device_graph import DeviceGraphStore
from offline_cancel_risk.features.device_store import DeviceIntegrityStore
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.leader import FileLeaderLock
from offline_cancel_risk.control_plane.loop import ControlPlaneLoop
from offline_cancel_risk.control_plane.metrics import LabelMetricsStore
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.models.canary import CanaryController
from offline_cancel_risk.outcomes.store import OutcomeStore
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.settings import Settings, apply_profile, get_settings, load_policy


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
    stream: StreamPublisher | None = None,
    table: AssessmentStore | None = None,
    registry: ModelRegistry | None = None,
    shadow_metrics: ShadowMetricsStore | None = None,
    canary: CanaryController | None = None,
    overlays: PolicyOverlayStore | None = None,
) -> FastAPI:
    settings = apply_profile(settings or get_settings())
    if (
        settings.profile.strip().lower() == "prod"
        and not settings.gps_base_url.strip()
        and gps_client is None
    ):
        raise RuntimeError(
            "OCR_PROFILE=prod requires OCR_GPS_BASE_URL or an injected gps_client"
        )
    gps = gps_client if gps_client is not None else _default_gps_client(settings)
    stream_pub = stream if stream is not None else make_stream_publisher(settings)
    table_pub = table or make_assessment_store(settings)
    policy = load_policy(settings.policy_path)
    guardrails = load_policy(settings.policy_guardrails_path)
    gates = load_policy(settings.promote_gates_path)
    overlay_store = overlays or PolicyOverlayStore(settings.policy_overlays_path)
    cp_path = settings.control_plane_sqlite_path
    audit_log = PolicyAuditLog(cp_path)
    forecast_store = SupplyForecastStore(cp_path)
    hardgate_store = EnforcementHardgateStore(cp_path)
    label_metrics_store = LabelMetricsStore(cp_path)
    operating_point_cfg = load_policy(settings.operating_point_path)
    ticket_store = LabelTicketStore(
        settings.label_tickets_path,
        stream_path=settings.label_tickets_stream_path,
    )
    chain_store = DriverChainStore(settings.driver_chains_path)
    baseline_store = EntityBaselineStore(settings.entity_baselines_path)
    cancel_stats_store = EntityCancelStatsStore(settings.entity_cancel_stats_path)
    device_store = DeviceIntegrityStore(settings.device_integrity_path)
    device_graph_store = DeviceGraphStore(settings.device_graph_path)
    chat_store = ChatSignalStore(settings.chat_signals_path)
    anomaly_store = EntityAnomalyStore(settings.entity_anomaly_path)
    outcome_store = OutcomeStore(settings.outcomes_path)
    reg = registry or ModelRegistry(settings.models_sqlite_path, settings.models_root)
    metrics = shadow_metrics or ShadowMetricsStore(settings.shadow_metrics_path)
    canary_ctrl = canary or CanaryController(
        settings.canary_sqlite_path,
        reg,
        metrics,
        gates=gates,
        thresholds={k: float(v) for k, v in policy["thresholds"].items()},
    )
    queue = make_job_queue(settings)
    lock_path = settings.control_plane_lock_path.strip() or str(
        Path(settings.control_plane_sqlite_path).with_suffix(".lock")
    )
    leader = FileLeaderLock(lock_path)
    control_loop = ControlPlaneLoop(
        debounce_seconds=settings.metrics_debounce_seconds,
        tick_seconds=settings.control_plane_tick_seconds,
        run_kwargs={
            "settings": settings,
            "policy": policy,
            "guardrails": guardrails,
            "overlays": overlay_store,
            "audit": audit_log,
            "forecast": forecast_store,
            "hardgates": hardgate_store,
            "label_metrics": label_metrics_store,
            "op_cfg": operating_point_cfg,
        },
        table=table_pub,
        tickets=ticket_store,
        leader=leader,
    )

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
                    tickets=ticket_store,
                    label_metrics=label_metrics_store,
                    driver_chains=chain_store,
                    baselines=baseline_store,
                    cancel_stats=cancel_stats_store,
                    devices=device_store,
                    device_graph=device_graph_store,
                    chat_store=chat_store,
                    anomalies=anomaly_store,
                    outcomes=outcome_store,
                )
            )
        control_loop.start()
        try:
            yield
        finally:
            await control_loop.stop()
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
    app.state.audit = audit_log
    app.state.forecast = forecast_store
    app.state.hardgates = hardgate_store
    app.state.label_metrics = label_metrics_store
    app.state.operating_point_cfg = operating_point_cfg
    app.state.tickets = ticket_store
    app.state.driver_chains = chain_store
    app.state.baselines = baseline_store
    app.state.cancel_stats = cancel_stats_store
    app.state.devices = device_store
    app.state.device_graph = device_graph_store
    app.state.chat_store = chat_store
    app.state.anomalies = anomaly_store
    app.state.outcomes = outcome_store
    app.state.control_loop = control_loop
    app.state.gates = gates
    app.state.queue = queue
    app.state.registry = reg
    app.state.shadow_metrics = metrics
    app.state.canary = canary_ctrl
    app.include_router(router)
    return app


app = create_app()
