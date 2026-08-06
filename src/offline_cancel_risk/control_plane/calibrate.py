"""Score calibrator persistence and fit runner."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.metrics import holdout_split, resolve_scores
from offline_cancel_risk.control_plane.patterns import learning_cfg
from offline_cancel_risk.scoring.calibration import (
    brier_score,
    calibration_cfg,
    expected_calibration_error,
    fit_calibrator,
    predict_calibrated,
)

_LOG = logging.getLogger(__name__)
_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class CalibratorStore:
    def __init__(self, sqlite_path: Path | str) -> None:
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibrators (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  head TEXT NOT NULL,
                  method TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  ece REAL NOT NULL,
                  support INTEGER NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code, head)
                )
                """
            )
            conn.commit()

    def upsert(
        self,
        *,
        region_code: str,
        city_code: str,
        head: str,
        method: str,
        params: dict[str, Any],
        ece: float,
        support: int,
    ) -> None:
        region_code = (region_code or "").strip().upper()
        city_code = (city_code or "").strip().upper()
        updated = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO calibrators(
                  region_code, city_code, head, method, params_json,
                  ece, support, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region_code, city_code, head) DO UPDATE SET
                  method=excluded.method,
                  params_json=excluded.params_json,
                  ece=excluded.ece,
                  support=excluded.support,
                  updated_at=excluded.updated_at
                """,
                (
                    region_code,
                    city_code,
                    head,
                    method,
                    json.dumps(params),
                    float(ece),
                    int(support),
                    updated,
                ),
            )
            conn.commit()

    def get(self, region: str, city: str, head: str) -> dict[str, Any] | None:
        region_code = (region or "").strip().upper()
        city_code = (city or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM calibrators
                WHERE region_code=? AND city_code=? AND head=?
                """,
                (region_code, city_code, head),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_market(self, region: str, city: str = "") -> list[dict[str, Any]]:
        region_code = (region or "").strip().upper()
        city_code = (city or "").strip().upper()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM calibrators
                WHERE region_code=? AND city_code=?
                ORDER BY head
                """,
                (region_code, city_code),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        return d


class CalibrationRunStore:
    def __init__(self, sqlite_path: Path | str) -> None:
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  decision TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def record(self, *, region_code: str, city_code: str, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO calibration_runs(
                  region_code, city_code, mode, decision, reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    region_code.strip().upper(),
                    (city_code or "").strip().upper(),
                    str(report.get("mode")),
                    str(report.get("decision")),
                    str(report.get("reason")),
                    json.dumps(report),
                    _utc_now_iso(),
                ),
            )
            conn.commit()

    def latest(self, region_code: str, city_code: str = "") -> dict[str, Any] | None:
        region = region_code.strip().upper()
        city = (city_code or "").strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM calibration_runs
                WHERE region_code=? AND city_code=?
                ORDER BY id DESC LIMIT 1
                """,
                (region, city),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])


@dataclass
class CalibrationFitContext:
    base_policy: dict[str, Any]
    audit: PolicyAuditLog
    calibrators: CalibratorStore
    assessments: list[dict[str, Any]]
    feedback: list[dict[str, Any]]
    region_code: str
    city_code: str = ""
    mode_override: str | None = None
    run_store: CalibrationRunStore | None = None


def _labeled_pairs_for_head(
    assessments: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    *,
    head: str,
    policy: dict[str, Any],
    region: str,
    city: str,
) -> list[dict[str, Any]]:
    """All labeled market pairs (full score support — not pattern cohort only)."""
    del policy  # reserved for future filters; fit uses full labeled support
    by_id = {a["order_display_id"]: a for a in assessments}
    pairs: list[dict[str, Any]] = []
    for fb in feedback:
        oid = fb.get("order_display_id")
        if not oid:
            continue
        assess = by_id.get(oid)
        if assess is None:
            continue
        if region and (assess.get("region_code") or "").strip().upper() != region:
            continue
        if city and (assess.get("city_code") or "").strip().upper() != city:
            continue
        labels = fb.get("labels") or {}
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except json.JSONDecodeError:
                labels = {}
        if head not in labels or labels[head] is None:
            continue
        scores = resolve_scores(assess, blend=None)
        pairs.append(
            {
                "order_display_id": oid,
                "x": float(scores[head]),
                "y": int(labels[head]),
                "labels": labels,
            }
        )
    return pairs


def _finish(
    ctx: CalibrationFitContext,
    report: dict[str, Any],
    *,
    region: str,
    city: str,
) -> dict[str, Any]:
    if ctx.run_store is not None:
        ctx.run_store.record(region_code=region, city_code=city, report=report)
    return report


def run_calibration_fit(ctx: CalibrationFitContext) -> dict[str, Any]:
    region = ctx.region_code.strip().upper()
    city = (ctx.city_code or "").strip().upper()
    cfg = calibration_cfg(ctx.base_policy)
    mode = (ctx.mode_override or cfg["mode"]).strip().lower()
    if mode not in {"shadow", "apply", "off"}:
        mode = "shadow"

    report: dict[str, Any] = {
        "region_code": region,
        "city_code": city,
        "mode": mode,
        "decision": "skipped",
        "reason": "",
        "heads": {},
        "created_at": _utc_now_iso(),
    }

    if mode == "off":
        report["reason"] = "mode_off"
        return _finish(ctx, report, region=region, city=city)

    learn = learning_cfg(ctx.base_policy)
    min_support = max(int(cfg["min_labeled"]), int(learn["min_pattern_support"]))
    candidates: dict[str, dict[str, Any]] = {}
    head_reports: dict[str, dict[str, Any]] = {}

    for head in _HEADS:
        pairs = _labeled_pairs_for_head(
            ctx.assessments,
            ctx.feedback,
            head=head,
            policy=ctx.base_policy,
            region=region,
            city=city,
        )
        head_info: dict[str, Any] = {"support": len(pairs), "min_support": min_support}
        if len(pairs) < min_support:
            head_info.update(
                {"decision": "rejected", "reason": "insufficient_labels"}
            )
            head_reports[head] = head_info
            continue

        train_rows, hold_rows = holdout_split(
            pairs, holdout_fraction=float(cfg["holdout_fraction"])
        )
        if not hold_rows:
            head_info.update(
                {"decision": "rejected", "reason": "empty_holdout"}
            )
            head_reports[head] = head_info
            continue

        train_xs = [float(r["x"]) for r in train_rows]
        train_ys = [int(r["y"]) for r in train_rows]
        try:
            model = fit_calibrator(
                train_xs, train_ys, platt_max_n=int(cfg["platt_max_n"])
            )
        except ValueError as exc:
            head_info.update(
                {"decision": "rejected", "reason": f"fit_failed:{exc}"}
            )
            head_reports[head] = head_info
            continue

        hold_xs = [float(r["x"]) for r in hold_rows]
        hold_ys = [int(r["y"]) for r in hold_rows]
        hold_probs = [predict_calibrated(model, x) for x in hold_xs]
        ece = expected_calibration_error(
            hold_probs,
            hold_ys,
            n_bins=int(cfg["ece_bins"]),
            strategy=str(cfg["ece_strategy"]),
        )
        brier = brier_score(hold_probs, hold_ys)
        head_info.update(
            {
                "method": model["method"],
                "ece": ece,
                "brier": brier,
                "train_support": len(train_xs),
                "holdout_support": len(hold_rows),
            }
        )
        if ece > float(cfg["max_ece"]) + 1e-12:
            head_info.update({"decision": "rejected", "reason": "high_ece"})
            head_reports[head] = head_info
            continue
        if brier > float(cfg["max_brier"]) + 1e-12:
            head_info.update({"decision": "rejected", "reason": "high_brier"})
            head_reports[head] = head_info
            continue

        candidates[head] = {
            "model": model,
            "ece": ece,
            "brier": brier,
            "support": len(pairs),
            "head_info": head_info,
        }
        head_info["decision"] = "candidate"
        head_reports[head] = head_info

    report["heads"] = head_reports

    if not candidates:
        reasons = {h: hr.get("reason") for h, hr in head_reports.items()}
        if all(r == "insufficient_labels" for r in reasons.values()):
            reason = "insufficient_labels"
        elif any(r == "high_ece" for r in reasons.values()):
            reason = "high_ece"
        elif any(r == "high_brier" for r in reasons.values()):
            reason = "high_brier"
        elif any(r == "empty_holdout" for r in reasons.values()):
            reason = "empty_holdout"
        else:
            reason = "no_head_passed_gates"
        report.update({"decision": "rejected", "reason": reason})
        ctx.audit.append(
            actor="calibrator",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason=reason,
            metrics_before={"heads": head_reports},
        )
        return _finish(ctx, report, region=region, city=city)

    last = ctx.audit.last_apply_at(region, city, head="calibration")
    if last is not None and _parse_ts(last) > datetime.now(timezone.utc) - timedelta(
        minutes=int(cfg["cooldown_minutes"])
    ):
        for head, cand in candidates.items():
            cand["head_info"].update({"decision": "rejected", "reason": "cooldown"})
        report.update({"decision": "rejected", "reason": "cooldown"})
        ctx.audit.append(
            actor="calibrator",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="cooldown",
            metrics_before={"heads": head_reports},
        )
        return _finish(ctx, report, region=region, city=city)

    after_cal: dict[str, Any] = {}
    for head, cand in candidates.items():
        model = cand["model"]
        ece = float(cand["ece"])
        brier = float(cand["brier"])
        support = int(cand["support"])
        ctx.calibrators.upsert(
            region_code=region,
            city_code=city,
            head=head,
            method=str(model["method"]),
            params=dict(model["params"]),
            ece=ece,
            support=support,
        )
        cand["head_info"].update({"decision": "fitted", "reason": "holdout_ok"})
        after_cal[head] = {
            "method": model["method"],
            "ece": ece,
            "brier": brier,
            "support": support,
            "params": model["params"],
        }

    report.update({"decision": "fitted", "reason": "holdout_ok"})
    ctx.audit.append(
        actor="calibrator",
        action="apply",
        region_code=region,
        city_code=city,
        decision="accepted",
        reason="holdout_ok",
        after={"calibration": after_cal},
        metrics_after={"heads": head_reports},
    )
    _LOG.info(
        "calibration fitted region=%s city=%s heads=%s",
        region,
        city,
        sorted(after_cal),
    )
    return _finish(ctx, report, region=region, city=city)
