"""Labeled holdout eval: model vs naive baselines + pattern-cohort precision."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from offline_cancel_risk.adapters.events import CsvOrdersClient
from offline_cancel_risk.adapters.gps import CsvGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.control_plane.patterns import in_pattern_cohort, learning_cfg
from offline_cancel_risk.domain.models import GpsPoint
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import ROOT, load_policy
from offline_cancel_risk.timeutil import parse_ts

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")

PICKUP = (14.5500, 121.0200)
DEST = (14.6500, 121.0800)


@dataclass(frozen=True)
class LabeledCase:
    req: AssessRequest
    labels: dict[str, int]
    points: list[GpsPoint]


class _MultiGpsClient:
    def __init__(self, tracks: dict[int, list[GpsPoint]]) -> None:
        self._tracks = tracks

    async def fetch_track(
        self, driver_id: int, start: datetime, end: datetime
    ) -> list[GpsPoint]:
        return [
            p
            for p in self._tracks.get(int(driver_id), [])
            if start <= parse_ts(p.ts) <= end
        ]


def _cluster(
    *,
    center: tuple[float, float],
    start: datetime,
    n: int = 36,
    step_minutes: int = 2,
) -> list[GpsPoint]:
    points: list[GpsPoint] = []
    for i in range(n):
        points.append(
            GpsPoint(
                lat=center[0] + (i % 5) * 0.00001,
                lon=center[1] + (i % 3) * 0.00001,
                ts=(start + timedelta(minutes=i * step_minutes)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                speed_mps=0.3,
            )
        )
    return points


def _csv_demo_cases() -> list[LabeledCase]:
    """Seed cases from examples/csv_demo (known offline + theft labels)."""
    demo = ROOT / "examples" / "csv_demo"
    labeled = CsvOrdersClient(demo / "sample_orders.csv").load_labeled()
    gps = CsvGpsClient(demo / "sample_gps.csv")
    # ponytail: pull full driver tracks once via private cache loader
    rows = gps._load()  # noqa: SLF001
    by_driver: dict[int, list[GpsPoint]] = {}
    for did, point in rows:
        by_driver.setdefault(int(did), []).append(point)
    out: list[LabeledCase] = []
    for req, labels in labeled:
        out.append(
            LabeledCase(
                req=req,
                labels={h: int(labels[h] or 0) for h in _HEADS},
                points=list(by_driver.get(int(req.driver_id), [])),
            )
        )
    return out


def default_holdout_cases() -> list[LabeledCase]:
    """Labeled pack — CSV demo seed + synthetic theft/clean; CI-stable."""
    cases: list[LabeledCase] = list(_csv_demo_cases())

    # Extra food theft at pickup (pattern: selective_theft)
    for i, did in enumerate((111, 112)):
        day = i + 5
        assign = datetime(2024, 2, day, 10, 0, 0)
        cancel = datetime(2024, 2, day, 11, 20, 0)
        req = AssessRequest(
            order_display_id=f"EVAL-THEFT-{i+1}",
            driver_id=did,
            cancel_ts=cancel.strftime("%Y-%m-%d %H:%M:%S"),
            assign_ts=assign.strftime("%Y-%m-%d %H:%M:%S"),
            latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
            path_point_num=2,
            order_status="CANCELLED",
            category="FOOD",
            order_value=800.0 + i * 10,
            currency="PHP",
            next_driver_no_order=True,
        )
        cases.append(
            LabeledCase(
                req=req,
                labels={
                    "cancelled_offline": 0,
                    "cancel_abuse": 0,
                    "selective_theft": 1,
                },
                points=_cluster(center=PICKUP, start=assign),
            )
        )

    # Clean negatives (low value, replacement present)
    for i, did in enumerate((301, 302, 303)):
        day = i + 20
        assign = datetime(2024, 3, day, 10, 0, 0)
        cancel = datetime(2024, 3, day, 10, 25, 0)
        req = AssessRequest(
            order_display_id=f"EVAL-CLEAN-{i+1}",
            driver_id=did,
            cancel_ts=cancel.strftime("%Y-%m-%d %H:%M:%S"),
            assign_ts=assign.strftime("%Y-%m-%d %H:%M:%S"),
            latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
            path_point_num=2,
            order_status="CANCELLED",
            category="HAUL",
            order_value=40.0,
            currency="PHP",
            next_driver_no_order=False,
            replacement_order_id=f"REP-{i+1}",
            replacement_placed_at=cancel.strftime("%Y-%m-%d %H:%M:%S"),
            replacement_latlong=f"{PICKUP[0]}|{PICKUP[1]},{DEST[0]}|{DEST[1]}",
        )
        cases.append(
            LabeledCase(
                req=req,
                labels={
                    "cancelled_offline": 0,
                    "cancel_abuse": 0,
                    "selective_theft": 0,
                },
                points=_cluster(center=PICKUP, start=assign, n=8, step_minutes=1),
            )
        )

    return cases


def _empty_counts() -> dict[str, int]:
    return {"tp": 0, "fp": 0, "tn": 0, "fn": 0}


def _finalize(counts: dict[str, int]) -> dict[str, float | int]:
    tp, fp, tn, fn = counts["tp"], counts["fp"], counts["tn"], counts["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "support": tp + fp + tn + fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _tally(
    *,
    pred: int,
    truth: int,
    bucket: dict[str, int],
) -> None:
    if pred == 1 and truth == 1:
        bucket["tp"] += 1
    elif pred == 1 and truth == 0:
        bucket["fp"] += 1
    elif pred == 0 and truth == 0:
        bucket["tn"] += 1
    else:
        bucket["fn"] += 1


def _baseline_pred(name: str) -> int:
    if name == "never":
        return 0
    if name == "always":
        return 1
    raise ValueError(name)


def run_holdout_eval(
    *,
    policy_path: Path | str | None = None,
    cases: list[LabeledCase] | None = None,
    out_path: Path | str | None = None,
) -> dict[str, Any]:
    """Score labeled cases; emit overall + pattern-cohort metrics vs baselines."""
    policy = load_policy(
        Path(policy_path) if policy_path else ROOT / "config" / "policy.default.yaml"
    )
    pack = cases if cases is not None else default_holdout_cases()
    tracks = {c.req.driver_id: c.points for c in pack}
    gps = _MultiGpsClient(tracks)

    model_all = {h: _empty_counts() for h in _HEADS}
    model_pattern = {h: _empty_counts() for h in _HEADS}
    always_all = {h: _empty_counts() for h in _HEADS}
    never_all = {h: _empty_counts() for h in _HEADS}
    pattern_n = {h: 0 for h in _HEADS}

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="ocr-holdout-") as tmp:
            tmp_path = Path(tmp)
            stream = JsonlStreamPublisher(stream_path=tmp_path / "risk_events.jsonl")
            table = SqliteTablePublisher(sqlite_path=tmp_path / "assessments.db")
            for case in pack:
                result: AssessmentResult = await assess_order(
                    case.req, gps, policy, stream=stream, table=table
                )
                flags = result.flags.model_dump()
                for head in _HEADS:
                    truth = int(case.labels[head])
                    pred = int(flags[head])
                    _tally(pred=pred, truth=truth, bucket=model_all[head])
                    _tally(
                        pred=_baseline_pred("always"),
                        truth=truth,
                        bucket=always_all[head],
                    )
                    _tally(
                        pred=_baseline_pred("never"),
                        truth=truth,
                        bucket=never_all[head],
                    )
                    if in_pattern_cohort(result, head, policy):
                        pattern_n[head] += 1
                        _tally(pred=pred, truth=truth, bucket=model_pattern[head])

    asyncio.run(_run())

    learn = learning_cfg(policy)
    report: dict[str, Any] = {
        "order_count": len(pack),
        "target_precision": learn["target_precision"],
        "by_head": {},
        "baselines": {
            "always": {},
            "never": {},
        },
        "pattern_cohort": {},
        "beats_always_precision": {},
    }
    for head in _HEADS:
        model_m = _finalize(model_all[head])
        always_m = _finalize(always_all[head])
        never_m = _finalize(never_all[head])
        pattern_m = _finalize(model_pattern[head])
        report["by_head"][head] = model_m
        report["baselines"]["always"][head] = always_m
        report["baselines"]["never"][head] = never_m
        report["pattern_cohort"][head] = {
            **pattern_m,
            "cohort_n": pattern_n[head],
        }
        # Precision lift vs always-flag when model made ≥1 positive prediction.
        if (model_m["tp"] + model_m["fp"]) > 0 and (always_m["tp"] + always_m["fp"]) > 0:
            report["beats_always_precision"][head] = (
                float(model_m["precision"]) >= float(always_m["precision"])
            )
        else:
            report["beats_always_precision"][head] = True

    if out_path is not None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def assert_floors(report: dict[str, Any], floors: dict[str, Any]) -> None:
    """Raise AssertionError if report misses CI floors."""
    min_orders = int(floors.get("min_order_count", 1))
    if int(report["order_count"]) < min_orders:
        raise AssertionError(
            f"order_count {report['order_count']} < min {min_orders}"
        )
    for head, spec in (floors.get("by_head") or {}).items():
        got = report["by_head"][head]
        if "min_precision" in spec and float(got["precision"]) < float(
            spec["min_precision"]
        ):
            raise AssertionError(
                f"{head} precision {got['precision']} < {spec['min_precision']}"
            )
        if "min_recall" in spec and float(got["recall"]) < float(spec["min_recall"]):
            raise AssertionError(
                f"{head} recall {got['recall']} < {spec['min_recall']}"
            )
    for head, must_beat in (floors.get("beats_always_precision") or {}).items():
        if must_beat and not report["beats_always_precision"].get(head, False):
            raise AssertionError(f"{head} does not beat always-flag precision")
    for head, spec in (floors.get("pattern_cohort") or {}).items():
        got = report["pattern_cohort"][head]
        if "min_precision" in spec and (got["tp"] + got["fp"]) > 0:
            if float(got["precision"]) < float(spec["min_precision"]):
                raise AssertionError(
                    f"pattern {head} precision {got['precision']} "
                    f"< {spec['min_precision']}"
                )
