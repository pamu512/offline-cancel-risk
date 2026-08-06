"""Platt/isotonic score calibration helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


def calibration_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    raw = dict(policy.get("calibration") or {})
    return {
        "mode": str(raw.get("mode", "shadow")).strip().lower(),
        "on_tick": bool(raw.get("on_tick", False)),
        "min_labeled": int(raw.get("min_labeled", 30)),
        "platt_max_n": int(raw.get("platt_max_n", 80)),
        "holdout_fraction": float(raw.get("holdout_fraction", 0.3)),
        "max_ece": float(raw.get("max_ece", 0.05)),
        "cooldown_minutes": int(raw.get("cooldown_minutes", 1440)),
        "ece_bins": int(raw.get("ece_bins", 10)),
    }


def expected_calibration_error(
    probs: list[float], labels: list[int], *, n_bins: int
) -> float:
    if not probs:
        return 0.0
    probs_arr = np.asarray(probs, dtype=float)
    labels_arr = np.asarray(labels, dtype=int)
    n_bins = max(1, int(n_bins))
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    n = len(probs_arr)
    ece = 0.0
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        if i == n_bins - 1:
            mask = (probs_arr >= lo) & (probs_arr <= hi)
        else:
            mask = (probs_arr >= lo) & (probs_arr < hi)
        count = int(mask.sum())
        if count == 0:
            continue
        bin_conf = float(probs_arr[mask].mean())
        bin_acc = float(labels_arr[mask].mean())
        ece += (count / n) * abs(bin_acc - bin_conf)
    return float(ece)


def fit_calibrator(xs: list[float], ys: list[int], *, platt_max_n: int) -> dict:
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    if not xs:
        raise ValueError("empty training set")
    if len(xs) < int(platt_max_n):
        return _fit_platt(xs, ys)
    return _fit_isotonic(xs, ys)


def _fit_platt(xs: list[float], ys: list[int]) -> dict:
    y_arr = np.asarray(ys, dtype=int)
    if len(set(y_arr.tolist())) < 2:
        raise ValueError("Platt fitting requires both classes")
    X = np.asarray(xs, dtype=float).reshape(-1, 1)
    lr = LogisticRegression(solver="lbfgs")
    lr.fit(X, y_arr)
    return {
        "method": "platt",
        "params": {
            "coef": lr.coef_.ravel().tolist(),
            "intercept": lr.intercept_.tolist(),
        },
        "support": len(xs),
    }


def _fit_isotonic(xs: list[float], ys: list[int]) -> dict:
    X = np.asarray(xs, dtype=float)
    y_arr = np.asarray(ys, dtype=int)
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(X, y_arr)
    return {
        "method": "isotonic",
        "params": {
            "X_thresholds": iso.X_thresholds_.tolist(),
            "y_thresholds": iso.y_thresholds_.tolist(),
        },
        "support": len(xs),
    }


def predict_calibrated(model: dict, x: float) -> float:
    method = model["method"]
    params = model["params"]
    if method == "platt":
        coef = float(params["coef"][0])
        intercept = float(params["intercept"][0])
        z = coef * float(x) + intercept
        p = 1.0 / (1.0 + np.exp(-z))
    elif method == "isotonic":
        xt = np.asarray(params["X_thresholds"], dtype=float)
        yt = np.asarray(params["y_thresholds"], dtype=float)
        p = float(np.interp(float(x), xt, yt))
    else:
        raise ValueError(f"unknown calibration method: {method}")
    return float(np.clip(p, 0.0, 1.0))
