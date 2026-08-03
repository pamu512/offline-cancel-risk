"""Device integrity scoring from Downstream ingest (no SDK here)."""

from __future__ import annotations

from typing import Any


_FLAG_WEIGHTS = (
    ("spoof_suspected", 0.85),
    ("fake_app", 0.80),
    ("emulator", 0.75),
    ("rooted", 0.55),
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def normalize_device_risk(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(raw or {})
    out: dict[str, Any] = {
        "spoof_suspected": _as_bool(raw.get("spoof_suspected")),
        "fake_app": _as_bool(raw.get("fake_app") or raw.get("tampered_app")),
        "emulator": _as_bool(raw.get("emulator")),
        "rooted": _as_bool(raw.get("rooted") or raw.get("jailbroken")),
        "risk_score": None,
        "vendor": raw.get("vendor"),
    }
    if raw.get("risk_score") is not None and raw.get("risk_score") != "":
        out["risk_score"] = max(0.0, min(1.0, float(raw["risk_score"])))
    return out


def instant_device_risk(normalized: dict[str, Any]) -> float:
    """max(vendor_score, strongest flag weight) — no additive double-count."""
    flag_f = 0.0
    for key, weight in _FLAG_WEIGHTS:
        if normalized.get(key):
            flag_f = max(flag_f, float(weight))
    vendor = normalized.get("risk_score")
    v = float(vendor) if vendor is not None else 0.0
    return max(0.0, min(1.0, max(v, flag_f)))


def ewma_risk(prev: float | None, instant: float, alpha: float) -> float:
    a = max(0.0, min(1.0, float(alpha)))
    if prev is None:
        return float(instant)
    return a * float(instant) + (1.0 - a) * float(prev)


def evaluate_device_integrity(
    raw: dict[str, Any] | None,
    *,
    prev_ewma: float | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Return effective risk, fire decision, GPS dampen multiplier, reasons."""
    cfg = dict(policy.get("device_integrity") or policy)
    # Allow nested under abuse.device_integrity or top-level device_integrity
    if "device_integrity" in (policy.get("abuse") or {}):
        cfg = {**cfg, **dict(policy["abuse"]["device_integrity"])}
    alpha = float(cfg.get("ewma_alpha", 0.4))
    thr = float(cfg.get("risk_threshold", 0.7))
    gps_delta = float(cfg.get("gps_dampen", 0.5))
    abuse_beta = float(cfg.get("abuse_bonus_scale", 0.35))

    norm = normalize_device_risk(raw)
    instant = instant_device_risk(norm)
    ewma = ewma_risk(prev_ewma, instant, alpha)
    effective = max(instant, ewma)
    fires = effective >= thr
    reasons: list[str] = []
    if fires:
        reasons.append("device_integrity")
        for key, _ in _FLAG_WEIGHTS:
            if norm.get(key):
                reasons.append(f"device_{key}")
    gps_mult = 1.0
    if norm.get("spoof_suspected") or norm.get("emulator"):
        gps_mult = max(0.0, 1.0 - gps_delta * effective)
    abuse_bonus = abuse_beta * effective if fires else 0.0
    return {
        "normalized": norm,
        "instant_risk": instant,
        "ewma_risk": ewma,
        "effective_risk": effective,
        "fires": fires,
        "abuse_bonus": abuse_bonus,
        "gps_multiplier": gps_mult,
        "reasons": reasons,
        "threshold": thr,
    }
