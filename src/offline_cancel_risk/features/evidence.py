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
    marketplace: dict[str, Any] | None = None,
    device_eval: dict[str, Any] | None = None,
    device_graph: dict[str, Any] | None = None,
    chat_eval: dict[str, Any] | None = None,
    anomaly_eval: dict[str, Any] | None = None,
    stop_presence: dict[str, Any] | None = None,
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
                "weight": min(1.0, float(cancel_rate) / 4.0),
            }
        )
    if marketplace:
        acr = marketplace.get("accept_cancel_rate")
        if acr is not None:
            items.append(
                {
                    "feature": "accept_cancel_rate",
                    "value": round(float(acr), 4),
                    "head": "cancel_abuse",
                    "weight": float(acr),
                }
            )
        cr = marketplace.get("completion_rate")
        if cr is not None:
            items.append(
                {
                    "feature": "completion_rate",
                    "value": round(float(cr), 4),
                    "head": "cancel_abuse",
                    "weight": max(0.0, 1.0 - float(cr)),
                }
            )
    if device_eval and device_eval.get("fires"):
        items.append(
            {
                "feature": "device_integrity",
                "value": {
                    "effective_risk": round(
                        float(device_eval.get("effective_risk") or 0.0), 4
                    ),
                    "instant_risk": round(
                        float(device_eval.get("instant_risk") or 0.0), 4
                    ),
                    "ewma_risk": round(float(device_eval.get("ewma_risk") or 0.0), 4),
                    "flags": device_eval.get("normalized") or {},
                },
                "head": "cancel_abuse",
                "weight": float(device_eval.get("effective_risk") or 0.0),
            }
        )
    if device_graph and device_graph.get("signals"):
        items.append(
            {
                "feature": "device_graph",
                "value": {
                    "signals": list(device_graph.get("signals") or []),
                    "drivers_on_device": device_graph.get("drivers_on_device"),
                    "users_on_device": device_graph.get("users_on_device"),
                    "devices_for_driver": device_graph.get("devices_for_driver"),
                    "shared_device_pair": device_graph.get("shared_device_pair"),
                },
                "head": "cancel_abuse",
                "weight": min(1.0, 0.25 * len(device_graph.get("signals") or [])),
            }
        )
    if chat_eval and chat_eval.get("abuse_bonus"):
        items.append(
            {
                "feature": "chat_force_cancel",
                "value": {
                    "risk": round(float(chat_eval.get("risk") or 0.0), 4),
                    "flags": chat_eval.get("normalized") or {},
                    "reasons": list(chat_eval.get("reasons") or []),
                    "driver_signal_count": chat_eval.get("driver_signal_count"),
                },
                "head": "cancel_abuse",
                "weight": float(chat_eval.get("risk") or 0.0),
            }
        )
    if anomaly_eval and anomaly_eval.get("fires"):
        top = None
        for d in anomaly_eval.get("details") or []:
            zs = [
                z
                for z in (d.get("z_self"), d.get("z_peer"))
                if z is not None
            ]
            if not zs:
                continue
            mz = max(zs)
            if top is None or mz > top[0]:
                top = (mz, d)
        items.append(
            {
                "feature": "entity_anomaly",
                "value": {
                    "mode": anomaly_eval.get("mode"),
                    "signals": list(anomaly_eval.get("signals") or []),
                    "cohort_key": anomaly_eval.get("cohort_key"),
                    "top": top[1] if top else None,
                },
                "head": "cancel_abuse",
                "weight": min(1.0, (top[0] / 6.0) if top else 0.5),
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
    if stop_presence:
        items.append(
            {
                "feature": "stop_presence",
                "value": {
                    "dwell_target_s": stop_presence.get("dwell_target_s"),
                    "median_gap_s": stop_presence.get("median_gap_s"),
                    "median_gap_raw_s": stop_presence.get("median_gap_raw_s"),
                    "min_pts_effective": stop_presence.get("min_pts_effective"),
                    "drop_off_min_pts_effective": stop_presence.get(
                        "drop_off_min_pts_effective"
                    ),
                    "place_class": stop_presence.get("place_class"),
                    "vehicle_class": stop_presence.get("vehicle_class"),
                    "place_factor": stop_presence.get("place_factor"),
                    "vehicle_factor": stop_presence.get("vehicle_factor"),
                    "autoscaled": stop_presence.get("autoscaled"),
                },
                "head": "cancelled_offline",
                "weight": 0.35,
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
