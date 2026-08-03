from pathlib import Path

from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.tuner import TunerContext, run_tuner
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.settings import load_policy

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _scores(offline: float) -> dict[str, float]:
    return {
        "cancelled_offline": offline,
        "cancel_abuse": 0.1,
        "selective_theft": 0.1,
    }


def _labels(offline: int) -> dict[str, int]:
    return {
        "cancelled_offline": offline,
        "cancel_abuse": 0,
        "selective_theft": 0,
    }


def _ctx(
    tmp_path: Path,
    *,
    assessments: list[dict],
    feedback: list[dict],
    supply: float,
    demand: float,
    hour_cap: int = 10_000,
    min_labeled: int = 2,
    min_pattern_support: int | None = None,
) -> TunerContext:
    db = tmp_path / "cp.db"
    forecast = SupplyForecastStore(db)
    forecast.upsert(
        [
            {
                "region_code": "PH",
                "city_code": "MNL",
                "period_start": "2020-01-01T00:00:00Z",
                "period_end": "2099-01-01T00:00:00Z",
                "forecast_supply": supply,
                "forecast_demand": demand,
                "source": "test",
            }
        ]
    )
    hardgates = EnforcementHardgateStore(db)
    hardgates.upsert(
        "PH", "MNL", window="hour", max_enforcements=hour_cap, actor="test"
    )
    policy = load_policy("config/policy.default.yaml")
    policy["learning"] = dict(policy.get("learning") or {})
    policy["learning"]["min_pattern_support"] = (
        min_labeled if min_pattern_support is None else min_pattern_support
    )
    policy["learning"]["min_pattern_recall"] = 0.2
    policy["learning"]["target_precision"] = 0.98
    return TunerContext(
        base_policy=policy,
        guardrails=load_policy("config/policy_guardrails.default.yaml"),
        overlays=PolicyOverlayStore(tmp_path / "overlays.db"),
        audit=PolicyAuditLog(db),
        forecast=forecast,
        hardgates=hardgates,
        op_cfg=load_policy("config/operating_point.default.yaml"),
        assessments=assessments,
        feedback=feedback,
        region_code="PH",
        city_code="MNL",
        min_labeled=min_labeled,
        cooldown_minutes=0,
        min_f1_lift=0.01,
        threshold_step=0.05,
        at_ts="2026-07-25T12:00:00Z",
    )


def test_tuner_rejects_when_no_pattern_cohort(tmp_path: Path):
    # Mid scores fall outside pattern strata (score_min 0.85) → insufficient_pattern_labels
    assessments = []
    feedback = []
    for i in range(10):
        oid = f"P{i}"
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.6 if i % 2 == 0 else 0.55),
            }
        )
        feedback.append(
            {
                "order_display_id": oid,
                "labels": _labels(1 if i < 3 else 0),
            }
        )
    ctx = _ctx(tmp_path, assessments=assessments, feedback=feedback, supply=70, demand=100)
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["decision"] == "rejected"
    assert offline["reason"] == "insufficient_pattern_labels"


def test_tuner_applies_overlay_when_pattern_recall_improves(tmp_path: Path):
    assessments = []
    feedback = []
    for i in range(20):
        oid = f"S{i}"
        pos = i < 10
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.95 if pos else 0.1),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(1 if pos else 0)})
    # Start from a bad threshold via overlay so lift is possible
    overlays = PolicyOverlayStore(tmp_path / "overlays.db")
    overlays.upsert("PH", "MNL", {"thresholds": {"cancelled_offline": 0.99}})
    ctx = _ctx(
        tmp_path,
        assessments=assessments,
        feedback=feedback,
        supply=150,
        demand=100,  # surplus
        min_labeled=5,
        min_pattern_support=5,
    )
    ctx.overlays = overlays
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["action"] == "apply"
    assert offline["decision"] == "accepted"
    assert offline["reason"] == "holdout_pattern_recall_lift"
    stored = overlays.get("PH", "MNL")
    assert stored is not None
    assert stored["thresholds"]["cancelled_offline"] < 0.99


def test_tuner_prefers_high_tau_for_pattern_precision(tmp_path: Path):
    """High τ hits 0.98 precision on S even when a low τ would raise global F1."""
    assessments = []
    feedback = []
    # Pattern cohort: 16 clear positives at 0.92 + 4 hard negatives at 0.86
    for i in range(16):
        oid = f"TP{i}"
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.92),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(1)})
    for i in range(4):
        oid = f"FP{i}"
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.86),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(0)})
    # Outside S: many true positives at mid score — inflate global F1 if τ is low
    for i in range(30):
        oid = f"G{i}"
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.6),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(1)})

    overlays = PolicyOverlayStore(tmp_path / "overlays.db")
    overlays.upsert("PH", "MNL", {"thresholds": {"cancelled_offline": 0.95}})
    ctx = _ctx(
        tmp_path,
        assessments=assessments,
        feedback=feedback,
        supply=150,
        demand=100,
        min_labeled=5,
        min_pattern_support=5,
    )
    ctx.overlays = overlays
    ctx.holdout_fraction = 0.0  # evaluate on full train for a stable τ pick
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["decision"] == "accepted"
    thr = float(offline["overlay"]["thresholds"]["cancelled_offline"])
    # Must sit above the hard negatives in S (0.86) to keep Precision_S ≥ 0.98
    assert thr > 0.86


def test_tuner_rejects_when_hourly_hardgate_breached(tmp_path: Path):
    assessments = []
    feedback = []
    for i in range(12):
        oid = f"H{i}"
        assessments.append(
            {
                "order_display_id": oid,
                "region_code": "PH",
                "city_code": "MNL",
                "scores": _scores(0.9),
            }
        )
        feedback.append({"order_display_id": oid, "labels": _labels(1)})
    ctx = _ctx(
        tmp_path,
        assessments=assessments,
        feedback=feedback,
        supply=150,
        demand=100,
        hour_cap=0,
        min_labeled=5,
        min_pattern_support=5,
    )
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["decision"] == "rejected"
    assert offline["reason"] == "no_candidate_in_gates"
