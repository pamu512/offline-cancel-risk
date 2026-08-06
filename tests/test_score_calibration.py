from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.calibrate import (
    CalibrationFitContext,
    CalibrationRunStore,
    CalibratorStore,
    run_calibration_fit,
)
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.main import create_app
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.scoring.calibration import (
    apply_calibrated_score,
    brier_score,
    expected_calibration_error,
    fit_calibrator,
    predict_calibrated,
)
from offline_cancel_risk.settings import Settings, load_policy


def test_ece_perfect_is_near_zero():
    probs = [0.1, 0.1, 0.9, 0.9]
    labels = [0, 0, 1, 1]
    assert expected_calibration_error(probs, labels, n_bins=2, strategy="equal") <= 0.1


def test_quantile_ece_exposes_miscalibration_equal_width_hides():
    """Scores clustered in one equal-width bin can fake a low ECE; quantile does not."""
    probs = [0.501 + 0.001 * i for i in range(30)]  # all in [0.5, 0.6)
    labels = [0] * 15 + [1] * 15
    e_equal = expected_calibration_error(probs, labels, n_bins=10, strategy="equal")
    e_q = expected_calibration_error(probs, labels, n_bins=5, strategy="quantile")
    assert e_equal < 0.05
    assert e_q > 0.2


def test_brier_and_apply_discount():
    assert brier_score([0.0, 1.0], [0, 1]) == pytest.approx(0.0)
    assert apply_calibrated_score(
        p=0.8, scores_raw=1.0, scores_current=0.5
    ) == pytest.approx(0.4)


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
    policy["calibration"] = {
        "mode": mode,
        "on_tick": False,
        "min_labeled": 30,
        "platt_max_n": 10,
        "holdout_fraction": 0.3,
        "max_ece": 0.05,
        "max_brier": 0.25,
        "cooldown_minutes": cooldown_minutes,
        "ece_bins": 10,
        "ece_strategy": "quantile",
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


def _separable_full_support(n: int = 40) -> tuple[list[dict], list[dict]]:
    assessments: list[dict] = []
    feedback: list[dict] = []
    for i in range(n):
        high = i < n // 2
        raw = 0.92 if high else 0.15
        oid = f"o{i:03d}"
        assessments.append(_assess(oid, raw))
        feedback.append(
            {
                "order_display_id": oid,
                "labels": {"cancelled_offline": 1 if high else 0},
            }
        )
    return assessments, feedback


def test_fit_rejects_insufficient_labels(tmp_path: Path):
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


def test_fit_rejects_empty_holdout(tmp_path: Path):
    assessments, feedback = _separable_full_support(40)
    ctx = _fit_ctx(tmp_path, assessments=assessments, feedback=feedback)
    ctx.base_policy["calibration"]["holdout_fraction"] = 0.0
    report = run_calibration_fit(ctx)
    assert report["decision"] == "rejected"
    assert report["reason"] == "empty_holdout"


def test_fit_persists_when_holdout_ok(tmp_path: Path):
    assessments, feedback = _separable_full_support(40)
    ctx = _fit_ctx(tmp_path, assessments=assessments, feedback=feedback, mode="shadow")
    report = run_calibration_fit(ctx)
    assert report["decision"] == "fitted"
    row = ctx.calibrators.get("PH", "MNL", "cancelled_offline")
    assert row is not None
    assert row["support"] >= 30
    assert float(row["ece"]) <= 0.05
    head = report["heads"]["cancelled_offline"]
    assert "brier" in head
    assert float(head["brier"]) <= 0.25
    latest = ctx.run_store.latest("PH", "MNL")
    assert latest is not None
    assert latest["decision"] == "fitted"


def test_fit_rejects_high_ece(tmp_path: Path, monkeypatch):
    assessments, feedback = _separable_full_support(40)
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
    assessments, feedback = _separable_full_support(40)
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


def _pickup_cluster(n: int = 40) -> list[GpsPoint]:
    base = datetime(2024, 1, 1, 10, 0, 0)
    return [
        GpsPoint(
            lat=14.55 + (i % 5) * 1e-5,
            lon=121.03 + (i % 3) * 1e-5,
            ts=(base + timedelta(minutes=i * 2)).strftime("%Y-%m-%d %H:%M:%S"),
            speed_mps=0.3,
        )
        for i in range(n)
    ]


def _assess_req(oid: str) -> AssessRequest:
    return AssessRequest(
        order_display_id=oid,
        driver_id=42,
        cancel_ts="2024-01-01 11:20:00",
        assign_ts="2024-01-01 10:00:00",
        latlong="14.55|121.03,14.65|121.08",
        path_point_num=2,
        order_status="CANCELLED",
        category="FOOD",
        order_value=800.0,
        currency="PHP",
        region_code="PH",
        city_code="MNL",
    )


def _seed_calibrator(store: CalibratorStore) -> None:
    store.upsert(
        region_code="PH",
        city_code="MNL",
        head="cancelled_offline",
        method="isotonic",
        params={"X_thresholds": [0.0, 1.0], "y_thresholds": [0.1, 0.95]},
        ece=0.02,
        support=40,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sqlite_path=str(tmp_path / "assess.db"),
        stream_path=str(tmp_path / "stream.jsonl"),
        policy_overlays_path=str(tmp_path / "overlays.db"),
        control_plane_sqlite_path=str(tmp_path / "cp.db"),
        assess_gps_cache_path=str(tmp_path / "gps_cache.db"),
        label_tickets_path=str(tmp_path / "tickets.db"),
        label_tickets_stream_path=str(tmp_path / "tickets.jsonl"),
        driver_chains_path=str(tmp_path / "chains.db"),
        entity_baselines_path=str(tmp_path / "baselines.db"),
        entity_cancel_stats_path=str(tmp_path / "cancel_stats.db"),
        device_integrity_path=str(tmp_path / "devices.db"),
        device_graph_path=str(tmp_path / "device_graph.db"),
        chat_signals_path=str(tmp_path / "chat.db"),
        entity_anomaly_path=str(tmp_path / "anomaly.db"),
        outcomes_path=str(tmp_path / "outcomes.db"),
        models_sqlite_path=str(tmp_path / "models.db"),
        models_root=str(tmp_path / "model_files"),
        shadow_metrics_path=str(tmp_path / "shadow.db"),
        canary_sqlite_path=str(tmp_path / "canary.db"),
        calibrators_path=str(tmp_path / "calibrators.db"),
        sync_assess=True,
        control_plane_tick_seconds=0,
    )


@pytest.mark.asyncio
async def test_assess_shadow_keeps_scores_writes_meta(tmp_path: Path):
    store = CalibratorStore(tmp_path / "cal.db")
    _seed_calibrator(store)
    policy = deepcopy(load_policy("config/policy.default.yaml"))
    policy["calibration"] = {**_policy()["calibration"], "mode": "shadow"}
    gps = FakeGpsClient(_pickup_cluster())
    stream = JsonlStreamPublisher(stream_path=str(tmp_path / "s.jsonl"))
    table = SqliteTablePublisher(sqlite_path=str(tmp_path / "a.db"))

    baseline = await assess_order(
        _assess_req("CAL-SHADOW-A"), gps, policy, stream=stream, table=table
    )
    with_cal = await assess_order(
        _assess_req("CAL-SHADOW-B"),
        gps,
        policy,
        stream=stream,
        table=table,
        calibrators=store,
    )

    assert with_cal.scores.cancelled_offline == pytest.approx(
        baseline.scores.cancelled_offline
    )
    meta = with_cal.calibration_meta["cancelled_offline"]
    assert meta["applied"] is False
    assert "p" in meta
    assert meta["mode"] == "shadow"


@pytest.mark.asyncio
async def test_assess_apply_replaces_scores_keeps_raw(tmp_path: Path):
    store = CalibratorStore(tmp_path / "cal.db")
    _seed_calibrator(store)
    policy = deepcopy(load_policy("config/policy.default.yaml"))
    policy["calibration"] = {**_policy()["calibration"], "mode": "apply"}
    gps = FakeGpsClient(_pickup_cluster())
    stream = JsonlStreamPublisher(stream_path=str(tmp_path / "s.jsonl"))
    table = SqliteTablePublisher(sqlite_path=str(tmp_path / "a.db"))

    result = await assess_order(
        _assess_req("CAL-APPLY-1"),
        gps,
        policy,
        stream=stream,
        table=table,
        calibrators=store,
    )

    meta = result.calibration_meta["cancelled_offline"]
    assert meta["applied"] is True
    assert result.scores_raw is not None
    expected_p = predict_calibrated(
        {
            "method": "isotonic",
            "params": {"X_thresholds": [0.0, 1.0], "y_thresholds": [0.1, 0.95]},
        },
        float(result.scores_raw.cancelled_offline),
    )
    expected = apply_calibrated_score(
        p=expected_p,
        scores_raw=float(result.scores_raw.cancelled_offline),
        scores_current=float(result.scores_raw.cancelled_offline),  # no discount
    )
    assert result.scores.cancelled_offline == pytest.approx(expected)
    assert result.scores.cancelled_offline == pytest.approx(float(meta["score_applied"]))
    assert result.scores.cancelled_offline != pytest.approx(
        float(result.scores_raw.cancelled_offline)
    )


def test_calibrate_api(tmp_path: Path):
    app = create_app(
        gps_client=FakeGpsClient([]),
        settings=_settings(tmp_path),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/tuning/calibrate",
        json={"region_code": "PH", "city_code": "MNL", "mode": "shadow"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "rejected"
    assert "insufficient" in body["reason"]
    latest = client.get(
        "/v1/tuning/calibrate/latest",
        params={"region_code": "PH", "city_code": "MNL"},
    )
    assert latest.status_code == 200
    assert "insufficient" in latest.json()["reason"]
