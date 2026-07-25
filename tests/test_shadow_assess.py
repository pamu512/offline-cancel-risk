from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.models.bundle import HEAD_NAMES
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import load_policy


def _bundle(tmp: Path, model_id: str, bias: float) -> Path:
    """Tiny model: predicts ~bias for all heads regardless of input (via intercept)."""
    rng = np.random.default_rng(abs(hash(model_id)) % (2**31))
    x = rng.random((30, 5))
    y = np.full((30, 3), bias)
    model = MultiOutputRegressor(Ridge(alpha=0.01)).fit(x, y)
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


def _req() -> AssessRequest:
    return AssessRequest(
        order_display_id="ORD-SHADOW-1",
        driver_id=42,
        cancel_ts="2024-01-01T12:00:00Z",
        assign_ts="2024-01-01T09:00:00Z",
        latlong="1.0|2.0,1.1|2.1",
        path_point_num=1,
        order_status="CANCELLED",
        category="FOOD",
        order_value=100.0,
        currency="SGD",
        next_driver_no_order=True,
    )


@pytest.mark.asyncio
async def test_shadow_does_not_change_serving_flags(tmp_path: Path):
    policy = load_policy("config/policy.default.yaml")
    reg = ModelRegistry(tmp_path / "reg.db", tmp_path / "models")
    metrics = ShadowMetricsStore(tmp_path / "metrics.db")
    reg.sideload(_bundle(tmp_path, "champ", 0.1), role="champion")

    start = datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(1.0, 2.0, (start + timedelta(minutes=i)).isoformat(), 0.2)
        for i in range(40)
    ]
    gps = FakeGpsClient(points)
    stream = JsonlStreamPublisher(tmp_path / "events.jsonl")
    table = SqliteTablePublisher(tmp_path / "assess.db")

    champ_only = await assess_order(
        _req(),
        gps,
        policy,
        stream=stream,
        table=table,
        generation=1,
        registry=reg,
        shadow_metrics=metrics,
    )
    reg.sideload(_bundle(tmp_path, "shadow_hi", 0.99), role="shadow")
    with_shadow = await assess_order(
        _req(),
        gps,
        policy,
        stream=stream,
        table=table,
        generation=2,
        registry=reg,
        shadow_metrics=metrics,
    )

    assert with_shadow.model_version == "champ"
    assert "shadow_hi" in with_shadow.shadow_scores
    assert with_shadow.model_roles["shadow_hi"] == "shadow"
    assert with_shadow.flags.model_dump() == champ_only.flags.model_dump()
    assert with_shadow.scores.model_dump() == champ_only.scores.model_dump()
    assert (
        with_shadow.shadow_scores["shadow_hi"].selective_theft
        > with_shadow.scores.selective_theft
    )
    assert metrics.count_for_shadow("shadow_hi") == 1
