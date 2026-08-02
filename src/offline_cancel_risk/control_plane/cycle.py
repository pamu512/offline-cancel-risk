"""Shared metrics + tuner cycle for API, debounce, and scheduled tick."""

from __future__ import annotations

from typing import Any

from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.forecast import SupplyForecastStore
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.metrics import (
    LabelMetricsStore,
    compute_label_metrics,
)
from offline_cancel_risk.control_plane.tuner import TunerContext, run_tuner
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.service import resolved_policy_for_market
from offline_cancel_risk.settings import Settings


def assessments_as_dicts(table: SqliteTablePublisher) -> list[dict[str, Any]]:
    return [
        {
            "order_display_id": r.order_display_id,
            "region_code": r.region_code,
            "city_code": r.city_code,
            "scores": r.scores.model_dump(),
            "rule_scores": r.rule_scores.model_dump(),
            "ml_scores": r.ml_scores.model_dump(),
            "attention_score": float(r.attention_score),
        }
        for r in table.list_latest_assessments()
    ]


def markets_from_assessments(
    assessments: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for a in assessments:
        region = (a.get("region_code") or "").strip().upper()
        city = (a.get("city_code") or "").strip().upper()
        if not region:
            continue
        key = (region, city)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def run_metrics_and_tune(
    *,
    settings: Settings,
    policy: dict[str, Any],
    guardrails: dict[str, Any],
    overlays: PolicyOverlayStore,
    audit: PolicyAuditLog,
    forecast: SupplyForecastStore,
    hardgates: EnforcementHardgateStore,
    label_metrics: LabelMetricsStore,
    op_cfg: dict[str, Any],
    table: SqliteTablePublisher,
    region_code: str,
    city_code: str = "",
    reason: str = "tuning_run",
) -> dict[str, Any]:
    feedback = table.list_feedback()
    assessments = assessments_as_dicts(table)
    resolved = resolved_policy_for_market(
        policy,
        overlays,
        region_code=region_code or None,
        city_code=city_code or None,
    )
    snapshots = compute_label_metrics(
        assessments,
        feedback,
        thresholds={k: float(v) for k, v in (resolved.get("thresholds") or {}).items()},
        region_code=region_code,
        city_code=city_code,
    )
    label_metrics.save_snapshots(snapshots)
    audit.append(
        actor="tuner",
        action="metrics_snapshot",
        region_code=region_code,
        city_code=city_code,
        after={"heads": [s["head"] for s in snapshots]},
        decision="recorded",
        reason=reason,
    )
    decisions = run_tuner(
        TunerContext(
            base_policy=policy,
            guardrails=guardrails,
            overlays=overlays,
            audit=audit,
            forecast=forecast,
            hardgates=hardgates,
            op_cfg=op_cfg,
            assessments=assessments,
            feedback=feedback,
            region_code=region_code,
            city_code=city_code,
            min_labeled=settings.tuner_min_labeled,
            cooldown_minutes=settings.tuner_cooldown_minutes,
            min_f1_lift=settings.tuner_min_f1_lift,
        )
    )
    return {"metrics": snapshots, "decisions": decisions}
