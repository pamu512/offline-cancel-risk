"""Model directory bundle contract and loaders (joblib + ONNX)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

HEAD_NAMES = ("cancelled_offline", "cancel_abuse", "selective_theft")


class BundleError(ValueError):
    """Invalid model bundle or prediction input."""


class Predictor(Protocol):
    def predict_array(self, x: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True)
class ModelHandle:
    model_id: str
    format: str
    feature_names: tuple[str, ...]
    feature_schema_version: str
    heads: tuple[str, ...]
    _predictor: Predictor

    def predict(self, features: dict[str, float]) -> dict[str, float]:
        missing = [n for n in self.feature_names if n not in features]
        if missing:
            raise BundleError(f"feature schema mismatch; missing: {missing}")
        row = np.asarray(
            [[float(features[n]) for n in self.feature_names]], dtype=np.float32
        )
        raw = self._predictor.predict_array(row)
        if raw.ndim == 1:
            raw = raw.reshape(1, -1)
        if raw.shape[1] != len(self.heads):
            raise BundleError(
                f"model output width {raw.shape[1]} != heads {len(self.heads)}"
            )
        vals = raw[0]
        return {
            head: float(np.clip(vals[i], 0.0, 1.0)) for i, head in enumerate(self.heads)
        }


class _JoblibPredictor:
    def __init__(self, model: Any) -> None:
        self._model = model

    def predict_array(self, x: np.ndarray) -> np.ndarray:
        out = self._model.predict(x)
        return np.asarray(out, dtype=np.float64)


class _OnnxPredictor:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._input_name = session.get_inputs()[0].name

    def predict_array(self, x: np.ndarray) -> np.ndarray:
        outputs = self._session.run(None, {self._input_name: x})
        arr = np.asarray(outputs[0], dtype=np.float64)
        return arr


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BundleError(f"{path.name} must be a JSON object")
    return data


def load_bundle(path: Path | str) -> ModelHandle:
    root = Path(path)
    if not root.is_dir():
        raise BundleError(f"bundle path is not a directory: {root}")

    meta_path = root / "model.json"
    schema_path = root / "feature_schema.json"
    if not meta_path.is_file() or not schema_path.is_file():
        raise BundleError("bundle requires model.json and feature_schema.json")

    meta = _read_json(meta_path)
    schema = _read_json(schema_path)

    model_id = str(meta.get("model_id") or root.name)
    fmt = str(meta.get("format", "")).lower()
    if fmt not in {"joblib", "onnx"}:
        raise BundleError(f"unsupported format: {fmt!r} (expected joblib|onnx)")

    heads = tuple(meta.get("heads") or [])
    if heads != HEAD_NAMES:
        raise BundleError(f"heads must be {HEAD_NAMES}, got {heads}")

    features = schema.get("features")
    if not isinstance(features, list) or not features:
        raise BundleError("feature_schema.json must list features")
    feature_names = tuple(str(f["name"]) for f in features)
    schema_version = str(schema.get("version") or meta.get("feature_schema_version") or "")
    if not schema_version:
        raise BundleError("feature schema version required")

    artifact = root / ("model.joblib" if fmt == "joblib" else "model.onnx")
    if not artifact.is_file():
        raise BundleError(f"missing artifact: {artifact.name}")

    expected = str(meta.get("checksum_sha256") or "")
    actual = _sha256(artifact)
    if not expected or expected != actual:
        raise BundleError("checksum mismatch for model artifact")

    if fmt == "joblib":
        import joblib

        payload = joblib.load(artifact)
        if isinstance(payload, dict) and "model" in payload:
            sk_model = payload["model"]
        else:
            sk_model = payload
        predictor: Predictor = _JoblibPredictor(sk_model)
    else:
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(artifact), providers=["CPUExecutionProvider"]
        )
        predictor = _OnnxPredictor(session)

    return ModelHandle(
        model_id=model_id,
        format=fmt,
        feature_names=feature_names,
        feature_schema_version=schema_version,
        heads=heads,
        _predictor=predictor,
    )
