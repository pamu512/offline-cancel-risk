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
    return TunerContext(
        base_policy=load_policy("config/policy.default.yaml"),
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


def test_tuner_rejects_when_no_candidate_in_peak_precision_gate(tmp_path: Path):
    # Scores barely separable; peak min_precision=0.85 hard to hit with noise
    assessments = []
    feedback = []
    for i in range(10):
        oid = f"P{i}"
        # half positive labels with mid scores → many FPs at low thr
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
    assert offline["reason"] in {
        "no_candidate_in_gates",
        "insufficient_labels",
        "f1_lift_below_min",
        "holdout_f1_lift_below_min",
    }


def test_tuner_applies_overlay_when_f1_improves_inside_gates(tmp_path: Path):
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
        min_labeled=10,
    )
    ctx.overlays = overlays
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["action"] == "apply"
    assert offline["decision"] == "accepted"
    stored = overlays.get("PH", "MNL")
    assert stored is not None
    assert stored["thresholds"]["cancelled_offline"] < 0.99


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
    )
    decisions = run_tuner(ctx)
    offline = next(d for d in decisions if d["head"] == "cancelled_offline")
    assert offline["decision"] == "rejected"
    assert offline["reason"] == "no_candidate_in_gates"
