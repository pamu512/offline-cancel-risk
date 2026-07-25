"""TDD: joblib + ONNX model bundle loaders."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.models.bundle import (
    BundleError,
    HEAD_NAMES,
    load_bundle,
)

FIXTURES = Path(__file__).parent / "fixtures" / "models"


def _feature_schema() -> dict:
    return {
        "version": "v1",
        "features": [
            {"name": "final_stop_confidence", "dtype": "float"},
            {"name": "sequence_score", "dtype": "float"},
            {"name": "dwell_fraction", "dtype": "float"},
            {"name": "abuse_score", "dtype": "float"},
            {"name": "theft_score", "dtype": "float"},
        ],
    }


def _write_bundle(
    root: Path,
    *,
    fmt: str,
    artifact_name: str,
    artifact_bytes: bytes | None = None,
    artifact_path: Path | None = None,
    checksum: str | None = None,
    schema: dict | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    schema = schema or _feature_schema()
    (root / "feature_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (root / "metrics_baseline.json").write_text(
        json.dumps({"ece": 0.04, "fp_dollar": 100.0}), encoding="utf-8"
    )
    dest = root / artifact_name
    if artifact_path is not None:
        shutil.copy(artifact_path, dest)
        raw = dest.read_bytes()
    else:
        assert artifact_bytes is not None
        dest.write_bytes(artifact_bytes)
        raw = artifact_bytes
    digest = checksum or hashlib.sha256(raw).hexdigest()
    model_json = {
        "model_id": root.name,
        "format": fmt,
        "heads": list(HEAD_NAMES),
        "feature_schema_version": schema["version"],
        "checksum_sha256": digest,
        "created_at": "2026-07-25T00:00:00Z",
    }
    (root / "model.json").write_text(json.dumps(model_json), encoding="utf-8")
    return root


def _train_joblib(path: Path) -> Path:
    rng = np.random.default_rng(0)
    x = rng.random((40, 5))
    y = np.clip(x @ np.array([0.4, 0.2, 0.2, 0.1, 0.1])[:, None] + 0.1, 0, 1)
    y = np.hstack([y, y * 0.8, y * 0.5])
    model = MultiOutputRegressor(Ridge(alpha=1.0)).fit(x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "heads": list(HEAD_NAMES)}, path)
    return path


def _export_onnx(joblib_path: Path, onnx_path: Path) -> Path:
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    payload = joblib.load(joblib_path)
    model = payload["model"]
    onx = convert_sklearn(
        model,
        initial_types=[("features", FloatTensorType([None, 5]))],
        target_opset=12,
    )
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    onnx_path.write_bytes(onx.SerializeToString())
    return onnx_path


@pytest.fixture(scope="module")
def joblib_artifact(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("art") / "model.joblib"
    return _train_joblib(p)


@pytest.fixture(scope="module")
def onnx_artifact(joblib_artifact: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("art") / "model.onnx"
    return _export_onnx(joblib_artifact, p)


def test_load_joblib_bundle_predicts_three_heads(tmp_path: Path, joblib_artifact: Path):
    bundle = _write_bundle(
        tmp_path / "m_joblib",
        fmt="joblib",
        artifact_name="model.joblib",
        artifact_path=joblib_artifact,
    )
    handle = load_bundle(bundle)
    out = handle.predict(
        {
            "final_stop_confidence": 0.9,
            "sequence_score": 0.8,
            "dwell_fraction": 0.7,
            "abuse_score": 0.2,
            "theft_score": 0.1,
        }
    )
    assert set(out) == set(HEAD_NAMES)
    assert all(0.0 <= v <= 1.0 for v in out.values())


def test_load_onnx_bundle_predicts_three_heads(tmp_path: Path, onnx_artifact: Path):
    bundle = _write_bundle(
        tmp_path / "m_onnx",
        fmt="onnx",
        artifact_name="model.onnx",
        artifact_path=onnx_artifact,
    )
    handle = load_bundle(bundle)
    out = handle.predict(
        {
            "final_stop_confidence": 0.9,
            "sequence_score": 0.8,
            "dwell_fraction": 0.7,
            "abuse_score": 0.2,
            "theft_score": 0.1,
        }
    )
    assert set(out) == set(HEAD_NAMES)


def test_checksum_mismatch_rejected(tmp_path: Path, joblib_artifact: Path):
    bundle = _write_bundle(
        tmp_path / "m_bad",
        fmt="joblib",
        artifact_name="model.joblib",
        artifact_path=joblib_artifact,
        checksum="0" * 64,
    )
    with pytest.raises(BundleError, match="checksum"):
        load_bundle(bundle)


def test_schema_drift_rejected(tmp_path: Path, joblib_artifact: Path):
    bundle = _write_bundle(
        tmp_path / "m_drift",
        fmt="joblib",
        artifact_name="model.joblib",
        artifact_path=joblib_artifact,
    )
    handle = load_bundle(bundle)
    with pytest.raises(BundleError, match="schema|feature|missing"):
        handle.predict(
            {
                "final_stop_confidence": 0.1,
                "sequence_score": 0.1,
                # dwell_fraction / abuse / theft omitted → schema mismatch
            }
        )
