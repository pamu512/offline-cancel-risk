"""Assess orchestrator: prepare → geometry → signals → score → publish."""

from __future__ import annotations

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.adapters.publishers import StreamPublisher, TablePublisher
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.features.anomaly import EntityAnomalyStore
from offline_cancel_risk.features.chat_signals import ChatSignalStore
from offline_cancel_risk.features.device_graph import DeviceGraphStore
from offline_cancel_risk.features.device_store import DeviceIntegrityStore
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.features.gps_cache import AssessGpsCache
from offline_cancel_risk.feedback.sampler import safe_inline_sample
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.models.canary import CanaryController, in_canary_cohort
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.outcomes.store import OutcomeStore
from offline_cancel_risk.pipeline.context import AssessContext
from offline_cancel_risk.pipeline.geometry import run_geometry_stage
from offline_cancel_risk.pipeline.idempotency import lookup_cached, make_idempotency_key
from offline_cancel_risk.pipeline.publish import run_publish_stage
from offline_cancel_risk.pipeline.score_build import ML_FEATURE_KEYS, run_score_stage
from offline_cancel_risk.pipeline.signals import run_signals_stage
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.service import resolved_policy_for_market
from offline_cancel_risk.scoring.policy import policy_hash

_MODEL_VERSION = "none"
_DEFAULT_GENERATION = 1


async def assess_order(
    req: AssessRequest,
    gps_client: GpsClient,
    policy: dict,
    *,
    stream: StreamPublisher,
    table: TablePublisher,
    model_version: str = _MODEL_VERSION,
    generation: int = _DEFAULT_GENERATION,
    registry: ModelRegistry | None = None,
    shadow_metrics: ShadowMetricsStore | None = None,
    canary: CanaryController | None = None,
    overlays: PolicyOverlayStore | None = None,
    tickets: LabelTicketStore | None = None,
    bias_hints: dict[str, str] | None = None,
    driver_chains: DriverChainStore | None = None,
    baselines: EntityBaselineStore | None = None,
    cancel_stats: EntityCancelStatsStore | None = None,
    devices: DeviceIntegrityStore | None = None,
    device_graph: DeviceGraphStore | None = None,
    chat_store: ChatSignalStore | None = None,
    anomalies: EntityAnomalyStore | None = None,
    outcomes: OutcomeStore | None = None,
    gps_cache: AssessGpsCache | None = None,
    feature_sink: dict[str, float] | None = None,
) -> AssessmentResult:
    if overlays is not None:
        policy = resolved_policy_for_market(
            policy,
            overlays,
            region_code=req.region_code,
            city_code=req.city_code,
        )
    if req.force_reassess:
        next_gen = getattr(table, "next_generation", None)
        if callable(next_gen):
            generation = int(next_gen(req.order_display_id))

    champion_rec = registry.get_champion() if registry is not None else None
    serving_model_id = (
        champion_rec.model_id if champion_rec is not None else model_version
    )
    canary_state = canary.active() if canary is not None else None
    use_canary = False
    if (
        canary_state is not None
        and registry is not None
        and in_canary_cohort(req.order_display_id, canary_state.canary_pct)
    ):
        use_canary = True
        serving_model_id = canary_state.challenger_model_id

    phash = policy_hash(policy)
    key = make_idempotency_key(
        req.order_display_id, phash, serving_model_id, generation
    )
    cached = lookup_cached(table, key)
    if cached is not None:
        safe_inline_sample(tickets, cached, policy, bias_hints=bias_hints)
        return cached

    ctx = AssessContext(
        req=req,
        policy=policy,
        gps_client=gps_client,
        stream=stream,
        table=table,
        model_version=model_version,
        generation=generation,
        registry=registry,
        shadow_metrics=shadow_metrics,
        canary=canary,
        overlays=overlays,
        tickets=tickets,
        bias_hints=bias_hints,
        driver_chains=driver_chains,
        baselines=baselines,
        cancel_stats=cancel_stats,
        devices=devices,
        device_graph=device_graph,
        chat_store=chat_store,
        anomalies=anomalies,
        outcomes=outcomes,
        gps_cache=gps_cache,
        phash=phash,
        serving_model_id=serving_model_id,
        champion_rec=champion_rec,
        canary_state=canary_state,
        use_canary=use_canary,
    )

    await run_geometry_stage(ctx)
    run_signals_stage(ctx)
    result = run_score_stage(ctx)
    if feature_sink is not None:
        feature_sink.update(
            {k: float(ctx.ml_feature_vec[k]) for k in ML_FEATURE_KEYS}
        )
    run_publish_stage(ctx)
    return result
