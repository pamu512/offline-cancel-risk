from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.multioutput import MultiOutputRegressor

from offline_cancel_risk.models.bundle import HEAD_NAMES, load_bundle
from offline_cancel_risk.pipeline.score_build import ML_FEATURE_KEYS
from offline_cancel_risk.train.shards import load_all_shards


def _feature_schema() -> dict:
    return {
        "version": "v1",
        "features": [
            {"name": name, "dtype": "float"} for name in ML_FEATURE_KEYS
        ],
    }


def _make_estimator(seed: int):
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_depth=6, max_iter=100, random_state=seed
        )
    except ImportError:
        from sklearn.linear_model import Ridge

        return Ridge(alpha=1.0)


def _precision_recall(
    y_true: np.ndarray, y_pred: np.ndarray, *, threshold: float = 0.5
) -> tuple[float, float]:
    y_bin = (y_true >= 0.5).astype(np.int8)
    pred_bin = (y_pred >= threshold).astype(np.int8)
    tp = int(((y_bin == 1) & (pred_bin == 1)).sum())
    fp = int(((y_bin == 0) & (pred_bin == 1)).sum())
    fn = int(((y_bin == 1) & (pred_bin == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


def fit_bundle(
    shard_dir: Path | str,
    bundle_dir: Path | str,
    *,
    holdout_frac: float = 0.05,
    seed: int = 0,
) -> dict:
    X, y = load_all_shards(shard_dir)
    n = X.shape[0]
    if n == 0:
        raise ValueError("no training rows in shards")

    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    n_hold = max(1, int(n * holdout_frac))
    hold, train = idx[:n_hold], idx[n_hold:]

    model = MultiOutputRegressor(_make_estimator(seed))
    model.fit(X[train], y[train])

    pred = np.clip(model.predict(X[hold]), 0.0, 1.0)
    y_hold = y[hold]

    per_head: dict[str, dict[str, float]] = {}
    for i, head in enumerate(HEAD_NAMES):
        precision, recall = _precision_recall(y_hold[:, i], pred[:, i])
        per_head[head] = {
            "mae": float(np.mean(np.abs(y_hold[:, i] - pred[:, i]))),
            "precision": precision,
            "recall": recall,
        }

    metrics = {
        "holdout_frac": holdout_frac,
        "n_train": int(train.shape[0]),
        "n_holdout": int(hold.shape[0]),
        "per_head": per_head,
    }

    root = Path(bundle_dir)
    root.mkdir(parents=True, exist_ok=True)

    schema = _feature_schema()
    (root / "feature_schema.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    (root / "metrics_baseline.json").write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    joblib_path = root / "model.joblib"
    joblib.dump({"model": model, "heads": list(HEAD_NAMES)}, joblib_path)
    digest = hashlib.sha256(joblib_path.read_bytes()).hexdigest()

    model_json = {
        "model_id": root.name,
        "format": "joblib",
        "heads": list(HEAD_NAMES),
        "feature_schema_version": schema["version"],
        "checksum_sha256": digest,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (root / "model.json").write_text(json.dumps(model_json), encoding="utf-8")

    load_bundle(root)  # smoke
    return metrics
