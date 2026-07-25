#!/usr/bin/env python3
"""Compute and persist label metrics from assessments + feedback."""

from __future__ import annotations

import json
import sys

from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.control_plane.metrics import (
    LabelMetricsStore,
    compute_label_metrics,
)
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.service import resolved_policy_for_market
from offline_cancel_risk.settings import get_settings, load_policy


def main(argv: list[str] | None = None) -> int:
    del argv
    settings = get_settings()
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
    overlays = PolicyOverlayStore(settings.policy_overlays_path)
    resolved = resolved_policy_for_market(
        policy, overlays, region_code=None, city_code=None
    )
    rows = compute_label_metrics(
        assessments,
        feedback,
        thresholds={k: float(v) for k, v in (resolved.get("thresholds") or {}).items()},
    )
    store = LabelMetricsStore(settings.control_plane_sqlite_path)
    store.save_snapshots(rows)
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
