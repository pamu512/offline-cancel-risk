from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.models.bundle import HEAD_NAMES
from offline_cancel_risk.models.canary import CanaryController, in_canary_cohort
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry


def _bundle(tmp: Path, model_id: str) -> Path:
    rng = np.random.default_rng(2)
    x = rng.random((20, 5))
    y = np.clip(np.hstack([x[:, :1]] * 3), 0, 1)
    model = MultiOutputRegressor(Ridge()).fit(x, y)
    root = tmp / model_id
    root.mkdir(parents=True)
    art = root / "model.joblib"
    joblib.dump({"model": model, "heads": list(HEAD_NAMES)}, art)
    schema = {
        "version": "v1",
        "features": [
            {"name": n, "dtype": "float"}
            for n in [
                "final_stop_confidence",
                "sequence_score",
                "dwell_fraction",
                "abuse_score",
                "theft_score",
            ]
        ],
    }
    (root / "feature_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (root / "metrics_baseline.json").write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(art.read_bytes()).hexdigest()
    (root / "model.json").write_text(
        json.dumps(
            {
                "model_id": model_id,
                "format": "joblib",
                "heads": list(HEAD_NAMES),
                "feature_schema_version": "v1",
                "checksum_sha256": digest,
                "created_at": "2026-07-25T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return root


def test_in_canary_cohort_stable():
    assert in_canary_cohort("ORD-1", 100) is True
    assert in_canary_cohort("ORD-1", 0) is False
    a = in_canary_cohort("stable-id", 50)
    b = in_canary_cohort("stable-id", 50)
    assert a == b


def test_auto_canary_starts_when_ready(tmp_path: Path):
    reg = ModelRegistry(tmp_path / "r.db", tmp_path / "models")
    metrics = ShadowMetricsStore(tmp_path / "m.db")
    reg.sideload(_bundle(tmp_path, "champ"), role="champion")
    reg.sideload(_bundle(tmp_path, "chal"), role="shadow")
    for i in range(10):
        metrics.record(
            order_display_id=f"O{i}",
            champion_model_id="champ",
            shadow_model_id="chal",
            champion_scores={
                "cancelled_offline": 0.8 if i < 5 else 0.1,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            shadow_scores={
                "cancelled_offline": 0.8 if i < 8 else 0.1,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            order_value=10.0,
        )
    ctrl = CanaryController(
        tmp_path / "c.db",
        reg,
        metrics,
        gates={
            "min_shadow_assessments": 10,
            "max_fp_dollar_lift": 0.0,
            "max_fp_dollar_abs": 100.0,
            "min_catch_lift": 0.02,
            "auto_canary": True,
            "canary_pct": 5,
            "canary_hours": 24,
        },
        thresholds={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.75,
            "selective_theft": 0.75,
        },
    )
    status = ctrl.evaluate_and_maybe_start_canary("chal")
    assert status.promotion_ready == 1
    assert ctrl.active() is not None
    assert reg.get("chal").role == "canary"


def test_abort_rolls_back(tmp_path: Path):
    reg = ModelRegistry(tmp_path / "r.db", tmp_path / "models")
    metrics = ShadowMetricsStore(tmp_path / "m.db")
    reg.sideload(_bundle(tmp_path, "champ"), role="champion")
    reg.sideload(_bundle(tmp_path, "chal"), role="shadow")
    ctrl = CanaryController(
        tmp_path / "c.db",
        reg,
        metrics,
        gates={"auto_canary": False, "canary_pct": 5, "canary_hours": 24},
        thresholds={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.75,
            "selective_theft": 0.75,
        },
    )
    ctrl.start_canary("chal")
    ctrl.abort()
    assert ctrl.active() is None
    assert reg.get("chal").role == "failed_canary"
