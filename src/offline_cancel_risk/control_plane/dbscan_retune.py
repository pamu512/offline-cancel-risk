"""Market retune of v5 DBSCAN eps + min_pts from labeled pattern cohort + GPS cache."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.api.schemas import AssessRequest, AssessmentResult
from offline_cancel_risk.control_plane.audit import PolicyAuditLog
from offline_cancel_risk.control_plane.hardgates import EnforcementHardgateStore
from offline_cancel_risk.control_plane.metrics import (
    compute_label_metrics,
    holdout_split,
)
from offline_cancel_risk.control_plane.patterns import learning_cfg
from offline_cancel_risk.features.gps_cache import AssessGpsCache
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.policy.overlays import PolicyOverlayStore
from offline_cancel_risk.policy.resolve import GuardrailError, deep_merge
from offline_cancel_risk.policy.service import resolved_policy_for_market, save_overlay

_LOG = logging.getLogger(__name__)
_HEAD = "cancelled_offline"


class _NullStream:
    def publish(self, result: AssessmentResult) -> None:
        return None


class _NullTable:
    def upsert(self, result: AssessmentResult) -> None:
        return None

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    def latest(self, order_display_id: str) -> None:
        return None

    def next_generation(self, order_display_id: str) -> int:
        return 1

    def mark_prior_provisional(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def list_generations(self, order_display_id: str) -> list:
        return []

    def upsert_feedback(self, *args: Any, **kwargs: Any) -> None:
        return None

    def list_feedback(self) -> list:
        return []

    def list_latest_assessments(self) -> list:
        return []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def dbscan_retune_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    raw = dict(policy.get("dbscan_retune") or {})
    grid = dict(raw.get("grid") or {})
    return {
        "mode": str(raw.get("mode", "shadow")).strip().lower(),
        "on_tick": bool(raw.get("on_tick", False)),
        "cooldown_minutes": int(raw.get("cooldown_minutes", 1440)),
        "min_labeled": int(raw.get("min_labeled", 15)),
        "min_recall_lift": float(raw.get("min_recall_lift", 0.01)),
        "cache_retention_days": int(raw.get("cache_retention_days", 30)),
        "holdout_fraction": float(raw.get("holdout_fraction", 0.3)),
        "grid": {
            "clustering_radius_m": [
                float(x) for x in (grid.get("clustering_radius_m") or [30, 40, 50, 60, 80])
            ],
            "min_pts": [int(x) for x in (grid.get("min_pts") or [5, 7, 9, 11, 15])],
        },
    }


@dataclass
class DbscanRetuneContext:
    base_policy: dict[str, Any]
    guardrails: dict[str, Any]
    overlays: PolicyOverlayStore
    audit: PolicyAuditLog
    hardgates: EnforcementHardgateStore
    gps_cache: AssessGpsCache
    feedback: list[dict[str, Any]]
    region_code: str
    city_code: str = ""
    mode_override: str | None = None
    run_store: "DbscanRetuneStore | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


class DbscanRetuneStore:
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
                CREATE TABLE IF NOT EXISTS dbscan_retune_runs (
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
                INSERT INTO dbscan_retune_runs(
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
                SELECT payload_json FROM dbscan_retune_runs
                WHERE region_code=? AND city_code=?
                ORDER BY id DESC LIMIT 1
                """,
                (region, city),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])


def _within_bounds(bounds: dict[str, Any], key: str, value: float | int) -> bool:
    b = bounds.get(key)
    if not b:
        return True
    return float(b["min"]) <= float(value) <= float(b["max"])


def _candidate_grid(cfg: dict[str, Any], bounds: dict[str, Any]) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for eps in cfg["grid"]["clustering_radius_m"]:
        if not _within_bounds(bounds, "dbscan.clustering_radius_m", eps):
            continue
        for min_pts in cfg["grid"]["min_pts"]:
            if not _within_bounds(bounds, "dbscan.min_pts", min_pts):
                continue
            out.append((float(eps), int(min_pts)))
    return out


def _passes_gates(
    m: dict[str, Any],
    *,
    target_precision: float,
    min_pattern_recall: float,
) -> bool:
    if float(m["precision"]) + 1e-12 < target_precision:
        return False
    if float(m["recall"]) + 1e-12 < min_pattern_recall:
        return False
    return True


def _better(hold: dict[str, Any], best: dict[str, Any] | None) -> bool:
    if best is None:
        return True
    bm = best["holdout_metrics"]
    if float(hold["recall"]) > float(bm["recall"]) + 1e-12:
        return True
    if abs(float(hold["recall"]) - float(bm["recall"])) < 1e-12:
        if float(hold["precision"]) > float(bm["precision"]) + 1e-12:
            return True
    return False


async def _replay_assessments(
    rows: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    stream = _NullStream()
    table = _NullTable()
    out: list[dict[str, Any]] = []
    for row in rows:
        req = AssessRequest.model_validate(row["request"])
        gps = FakeGpsClient(row["points"])
        result = await assess_order(req, gps, policy, stream=stream, table=table)
        out.append(
            {
                "order_display_id": result.order_display_id,
                "region_code": result.region_code or req.region_code,
                "city_code": result.city_code or req.city_code,
                "scores": result.scores.model_dump(),
                "rule_scores": result.rule_scores.model_dump(),
                "ml_scores": result.ml_scores.model_dump(),
                "flags": result.flags.model_dump(),
                "reasons": list(result.reasons),
            }
        )
    return out


def _metrics(
    assessments: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    region: str,
    city: str,
) -> dict[str, Any] | None:
    thr = {h: float(v) for h, v in (policy.get("thresholds") or {}).items()}
    rows = compute_label_metrics(
        assessments,
        feedback,
        thresholds=thr,
        region_code=region,
        city_code=city,
        pattern_policy=policy,
    )
    for r in rows:
        if r["head"] == _HEAD:
            return r
    return None


def _projected_offline_flags(assessments: list[dict[str, Any]]) -> int:
    n = 0
    for a in assessments:
        flags = a.get("flags") or {}
        if int(flags.get(_HEAD, 0)) == 1:
            n += 1
    return n


def _hardgate_ok(
    hardgates: EnforcementHardgateStore,
    *,
    region: str,
    city: str,
    projected: int,
) -> tuple[bool, str]:
    caps = hardgates.effective_caps(region, city)
    for window in ("hour", "day", "week"):
        row = caps.get(window)
        if row is None:
            continue
        if projected > int(row["max_enforcements"]):
            return False, f"breaches_{window}_cap"
        return True, ""
    return True, ""


def run_dbscan_retune(ctx: DbscanRetuneContext) -> dict[str, Any]:
    region = ctx.region_code.strip().upper()
    city = (ctx.city_code or "").strip().upper()
    cfg = dbscan_retune_cfg(ctx.base_policy)
    mode = (ctx.mode_override or cfg["mode"]).strip().lower()
    if mode not in {"shadow", "apply", "off"}:
        mode = "shadow"

    report: dict[str, Any] = {
        "region_code": region,
        "city_code": city,
        "mode": mode,
        "decision": "skipped",
        "reason": "",
        "created_at": _utc_now_iso(),
    }

    if mode == "off":
        report["reason"] = "mode_off"
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    ctx.gps_cache.prune(cfg["cache_retention_days"])
    learn = learning_cfg(ctx.base_policy)
    target_precision = float(learn["target_precision"])
    min_pattern_recall = float(learn["min_pattern_recall"])
    min_support = max(int(learn["min_pattern_support"]), int(cfg["min_labeled"]))

    cached = ctx.gps_cache.latest_for_market(region, city)
    by_oid = {c["order_display_id"]: c for c in cached}
    labeled_rows: list[dict[str, Any]] = []
    labeled_fb: list[dict[str, Any]] = []
    for fb in ctx.feedback:
        oid = fb.get("order_display_id")
        if not oid or oid not in by_oid:
            continue
        labels = fb.get("labels") or {}
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except json.JSONDecodeError:
                labels = {}
        if labels.get(_HEAD) is None:
            continue
        labeled_rows.append(by_oid[oid])
        labeled_fb.append({"order_display_id": oid, "labels": labels})

    if len(labeled_rows) < min_support:
        report.update(
            {
                "decision": "rejected",
                "reason": "insufficient_labeled_cache",
                "support": len(labeled_rows),
                "min_support": min_support,
            }
        )
        ctx.audit.append(
            actor="dbscan_retuner",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="insufficient_labeled_cache",
            metrics_before={"support": len(labeled_rows)},
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    bounds = ctx.guardrails.get("bounds") or {}
    candidates = _candidate_grid(cfg, bounds)
    if not candidates:
        report.update({"decision": "rejected", "reason": "empty_grid"})
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    resolved = resolved_policy_for_market(
        ctx.base_policy, ctx.overlays, region_code=region, city_code=city or None
    )
    train_fb, hold_fb = holdout_split(
        labeled_fb, holdout_fraction=float(cfg["holdout_fraction"])
    )
    eval_fb = hold_fb if hold_fb else train_fb

    async def _baseline_and_search() -> tuple[
        dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]
    ]:
        base_assess = await _replay_assessments(labeled_rows, resolved)
        current_hold = _metrics(
            base_assess, eval_fb, policy=resolved, region=region, city=city
        )
        best: dict[str, Any] | None = None
        tried: list[dict[str, Any]] = []
        for eps, min_pts in candidates:
            trial = deepcopy(resolved)
            trial.setdefault("dbscan", {})
            trial["dbscan"]["clustering_radius_m"] = eps
            trial["dbscan"]["min_pts"] = min_pts
            assess = await _replay_assessments(labeled_rows, trial)
            train_m = _metrics(
                assess, train_fb, policy=trial, region=region, city=city
            )
            hold_m = _metrics(
                assess, eval_fb, policy=trial, region=region, city=city
            )
            entry = {
                "clustering_radius_m": eps,
                "min_pts": min_pts,
                "train_metrics": train_m,
                "holdout_metrics": hold_m,
                "projected_flags": _projected_offline_flags(assess),
            }
            tried.append(entry)
            if train_m is None or hold_m is None:
                continue
            if int(train_m.get("support") or 0) < min_support:
                continue
            if not _passes_gates(
                hold_m,
                target_precision=target_precision,
                min_pattern_recall=min_pattern_recall,
            ):
                continue
            ok_hg, hg_reason = _hardgate_ok(
                ctx.hardgates,
                region=region,
                city=city,
                projected=int(entry["projected_flags"]),
            )
            if not ok_hg:
                entry["hardgate"] = hg_reason
                continue
            if _better(hold_m, best):
                best = entry
        return current_hold, best, tried

    current_hold, best, tried = asyncio.run(_baseline_and_search())
    report["candidates_tried"] = len(tried)
    report["current_holdout"] = current_hold
    report["best"] = best

    if best is None:
        report.update({"decision": "rejected", "reason": "no_candidate_in_gates"})
        ctx.audit.append(
            actor="dbscan_retuner",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="no_candidate_in_gates",
            metrics_before=current_hold,
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    cur_rec = float((current_hold or {}).get("recall") or 0.0)
    best_rec = float(best["holdout_metrics"]["recall"])
    lift = best_rec - cur_rec
    report["recall_lift"] = lift
    if lift + 1e-12 < float(cfg["min_recall_lift"]):
        report.update({"decision": "rejected", "reason": "insufficient_recall_lift"})
        ctx.audit.append(
            actor="dbscan_retuner",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="insufficient_recall_lift",
            metrics_before=current_hold,
            metrics_after=best["holdout_metrics"],
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    overlay = {
        "dbscan": {
            "clustering_radius_m": best["clustering_radius_m"],
            "min_pts": best["min_pts"],
        }
    }
    report["suggested"] = overlay

    last = ctx.audit.last_apply_at(region, city, head="dbscan")
    if last is not None and _parse_ts(last) > datetime.now(timezone.utc) - timedelta(
        minutes=int(cfg["cooldown_minutes"])
    ):
        report.update({"decision": "rejected", "reason": "cooldown"})
        ctx.audit.append(
            actor="dbscan_retuner",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason="cooldown",
            after=overlay,
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    if mode == "shadow":
        report.update({"decision": "shadow", "reason": "shadow_no_write"})
        ctx.audit.append(
            actor="dbscan_retuner",
            action="suggest",
            region_code=region,
            city_code=city,
            decision="shadow",
            reason="shadow_no_write",
            after=overlay,
            metrics_before=current_hold,
            metrics_after=best["holdout_metrics"],
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    # mode == apply
    try:
        prior = ctx.overlays.get(region, city) or {}
        report["prior_overlay_dbscan"] = dict(prior.get("dbscan") or {})
        # Merge so threshold/blend overlays are not wiped by a dbscan-only write.
        merged = deep_merge(prior, overlay)
        save_overlay(
            ctx.overlays,
            ctx.guardrails,
            region_code=region,
            city_code=city,
            overlay=merged,
        )
    except GuardrailError as exc:
        report.update({"decision": "rejected", "reason": f"guardrail:{exc}"})
        ctx.audit.append(
            actor="dbscan_retuner",
            action="reject",
            region_code=region,
            city_code=city,
            decision="rejected",
            reason=str(exc),
            after=overlay,
        )
        if ctx.run_store is not None:
            ctx.run_store.record(region_code=region, city_code=city, report=report)
        return report

    report.update({"decision": "applied", "reason": "holdout_pattern_recall_lift"})
    ctx.audit.append(
        actor="dbscan_retuner",
        action="apply",
        region_code=region,
        city_code=city,
        decision="accepted",
        reason="holdout_pattern_recall_lift",
        before={"dbscan": report["prior_overlay_dbscan"]},
        after=overlay,
        metrics_before=current_hold,
        metrics_after=best["holdout_metrics"],
    )
    if ctx.run_store is not None:
        ctx.run_store.record(region_code=region, city_code=city, report=report)
    _LOG.info(
        "dbscan retune applied region=%s city=%s eps=%s min_pts=%s",
        region,
        city,
        overlay["dbscan"]["clustering_radius_m"],
        overlay["dbscan"]["min_pts"],
    )
    return report
