from pydantic import BaseModel, Field


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


class AssessmentResult(BaseModel):
    order_display_id: str
    driver_id: int
    scores: dict[str, float]
    flags: dict[str, int]
    expected_revenue_at_risk: dict[str, float]
    attention_score: float
    reasons: list[str]
    rule_scores: dict[str, float]
    ml_scores: dict[str, float | None]
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
