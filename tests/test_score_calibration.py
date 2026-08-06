from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.calibrate import (
    CalibrationFitContext,
    CalibrationRunStore,
    CalibratorStore,
    run_calibration_fit,
)
from offline_cancel_risk.scoring.calibration import (
    expected_calibration_error,
    fit_calibrator,
    predict_calibrated,
)
from offline_cancel_risk.settings import load_policy


def test_ece_perfect_is_near_zero():
    probs = [0.1, 0.1, 0.9, 0.9]
    labels = [0, 0, 1, 1]
    assert expected_calibration_error(probs, labels, n_bins=2) <= 0.1


def test_fit_picks_platt_below_threshold():
    rng = np.random.default_rng(0)
    xs = rng.uniform(0, 1, 40).tolist()
    ys = [1 if x > 0.5 else 0 for x in xs]
    model = fit_calibrator(xs, ys, platt_max_n=80)
    assert model["method"] == "platt"
    assert 0.0 <= predict_calibrated(model, 0.9) <= 1.0


def test_fit_picks_isotonic_at_or_above_threshold():
    rng = np.random.default_rng(1)
    xs = rng.uniform(0, 1, 100).tolist()
    ys = [1 if x > 0.4 else 0 for x in xs]
    model = fit_calibrator(xs, ys, platt_max_n=80)
    assert model["method"] == "isotonic"


def test_calibrator_store_roundtrip(tmp_path: Path):
    store = CalibratorStore(tmp_path / "cal.db")
    store.upsert(
        region_code="PH",
        city_code="MNL",
        head="cancelled_offline",
        method="platt",
        params={"coef": [2.0], "intercept": [-1.0]},
        ece=0.02,
        support=40,
    )
    row = store.get("PH", "MNL", "cancelled_offline")
    assert row is not None
    assert row["method"] == "platt"
    assert row["ece"] == 0.02


def _assess(oid: str, raw: float) -> dict:
    scores = {"cancelled_offline": raw, "cancel_abuse": 0.1, "selective_theft": 0.1}
    return {
        "order_display_id": oid,
        "region_code": "PH",
        "city_code": "MNL",
        "scores": scores,
        "scores_raw": scores,
    }


def _policy(*, mode: str = "shadow", cooldown_minutes: int = 0) -> dict[str, Any]:
    policy = deepcopy(load_policy("config/policy.default.yaml"))
    # platt_max_n low → isotonic; narrow S band + sklearn Platt L2 won't clear max_ece.
    policy["calibration"] = {
        "mode": mode,
        "on_tick": False,
        "min_labeled": 30,
        "platt_max_n": 10,
        "holdout_fraction": 0.3,
        "max_ece": 0.05,
        "cooldown_minutes": cooldown_minutes,
        "ece_bins": 10,
    }
    return policy


def _fit_ctx(
    tmp_path: Path,
    *,
    assessments: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    mode: str = "shadow",
    cooldown_minutes: int = 0,
) -> CalibrationFitContext:
    db = tmp_path / "cp.db"
    return CalibrationFitContext(
        base_policy=_policy(mode=mode, cooldown_minutes=cooldown_minutes),
        audit=PolicyAuditLog(db),
        calibrators=CalibratorStore(tmp_path / "cal.db"),
        assessments=assessments,
        feedback=feedback,
        region_code="PH",
        city_code="MNL",
        run_store=CalibrationRunStore(db),
    )


def _separable_s_cohort(n: int = 40) -> tuple[list[dict], list[dict]]:
    assessments: list[dict] = []
    feedback: list[dict] = []
    for i in range(n):
        high = i < n // 2
        raw = 0.97 if high else 0.86
        oid = f"o{i:03d}"
        assessments.append(_assess(oid, raw))
        feedback.append(
            {
                "order_display_id": oid,
                "labels": {"cancelled_offline": 1 if high else 0},
            }
        )
    return assessments, feedback


def test_fit_rejects_insufficient_pattern_labels(tmp_path: Path):
    assessments = [_assess(f"o{i}", 0.9) for i in range(5)]
    feedback = [
        {"order_display_id": f"o{i}", "labels": {"cancelled_offline": i % 2}}
        for i in range(5)
    ]
    ctx = _fit_ctx(tmp_path, assessments=assessments, feedback=feedback)
    report = run_calibration_fit(ctx)
    assert report["decision"] == "rejected"
    assert "insufficient" in report["reason"]
    assert ctx.calibrators.get("PH", "MNL", "cancelled_offline") is None


def test_fit_persists_when_ece_ok(tmp_path: Path):
    assessments, feedback = _separable_s_cohort(40)
    ctx = _fit_ctx(tmp_path, assessments=assessments, feedback=feedback, mode="shadow")
    report = run_calibration_fit(ctx)
    assert report["decision"] == "fitted"
    row = ctx.calibrators.get("PH", "MNL", "cancelled_offline")
    assert row is not None
    assert row["support"] >= 30
    assert float(row["ece"]) <= 0.05
    latest = ctx.run_store.latest("PH", "MNL")
    assert latest is not None
    assert latest["decision"] == "fitted"


def test_fit_rejects_high_ece(tmp_path: Path, monkeypatch):
    assessments, feedback = _separable_s_cohort(40)
    ctx = _fit_ctx(tmp_path, assessments=assessments, feedback=feedback)
    ctx.calibrators.upsert(
        region_code="PH",
        city_code="MNL",
        head="cancelled_offline",
        method="platt",
        params={"coef": [1.0], "intercept": [0.0]},
        ece=0.01,
        support=40,
    )
    monkeypatch.setattr(
        "offline_cancel_risk.control_plane.calibrate.expected_calibration_error",
        lambda *a, **k: 0.5,
    )
    report = run_calibration_fit(ctx)
    assert report["decision"] == "rejected"
    assert "ece" in report["reason"]
    prior = ctx.calibrators.get("PH", "MNL", "cancelled_offline")
    assert prior is not None
    assert prior["ece"] == 0.01
    assert prior["params"]["coef"] == [1.0]


def test_fit_respects_cooldown(tmp_path: Path):
    assessments, feedback = _separable_s_cohort(40)
    ctx = _fit_ctx(
        tmp_path,
        assessments=assessments,
        feedback=feedback,
        cooldown_minutes=1440,
    )
    ctx.audit.append(
        actor="calibrator",
        action="apply",
        region_code="PH",
        city_code="MNL",
        decision="accepted",
        reason="prior_fit",
        after={
            "calibration": {
                "cancelled_offline": {"method": "platt", "ece": 0.02, "support": 40}
            }
        },
    )
    report = run_calibration_fit(ctx)
    assert report["decision"] == "rejected"
    assert report["reason"] == "cooldown"
    assert ctx.calibrators.get("PH", "MNL", "cancelled_offline") is None
