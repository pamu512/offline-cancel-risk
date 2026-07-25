from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.main import create_app
from offline_cancel_risk.models.bundle import HEAD_NAMES
from offline_cancel_risk.settings import Settings


def _bundle(tmp: Path, model_id: str) -> Path:
    rng = np.random.default_rng(3)
    x = rng.random((15, 5))
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


@pytest.mark.asyncio
async def test_models_sideload_and_list(tmp_path: Path):
    settings = Settings(
        sync_assess=True,
        sqlite_path=str(tmp_path / "a.db"),
        stream_path=str(tmp_path / "e.jsonl"),
        models_sqlite_path=str(tmp_path / "m.db"),
        models_root=str(tmp_path / "models"),
        shadow_metrics_path=str(tmp_path / "s.db"),
        canary_sqlite_path=str(tmp_path / "c.db"),
    )
    app = create_app(gps_client=FakeGpsClient([]), settings=settings)
    bundle = _bundle(tmp_path, "api_model")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/models", json={"bundle_path": str(bundle), "role": "shadow"}
        )
        assert r.status_code == 200
        assert r.json()["model_id"] == "api_model"
        listed = await ac.get("/v1/models")
        assert listed.status_code == 200
        assert any(m["model_id"] == "api_model" for m in listed.json())
