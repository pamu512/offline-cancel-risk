from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.models.bundle import HEAD_NAMES
from offline_cancel_risk.models.registry import ModelRegistry


def _bundle(tmp: Path, model_id: str = "challenger_a") -> Path:
    rng = np.random.default_rng(1)
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


def test_sideload_defaults_to_shadow(tmp_path: Path):
    reg = ModelRegistry(tmp_path / "reg.db", tmp_path / "models")
    rec = reg.sideload(_bundle(tmp_path))
    assert rec.role == "shadow"
    assert reg.get_champion() is None
    assert len(reg.list_shadow()) == 1


def test_sideload_in_place_under_models_root(tmp_path: Path):
    models_root = tmp_path / "models"
    reg = ModelRegistry(tmp_path / "reg.db", models_root)
    bundle = _bundle(models_root, "in_place")
    rec = reg.sideload(bundle, role="shadow")
    assert rec.model_id == "in_place"
    assert rec.bundle_path == str(bundle.resolve())
    assert bundle.exists()
    assert (bundle / "model.json").exists()


def test_only_one_champion(tmp_path: Path):
    reg = ModelRegistry(tmp_path / "reg.db", tmp_path / "models")
    a = reg.sideload(_bundle(tmp_path, "a"), role="champion")
    assert a.role == "champion"
    b = reg.sideload(_bundle(tmp_path, "b"), role="champion")
    assert b.role == "champion"
    assert reg.get("a").role == "retired"
    assert reg.get_champion().model_id == "b"
