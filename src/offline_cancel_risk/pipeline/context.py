"""Mutable assess context shared across pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from offline_cancel_risk.adapters.gps import GpsClient
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.baselines.store import EntityBaselineStore
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.features.driver_chains import DriverChainStore
from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.models.canary import CanaryController
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.outcomes.store import OutcomeStore
from offline_cancel_risk.pipeline.window import GpsWindowResult
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.ports import (
    AssessmentStore,
    ChatSignalPort,
    DeviceGraphPort,
    DeviceIntegrityPort,
    EntityAnomalyPort,
    StreamPublisher,
)


@dataclass
class AssessContext:
    req: AssessRequest
    policy: dict[str, Any]
    gps_client: GpsClient
    stream: StreamPublisher
    table: AssessmentStore
    model_version: str = "none"
    generation: int = 1
    registry: ModelRegistry | None = None
    shadow_metrics: ShadowMetricsStore | None = None
    canary: CanaryController | None = None
    overlays: PolicyOverlayStore | None = None
    tickets: LabelTicketStore | None = None
    bias_hints: dict[str, str] | None = None
    driver_chains: DriverChainStore | None = None
    baselines: EntityBaselineStore | None = None
    cancel_stats: EntityCancelStatsStore | None = None
    devices: DeviceIntegrityPort | None = None
    device_graph: DeviceGraphPort | None = None
    chat_store: ChatSignalPort | None = None
    anomalies: EntityAnomalyPort | None = None
    outcomes: OutcomeStore | None = None

    # prepare / serving
    phash: str = ""
    serving_model_id: str = "none"
    champion_rec: Any = None
    canary_state: Any = None
    use_canary: bool = False

    # geometry
    gps_unavailable: bool = False
    sparse: bool = False
    window: GpsWindowResult | None = None
    gps_window: dict[str, Any] = field(default_factory=dict)
    stops: list[tuple[float, float]] = field(default_factory=list)
    confidence_list: list[float] = field(default_factory=list)
    final_stop_confidence: float = 0.0
    dwell_fraction: float = 0.0
    dwell_masks: list[bool] = field(default_factory=list)
    sequence_score: float = 0.0
    sequence_reasons: list[str] = field(default_factory=list)
    pickup_drop_meta: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    device_eval: dict[str, Any] = field(default_factory=dict)
    replacement: Any = None
    lineage_id: str = ""
    stage: str = "unknown"
    stage_meta: dict[str, Any] = field(default_factory=dict)
    after_pickup: bool = False
    progress: dict[str, Any] = field(default_factory=dict)
    near_dest: bool = False
    driver_chain_count: int = 1

    # entity signals
    cancel_rate: float | None = None
    driver_cancel_count: int = 0
    pair_cancel_count: int = 0
    marketplace_signals: list[str] = field(default_factory=list)
    marketplace_meta: dict[str, Any] = field(default_factory=dict)
    graph_meta: dict[str, Any] = field(default_factory=dict)
    device_graph_signals: list[str] = field(default_factory=list)
    chat_eval: dict[str, Any] = field(default_factory=dict)
    abuse_score: float = 0.0
    abuse_reasons: list[str] = field(default_factory=list)
    anomaly_eval: dict[str, Any] = field(default_factory=dict)
    theft_score: float = 0.0
    theft_reasons: list[str] = field(default_factory=list)

    # score / result
    features: dict[str, Any] = field(default_factory=dict)
    rule_scores: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    ml_feature_vec: dict[str, float] = field(default_factory=dict)
    ml_scores: dict[str, float | None] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    scores_raw: dict[str, float] = field(default_factory=dict)
    flags: dict[str, int] = field(default_factory=dict)
    baseline_meta: dict[str, dict[str, object]] = field(default_factory=dict)
    shadow_scores: dict[str, Any] = field(default_factory=dict)
    model_roles: dict[str, str] = field(default_factory=dict)
    serve_model_id: str = "none"
    ear: dict[str, float] = field(default_factory=dict)
    attention: float = 0.0
    ear_meta: dict[str, Any] = field(default_factory=dict)
    result: AssessmentResult | None = None

    @property
    def points(self) -> list[GpsPoint]:
        return list(self.window.points) if self.window is not None else []
