"""Entity baseline gate: rolling window + EWMA, band-limited discount."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def baselines_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    raw = dict(policy.get("baselines") or {})
    heads_in = dict(raw.get("heads") or {})
    heads: dict[str, dict[str, Any]] = {}
    for head in _HEADS:
        h = dict(heads_in.get(head) or {})
        heads[head] = {
            "enabled": bool(h.get("enabled", True)),
            "mode": h.get("mode"),  # None → inherit global
            "discount": float(h["discount"]) if "discount" in h else None,
        }
    mode = str(raw.get("mode", "shadow")).strip().lower()
    if mode not in {"shadow", "apply", "off"}:
        mode = "shadow"
    return {
        "mode": mode,
        "window_n": int(raw.get("window_n", 20)),
        "pair_window_n": int(raw.get("pair_window_n", 8)),
        "under_fraction": float(raw.get("under_fraction", 0.9)),
        "ewma_alpha": float(raw.get("ewma_alpha", 0.2)),
        "ewma_delta": float(raw.get("ewma_delta", 0.05)),
        "min_ewma_samples": int(raw.get("min_ewma_samples", 10)),
        "above_epsilon": float(raw.get("above_epsilon", 0.1)),
        "above_fraction": float(raw.get("above_fraction", 0.9)),
        "discount": float(raw.get("discount", 0.85)),
        "refresh_epsilon": float(raw.get("refresh_epsilon", 0.05)),
        "max_age_days": int(raw.get("max_age_days", 90)),
        "heads": heads,
    }


def entity_specs(
    driver_id: int, user_id: int | None
) -> list[tuple[str, str, int | None, int | None]]:
    """Return (kind, entity_key, driver_id, user_id)."""
    out: list[tuple[str, str, int | None, int | None]] = [
        ("driver", f"driver:{int(driver_id)}", int(driver_id), None)
    ]
    if user_id is None:
        return out
    uid = int(user_id)
    out.append(("user", f"user:{uid}", None, uid))
    out.append(("pair", f"pair:{int(driver_id)}:{uid}", int(driver_id), uid))
    return out


def window_size(kind: str, cfg: dict[str, Any]) -> int:
    if kind == "pair":
        return max(1, int(cfg["pair_window_n"]))
    return max(1, int(cfg["window_n"]))


def prune_window(
    window: list[dict[str, Any]],
    *,
    max_age_days: int,
    max_len: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or _utc_now()
    cutoff = now - timedelta(days=max(0, int(max_age_days)))
    kept: list[dict[str, Any]] = []
    for item in window:
        try:
            ts = _parse_ts(str(item["t"]))
        except (KeyError, ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        kept.append({"s": float(item["s"]), "t": str(item["t"])})
    if len(kept) > max_len:
        kept = kept[-max_len:]
    return kept


def under_consistent(
    window: list[dict[str, Any]],
    ewma: float,
    samples: int,
    *,
    thr: float,
    cfg: dict[str, Any],
    max_len: int,
) -> bool:
    # OR: soft arm
    if len(window) >= max_len:
        under = sum(1 for w in window if float(w["s"]) < thr)
        if under / max_len >= float(cfg["under_fraction"]):
            return True
    if samples >= int(cfg["min_ewma_samples"]) and ewma < thr - float(
        cfg["ewma_delta"]
    ):
        return True
    return False


def above_consistent(
    window: list[dict[str, Any]],
    ewma: float,
    samples: int,
    *,
    baseline: float,
    cfg: dict[str, Any],
    max_len: int,
) -> bool:
    # AND: hard activate
    eps = float(cfg["above_epsilon"])
    if samples < int(cfg["min_ewma_samples"]):
        return False
    if ewma <= baseline + eps:
        return False
    if len(window) < max_len:
        return False
    above = sum(1 for w in window if float(w["s"]) > baseline + eps)
    return above / max_len >= float(cfg["above_fraction"])


def candidate_baseline(window: list[dict[str, Any]], thr: float, ewma: float) -> float:
    under_scores = [float(w["s"]) for w in window if float(w["s"]) < thr]
    if under_scores:
        return sum(under_scores) / len(under_scores)
    return float(ewma)


def head_mode(cfg: dict[str, Any], head: str) -> str:
    global_mode = str(cfg["mode"])
    if global_mode == "off":
        return "off"
    h = (cfg.get("heads") or {}).get(head) or {}
    if not h.get("enabled", True):
        return "off"
    override = h.get("mode")
    if override in {"shadow", "apply", "off"}:
        return str(override)
    return global_mode


def head_discount(cfg: dict[str, Any], head: str) -> float:
    h = (cfg.get("heads") or {}).get(head) or {}
    if h.get("discount") is not None:
        return float(h["discount"])
    return float(cfg["discount"])


def band_eligible(score: float, baseline: float, armed_thr: float, eps: float) -> bool:
    """Discount only in (baseline+ε, armed_thr). Absolute thr crossings never dampened."""
    return float(baseline) + float(eps) < float(score) < float(armed_thr)


def apply_baselines(
    store: Any,
    *,
    scores: dict[str, float],
    thresholds: dict[str, float],
    policy: dict[str, Any],
    driver_id: int,
    user_id: int | None,
    region_code: str = "",
    city_code: str = "",
    assessed_at: str | None = None,
) -> tuple[dict[str, float], list[str], dict[str, Any]]:
    """Update baselines from raw scores; return (possibly discounted scores, reasons, meta)."""
    cfg = baselines_cfg(policy)
    if cfg["mode"] == "off":
        return dict(scores), [], {}

    ts = assessed_at or _utc_now().isoformat().replace("+00:00", "Z")
    now = _parse_ts(ts)
    raw = {h: float(scores.get(h, 0.0)) for h in _HEADS}
    out_scores = dict(raw)
    reasons: list[str] = []
    meta: dict[str, Any] = {}

    entities = entity_specs(driver_id, user_id)
    # Update all entities first from raw scores.
    rows_by_kind: dict[str, dict[str, dict[str, Any]]] = {
        kind: {} for kind, _, _, _ in entities
    }
    for kind, key, did, uid in entities:
        max_len = window_size(kind, cfg)
        for head in _HEADS:
            if head_mode(cfg, head) == "off":
                continue
            thr = float(thresholds.get(head, 0.75))
            row = store.update_observation(
                entity_key=key,
                entity_kind=kind,
                driver_id=did,
                user_id=uid,
                head=head,
                score=raw[head],
                live_thr=thr,
                cfg=cfg,
                max_len=max_len,
                region_code=region_code,
                city_code=city_code,
                observed_at=ts,
                now=now,
            )
            rows_by_kind[kind][head] = row

    # Prefer pair → driver → user for band reference / applied list.
    kind_order = [k for k, _, _, _ in entities]
    # pair, driver, user preferred order for backoff
    prefer = [k for k in ("pair", "driver", "user") if k in kind_order]

    for head in _HEADS:
        mode = head_mode(cfg, head)
        if mode == "off":
            continue
        active_kinds = [
            k
            for k in prefer
            if rows_by_kind.get(k, {}).get(head, {}).get("discount_active")
        ]
        if not active_kinds:
            continue

        # Band reference: first preferred kind that is discount_active
        ref_kind = active_kinds[0]
        ref = rows_by_kind[ref_kind][head]
        baseline = ref.get("baseline")
        armed_thr = ref.get("armed_thr")
        if baseline is None or armed_thr is None:
            continue
        eps = float(cfg["above_epsilon"])
        eligible = band_eligible(raw[head], float(baseline), float(armed_thr), eps)
        discount = head_discount(cfg, head)
        head_meta: dict[str, Any] = {
            "applied": active_kinds,
            "multiplier": discount,
            "mode": mode,
            "band_eligible": eligible,
            "ref_kind": ref_kind,
            "baseline": float(baseline),
            "armed_thr": float(armed_thr),
        }
        meta[head] = head_meta
        if not eligible:
            # Absolute thr / out-of-band: no dampen; meta records skip.
            continue
        if mode == "shadow":
            for k in active_kinds:
                reasons.append(f"baseline_shadow:{k}")
            continue
        # apply
        out_scores[head] = _clip01(raw[head] * discount)
        for k in active_kinds:
            reasons.append(f"baseline_discount:{k}")

    # Dedupe reasons preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return out_scores, uniq, meta
