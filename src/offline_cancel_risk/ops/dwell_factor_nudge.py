"""Suggest dwell place/vehicle factor nudges from labeled offline FP/FN strata.

Does not write overlays — ops review then PUT /v1/policy overlay.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from offline_cancel_risk.api.schemas import AssessmentResult

_HEAD = "cancelled_offline"


def dwell_factor_nudge_report(
    *,
    assessments: list[AssessmentResult],
    feedback: list[dict[str, Any]],
    policy: dict[str, Any],
    min_support: int = 10,
    min_fill_rate: float = 0.2,
) -> dict[str, Any]:
    """Per place_class: offline FP/FN rates → suggest factor multiply nudge."""
    labels_by_oid: dict[str, Any] = {}
    for row in feedback:
        oid = row.get("order_display_id")
        if not oid:
            continue
        lab = row.get("labels") or {}
        if isinstance(lab, str):
            try:
                lab = json.loads(lab)
            except json.JSONDecodeError:
                lab = {}
        labels_by_oid[str(oid)] = lab if isinstance(lab, dict) else {}

    place_factors = dict((policy.get("dwell") or {}).get("place_factors") or {})
    by_place: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "fp": 0, "fn": 0, "tp": 0, "tn": 0}
    )
    known = 0
    for r in assessments:
        lab = labels_by_oid.get(r.order_display_id)
        if not lab or _HEAD not in lab or lab[_HEAD] is None:
            continue
        truth = int(lab[_HEAD])
        pred = int(r.flags.cancelled_offline)
        place = str((r.gps_window or {}).get("presence_place_class") or "unknown")
        if place != "unknown":
            known += 1
        b = by_place[place]
        b["n"] += 1
        if pred == 1 and truth == 0:
            b["fp"] += 1
        elif pred == 0 and truth == 1:
            b["fn"] += 1
        elif pred == 1 and truth == 1:
            b["tp"] += 1
        else:
            b["tn"] += 1

    labeled_n = sum(b["n"] for b in by_place.values())
    fill_rate = (known / labeled_n) if labeled_n else 0.0
    suggestions: list[dict[str, Any]] = []
    if fill_rate < min_fill_rate:
        return {
            "labeled_n": labeled_n,
            "place_fill_rate_on_labeled": fill_rate,
            "min_fill_rate": min_fill_rate,
            "min_support": min_support,
            "ready": False,
            "reason": "place_fill_rate_below_min",
            "by_place": dict(by_place),
            "suggestions": [],
        }

    for place, b in sorted(by_place.items()):
        if place == "unknown" or b["n"] < min_support:
            continue
        fp_rate = b["fp"] / b["n"]
        fn_rate = b["fn"] / b["n"]
        current = float(place_factors.get(place, place_factors.get("unknown", 1.0)))
        # FP-heavy → raise D (harder presence); FN-heavy → lower D (easier).
        if fp_rate >= 0.25 and fp_rate > fn_rate:
            nudge = 1.1
            action = "raise_factor"
        elif fn_rate >= 0.25 and fn_rate > fp_rate:
            nudge = 0.9
            action = "lower_factor"
        else:
            continue
        suggested = round(max(0.5, min(2.0, current * nudge)), 3)
        suggestions.append(
            {
                "place_class": place,
                "n": b["n"],
                "fp_rate": round(fp_rate, 4),
                "fn_rate": round(fn_rate, 4),
                "current_factor": current,
                "suggested_factor": suggested,
                "action": action,
                "overlay_path": f"dwell.place_factors.{place}",
            }
        )

    return {
        "labeled_n": labeled_n,
        "place_fill_rate_on_labeled": fill_rate,
        "min_fill_rate": min_fill_rate,
        "min_support": min_support,
        "ready": True,
        "by_place": dict(by_place),
        "suggestions": suggestions,
        "note": "Review then PUT market overlay; not auto-applied.",
    }
