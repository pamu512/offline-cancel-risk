"""Structured explainability pack for reviewers."""

from __future__ import annotations

from typing import Any


def build_evidence(
    *,
    stage: str,
    stage_meta: dict[str, Any],
    progress: dict[str, Any],
    integrity: dict[str, Any],
    cancel_rate: float | None,
    pair_cancel_count: int,
    driver_chain_count: int,
    abuse_reasons: list[str],
    theft_reasons: list[str],
    final_stop_confidence: float,
    limit: int = 8,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "feature": "cancel_stage",
            "value": stage,
            "head": "cancel_abuse",
            "weight": 0.2 if stage != "unknown" else 0.05,
        },
        {
            "feature": "final_stop_confidence",
            "value": round(float(final_stop_confidence), 4),
            "head": "cancelled_offline",
            "weight": float(final_stop_confidence),
        },
        {
            "feature": "driver_chain_count",
            "value": int(driver_chain_count),
            "head": "cancel_abuse",
            "weight": min(1.0, int(driver_chain_count) / 5.0),
        },
        {
            "feature": "pair_cancel_count",
            "value": int(pair_cancel_count),
            "head": "cancel_abuse",
            "weight": min(1.0, int(pair_cancel_count) / 5.0),
        },
    ]
    if cancel_rate is not None:
        items.append(
            {
                "feature": "entity_cancel_rate",
                "value": round(float(cancel_rate), 4),
                "head": "cancel_abuse",
                "weight": float(cancel_rate),
            }
        )
    if progress.get("wrong_direction"):
        items.append(
            {
                "feature": "wrong_direction",
                "value": progress.get("away_heading_fraction"),
                "head": "cancel_abuse",
                "weight": 0.8,
            }
        )
    if progress.get("no_progress"):
        items.append(
            {
                "feature": "no_progress_to_pickup",
                "value": progress.get("progress_ratio"),
                "head": "cancel_abuse",
                "weight": 0.7,
            }
        )
    if integrity.get("teleport") or integrity.get("impossible_speed"):
        items.append(
            {
                "feature": "gps_integrity",
                "value": {
                    "max_jump_m": integrity.get("max_jump_m"),
                    "max_speed_mps": integrity.get("max_segment_speed_mps"),
                },
                "head": "cancelled_offline",
                "weight": 0.6,
            }
        )
    for r in abuse_reasons:
        items.append(
            {"feature": r, "value": True, "head": "cancel_abuse", "weight": 0.5}
        )
    for r in theft_reasons:
        items.append(
            {"feature": r, "value": True, "head": "selective_theft", "weight": 0.5}
        )
    if stage_meta.get("dropoff_dist_m") is not None:
        items.append(
            {
                "feature": "dropoff_dist_m",
                "value": round(float(stage_meta["dropoff_dist_m"]), 1),
                "head": "cancel_abuse",
                "weight": 0.3,
            }
        )
    items.sort(key=lambda x: float(x.get("weight") or 0.0), reverse=True)
    # Dedupe by feature name keeping highest weight
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item["feature"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out
