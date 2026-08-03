"""Entity / marketplace / device / chat / abuse-theft signal stage."""

from __future__ import annotations

import logging
from typing import Any

from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.anomaly import (
    cohort_key,
    evaluate_entity_anomaly,
)
from offline_cancel_risk.features.chat_signals import (
    evaluate_chat_signals,
    merge_chat_signals,
    normalize_chat_signals,
)
from offline_cancel_risk.features.theft import theft_feature_score
from offline_cancel_risk.pipeline.context import AssessContext

_LOG = logging.getLogger(__name__)


def _order_still_active(status: str) -> bool:
    return status.strip().upper() not in {
        "CANCELLED",
        "CANCELED",
        "COMPLETED",
        "DELIVERED",
        "DONE",
    }


def run_signals_stage(ctx: AssessContext) -> None:
    req = ctx.req
    policy = ctx.policy

    stats_window = int(
        policy.get("abuse", {}).get("cancel_stats_window_minutes", 120)
    )
    if ctx.cancel_stats is not None:
        try:
            accept_ts = req.accepted_at or req.assign_ts
            ctx.cancel_stats.record_assess_funnel(
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                order_display_id=req.order_display_id,
                accept_ts=accept_ts,
                cancel_ts=req.cancel_ts,
                cancel_with_cause=req.cancel_with_cause,
                cancel_reason_code=req.cancel_reason_code,
                extra_events=list(req.marketplace_events or []),
            )
            st = ctx.cancel_stats.stats(
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                as_of=req.cancel_ts,
                window_minutes=stats_window,
                exclude_order_id=req.order_display_id,
                abuse_policy=policy.get("abuse") or {},
            )
            ctx.driver_cancel_count = int(st["driver_cancel_count"])
            ctx.cancel_rate = float(st["driver_cancel_rate"])
            ctx.pair_cancel_count = int(st["pair_cancel_count"])
            ctx.marketplace_signals = list(st.get("signals") or [])
            ctx.marketplace_meta = {
                "accept_cancel_rate": st.get("accept_cancel_rate"),
                "completion_rate": st.get("completion_rate"),
                "with_cause_fraction": st.get("with_cause_fraction"),
                "accepts": st.get("accepts"),
                "cancels": st.get("cancels"),
                "completes": st.get("completes"),
            }
        except Exception:
            _LOG.exception(
                "Entity cancel stats failed for order=%s", req.order_display_id
            )

    if ctx.device_graph is not None and req.device_id:
        try:
            ctx.device_graph.observe(
                device_id=str(req.device_id),
                driver_id=int(req.driver_id),
                user_id=None,
                event_ts=req.cancel_ts,
            )
            ctx.graph_meta = ctx.device_graph.evaluate(
                device_id=str(req.device_id),
                driver_id=int(req.driver_id),
                user_id=int(req.user_id) if req.user_id is not None else None,
                as_of=req.cancel_ts,
                policy=policy,
            )
            ctx.device_graph_signals = list(ctx.graph_meta.get("signals") or [])
        except Exception:
            _LOG.exception(
                "Device graph failed for order=%s device=%s",
                req.order_display_id,
                req.device_id,
            )

    chat_cfg = policy.get("chat_signals") or {}
    stored_chat = None
    if ctx.chat_store is not None:
        try:
            stored_chat = ctx.chat_store.get(req.order_display_id)
        except Exception:
            _LOG.exception(
                "Chat signal lookup failed order=%s", req.order_display_id
            )
    merged_chat = merge_chat_signals(
        stored_chat.get("flags") if stored_chat else None,
        req.chat_signals,
    )
    driver_chat_n = 0
    if ctx.chat_store is not None:
        try:
            driver_chat_n = ctx.chat_store.driver_signal_count(
                int(req.driver_id),
                as_of=req.cancel_ts,
                window_minutes=int(chat_cfg.get("window_minutes", 10080)),
                min_risk=float(chat_cfg.get("risk_threshold", 0.55)),
            )
        except Exception:
            _LOG.exception(
                "Chat driver count failed driver=%s", req.driver_id
            )
    ctx.chat_eval = evaluate_chat_signals(
        merged_chat,
        driver_signal_count=driver_chat_n,
        no_progress=bool(ctx.progress.get("no_progress")),
        wrong_direction=bool(ctx.progress.get("wrong_direction")),
        policy=policy,
    )
    if ctx.chat_store is not None and (
        req.chat_signals
        or ctx.chat_eval["fires"]
        or any(
            merged_chat.get(k)
            for k in (
                "persuasion_suspected",
                "cash_offline_suggested",
                "rider_forced_cancel",
            )
        )
        or merged_chat.get("signal_score") is not None
    ):
        try:
            ctx.chat_store.upsert(
                order_display_id=req.order_display_id,
                driver_id=int(req.driver_id),
                user_id=req.user_id,
                flags=normalize_chat_signals(merged_chat),
                risk=float(ctx.chat_eval["risk"]),
                event_ts=req.cancel_ts,
            )
        except Exception:
            _LOG.exception(
                "Chat signal persist failed order=%s", req.order_display_id
            )

    ctx.abuse_score, ctx.abuse_reasons = abuse_feature_score(
        {
            "order_still_active": _order_still_active(req.order_status),
            "cancel_event_count": len(req.reassign_cancel_events) + 1,
            "driver_chain_count": ctx.driver_chain_count,
            "cancel_near_destination": ctx.near_dest,
            "cancel_after_pickup": ctx.after_pickup,
            "no_progress": bool(ctx.progress.get("no_progress")),
            "wrong_direction": bool(ctx.progress.get("wrong_direction")),
            "driver_cancel_count": ctx.driver_cancel_count,
            "driver_cancel_rate": ctx.cancel_rate or 0.0,
            "pair_cancel_count": ctx.pair_cancel_count,
            "marketplace_signals": ctx.marketplace_signals,
            "device_eval": ctx.device_eval,
            "device_graph_signals": ctx.device_graph_signals,
            "chat_eval": ctx.chat_eval,
        },
        policy["abuse"],
    )

    ctx.anomaly_eval = {
        "mode": "off",
        "fires": False,
        "signals": [],
        "details": [],
        "abuse_bonus": 0.0,
        "reasons": [],
    }
    if ctx.anomalies is not None:
        try:
            feat_vals: dict[str, float] = {"cancel_abuse": float(ctx.abuse_score)}
            if ctx.cancel_rate is not None:
                feat_vals["cancel_rate"] = float(ctx.cancel_rate)
            acr = ctx.marketplace_meta.get("accept_cancel_rate")
            if acr is not None and int(ctx.marketplace_meta.get("accepts") or 0) >= 1:
                feat_vals["accept_cancel_rate"] = float(acr)
            ctx.anomaly_eval = evaluate_entity_anomaly(
                store=ctx.anomalies,
                entity_key=f"driver:{int(req.driver_id)}",
                cohort=cohort_key(
                    city_code=req.city_code, region_code=req.region_code
                ),
                features=feat_vals,
                order_display_id=req.order_display_id,
                event_ts=req.cancel_ts,
                policy=policy,
            )
            if ctx.anomaly_eval.get("abuse_bonus"):
                ctx.abuse_score = min(
                    1.0,
                    float(ctx.abuse_score) + float(ctx.anomaly_eval["abuse_bonus"]),
                )
                for r in ctx.anomaly_eval.get("signals") or []:
                    if r not in ctx.abuse_reasons:
                        ctx.abuse_reasons.append(str(r))
            elif ctx.anomaly_eval.get("reasons"):
                for r in ctx.anomaly_eval["reasons"]:
                    if r not in ctx.abuse_reasons:
                        ctx.abuse_reasons.append(str(r))
        except Exception:
            _LOG.exception(
                "Entity anomaly failed for order=%s", req.order_display_id
            )

    ctx.theft_score, ctx.theft_reasons = theft_feature_score(
        {
            "category": req.category,
            "order_value": req.order_value,
            "next_driver_no_order": bool(req.next_driver_no_order),
            "cancel_after_pickup": ctx.after_pickup,
            "stage": ctx.stage,
        },
        policy["theft"],
    )
