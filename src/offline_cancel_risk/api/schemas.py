from pydantic import BaseModel, Field


class OutcomeIngestRequest(BaseModel):
    order_display_id: str
    outcome: str
    amount: float | None = None
    head: str | None = None
    region_code: str | None = None
    city_code: str | None = None
    occurred_at: str | None = None


class AssessRequest(BaseModel):
    order_display_id: str
    driver_id: int
    cancel_ts: str
    assign_ts: str
    latlong: str
    path_point_num: int
    order_status: str
    category: str
    order_value: float
    currency: str
    replacement_order_id: str | None = None
    replacement_placed_at: str | None = None
    replacement_latlong: str | None = None
    replacement_status: str | None = None
    reassign_cancel_events: list[dict] = Field(default_factory=list)
    next_driver_no_order: bool | None = None
    user_id: int | None = None
    merchant_id: int | None = None
    device_id: str | None = None
    city_code: str | None = None
    region_code: str | None = None
    # Optional Downstream device-intel ingest (spoof/root/risk_score).
    device_risk: dict[str, bool | float | str] | None = None
    # Marketplace funnel (slice 1). accepted_at defaults to assign_ts in assess.
    accepted_at: str | None = None
    cancel_reason_code: str | None = None
    cancel_with_cause: bool | None = None
    # Extra accept/complete/cancel rows for the same driver (window fill).
    marketplace_events: list[dict] = Field(default_factory=list)
    # Downstream chat/force-cancel flags (no NLP here). See POST /v1/chat-signals.
    chat_signals: dict[str, bool | float | str] | None = None
    # Optional stop-presence priors (skipped → unknown / factor 1.0). Market overlays tune tables.
    place_class: str | None = None
    vehicle_class: str | None = None
    # When true: bump assessment_generation and mark prior generations provisional.
    force_reassess: bool = False


class ThreeHeadScores(BaseModel):
    cancelled_offline: float
    cancel_abuse: float
    selective_theft: float


class ThreeHeadFlags(BaseModel):
    cancelled_offline: int
    cancel_abuse: int
    selective_theft: int


class ThreeHeadMlScores(BaseModel):
    cancelled_offline: float | None
    cancel_abuse: float | None
    selective_theft: float | None


class ExpectedRevenueAtRisk(BaseModel):
    cancelled_offline: float
    cancel_abuse: float
    selective_theft: float
    total: float


class AssessmentResult(BaseModel):
    order_display_id: str
    driver_id: int
    scores: ThreeHeadScores
    flags: ThreeHeadFlags
    expected_revenue_at_risk: ExpectedRevenueAtRisk
    attention_score: float
    reasons: list[str]
    rule_scores: ThreeHeadScores
    ml_scores: ThreeHeadMlScores
    gps_window: dict[str, str | int | float | bool]
    lineage_id: str
    assessment_generation: int
    provisional: bool
    policy_hash: str
    model_version: str
    twin_version: str
    graph_version: str
    feature_vector_ref: str
    assessed_at: str
    shadow_scores: dict[str, ThreeHeadScores] = Field(default_factory=dict)
    model_roles: dict[str, str] = Field(default_factory=dict)
    city_code: str | None = None
    region_code: str | None = None
    routing: dict[str, str | float] = Field(default_factory=dict)
    # Pre-baseline-discount blended scores (learning / warehouse joins).
    scores_raw: ThreeHeadScores | None = None
    baseline_meta: dict[str, dict[str, object]] = Field(default_factory=dict)
    cancel_stage: str | None = None
    evidence: list[dict[str, object]] = Field(default_factory=list)
    ear_meta: dict = Field(default_factory=dict)
