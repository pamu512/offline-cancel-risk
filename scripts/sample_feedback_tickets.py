#!/usr/bin/env python3
"""Batch-fill daily label ticket quota."""

from __future__ import annotations

import argparse
import json
import sys

from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.control_plane.metrics import LabelMetricsStore
from offline_cancel_risk.feedback.sampler import (
    bias_hints_from_metrics,
    run_batch_sample,
)
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.settings import get_settings, load_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region-code", default="")
    parser.add_argument("--city-code", default="")
    args = parser.parse_args(argv)
    settings = get_settings()
    table = SqliteTablePublisher(settings.sqlite_path)
    store = LabelTicketStore(
        settings.label_tickets_path,
        stream_path=settings.label_tickets_stream_path,
    )
    labeled = {f["order_display_id"] for f in table.list_feedback()}
    hints = bias_hints_from_metrics(
        LabelMetricsStore(settings.control_plane_sqlite_path).latest(limit=50)
    )
    created = run_batch_sample(
        store,
        table.list_latest_assessments(),
        load_policy(settings.policy_path),
        labeled_order_ids=labeled,
        bias_hints=hints,
        region_code=args.region_code,
        city_code=args.city_code,
    )
    print(
        json.dumps(
            {
                "created": len(created),
                "ticket_ids": [t["ticket_id"] for t in created],
                "day_count": store.day_count(),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
