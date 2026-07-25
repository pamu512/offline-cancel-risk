"""Map supply_ratio to precision/recall hardgate bands."""

from __future__ import annotations

from typing import Any


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def resolve_operating_point(
    cfg: dict[str, Any], supply_ratio: float | None
) -> dict[str, Any]:
    """Return min/max precision/recall bounds and regime label."""
    if supply_ratio is None:
        fb = dict(cfg.get("fallback_when_no_forecast") or {})
        return {
            "min_precision": float(fb.get("min_precision", 0.75)),
            "min_recall": float(fb.get("min_recall", 0.5)),
            "max_precision": float(fb.get("max_precision", 1.0)),
            "max_recall": float(fb.get("max_recall", 1.0)),
            "regime": "fallback",
            "supply_ratio": None,
        }

    ratio_min = float(cfg.get("ratio_min", 0.5))
    ratio_max = float(cfg.get("ratio_max", 2.0))
    ratio = max(ratio_min, min(ratio_max, float(supply_ratio)))

    peak = cfg["peak"]
    surplus = cfg["surplus"]
    peak_r = float(peak["ratio"])
    surplus_r = float(surplus["ratio"])

    keys = ("min_precision", "min_recall", "max_precision", "max_recall")

    if ratio <= peak_r:
        out = {k: float(peak[k]) for k in keys}
        out["regime"] = "peak"
        out["supply_ratio"] = ratio
        return out
    if ratio >= surplus_r:
        out = {k: float(surplus[k]) for k in keys}
        out["regime"] = "surplus"
        out["supply_ratio"] = ratio
        return out

    t = (ratio - peak_r) / (surplus_r - peak_r)
    out = {k: _lerp(float(peak[k]), float(surplus[k]), t) for k in keys}
    out["regime"] = "mid"
    out["supply_ratio"] = ratio
    return out


def supply_ratio_from_forecast(row: dict[str, Any] | None) -> float | None:
    if row is None:
        return None
    demand = float(row["forecast_demand"])
    if demand <= 0:
        return None
    return float(row["forecast_supply"]) / demand
