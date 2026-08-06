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
        "max_brier": float(raw.get("max_brier", 0.25)),
        "cooldown_minutes": int(raw.get("cooldown_minutes", 1440)),
        "ece_bins": int(raw.get("ece_bins", 10)),
        # quantile bins avoid empty mass when scores cluster (e.g. old S-only fits)
        "ece_strategy": str(raw.get("ece_strategy", "quantile")).strip().lower(),
    }


def brier_score(probs: list[float], labels: list[int]) -> float:
    if not probs:
        return 0.0
    if len(probs) != len(labels):
        raise ValueError("probs and labels must have the same length")
    p = np.asarray(probs, dtype=float)
    y = np.asarray(labels, dtype=float)
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(
    probs: list[float],
    labels: list[int],
    *,
    n_bins: int,
    strategy: str = "quantile",
) -> float:
    """ECE with equal-width or quantile probability bins.

    Quantile bins put ~equal mass in each bin so clustered scores cannot
    collapse into a single equal-width bin and fake a near-zero ECE.
    """
    if not probs:
        return 0.0
    if len(probs) != len(labels):
        raise ValueError("probs and labels must have the same length")
    probs_arr = np.asarray(probs, dtype=float)
    labels_arr = np.asarray(labels, dtype=int)
    n_bins = max(1, int(n_bins))
    n = len(probs_arr)
    strategy = (strategy or "quantile").strip().lower()

    if strategy == "equal":
        edges = np.linspace(0.0, 1.0, n_bins + 1)
    else:
        qs = np.linspace(0.0, 1.0, n_bins + 1)
        edges = np.unique(np.quantile(probs_arr, qs))
        if len(edges) < 2:
            # All probs identical — single-bin ECE = |acc - conf|
            return float(abs(float(labels_arr.mean()) - float(probs_arr.mean())))

    ece = 0.0
    for i in range(len(edges) - 1):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == len(edges) - 2:
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
        # Stable sigmoid (avoid overflow on extreme z).
        if z >= 0:
            p = 1.0 / (1.0 + np.exp(-z))
        else:
            ez = np.exp(z)
            p = ez / (1.0 + ez)
    elif method == "isotonic":
        xt = np.asarray(params["X_thresholds"], dtype=float)
        yt = np.asarray(params["y_thresholds"], dtype=float)
        if xt.size == 0:
            p = 0.0
        else:
            p = float(np.interp(float(x), xt, yt))
    else:
        raise ValueError(f"unknown calibration method: {method}")
    return float(np.clip(p, 0.0, 1.0))


def apply_calibrated_score(
    *, p: float, scores_raw: float, scores_current: float
) -> float:
    """Map calibrated p back through any baseline discount on the live score.

    discount = scores_current / scores_raw (1.0 when baselines did not fire).
    """
    raw = float(scores_raw)
    cur = float(scores_current)
    if abs(raw) <= 1e-12:
        return float(np.clip(p, 0.0, 1.0))
    discount = cur / raw
    return float(np.clip(float(p) * discount, 0.0, 1.0))
