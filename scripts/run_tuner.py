#!/usr/bin/env python3
"""Run metrics + constrained threshold tuner for a market."""

from __future__ import annotations

import argparse
import json
import sys

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
from offline_cancel_risk.settings import get_settings, load_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region-code", required=True)
    parser.add_argument("--city-code", default="")
    args = parser.parse_args(argv)

    settings = get_settings()
    cp = settings.control_plane_sqlite_path
    table = SqliteTablePublisher(settings.sqlite_path)
    feedback = table.list_feedback()
    assessments = [
        {
            "order_display_id": r.order_display_id,
            "region_code": r.region_code,
            "city_code": r.city_code,
            "scores": r.scores.model_dump(),
        }
        for r in table.list_latest_assessments()
    ]
    policy = load_policy(settings.policy_path)
    guardrails = load_policy(settings.policy_guardrails_path)
    overlays = PolicyOverlayStore(settings.policy_overlays_path)
    resolved = resolved_policy_for_market(
        policy,
        overlays,
        region_code=args.region_code,
        city_code=args.city_code or None,
    )
    snapshots = compute_label_metrics(
        assessments,
        feedback,
        thresholds={k: float(v) for k, v in (resolved.get("thresholds") or {}).items()},
        region_code=args.region_code,
        city_code=args.city_code,
    )
    LabelMetricsStore(cp).save_snapshots(snapshots)
    decisions = run_tuner(
        TunerContext(
            base_policy=policy,
            guardrails=guardrails,
            overlays=overlays,
            audit=PolicyAuditLog(cp),
            forecast=SupplyForecastStore(cp),
            hardgates=EnforcementHardgateStore(cp),
            op_cfg=load_policy(settings.operating_point_path),
            assessments=assessments,
            feedback=feedback,
            region_code=args.region_code,
            city_code=args.city_code,
            min_labeled=settings.tuner_min_labeled,
            cooldown_minutes=settings.tuner_cooldown_minutes,
            min_f1_lift=settings.tuner_min_f1_lift,
        )
    )
    print(json.dumps({"metrics": snapshots, "decisions": decisions}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
