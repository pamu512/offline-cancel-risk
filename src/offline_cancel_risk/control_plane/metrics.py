"""Join labeled feedback to assessment scores → per-head P/R/F1."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from offline_cancel_risk.scoring.policy import apply_thresholds

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def compute_label_metrics(
    assessments: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    *,
    thresholds: dict[str, float],
    region_code: str = "",
    city_code: str = "",
) -> list[dict[str, Any]]:
    region = (region_code or "").strip().upper()
    city = (city_code or "").strip().upper()
    by_id = {a["order_display_id"]: a for a in assessments}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for fb in feedback:
        assess = by_id.get(fb["order_display_id"])
        if assess is None:
            continue
        if region:
            if (assess.get("region_code") or "").strip().upper() != region:
                continue
        if city:
            if (assess.get("city_code") or "").strip().upper() != city:
                continue
        pairs.append((assess, fb))

    policy = {"thresholds": thresholds}
    out: list[dict[str, Any]] = []
    for head in _HEADS:
        tp = fp = fn = tn = 0
        labeled = 0
        for assess, fb in pairs:
            labels = fb.get("labels") or {}
            if head not in labels:
                continue
            y = int(labels[head])
            scores = {
                h: float((assess.get("scores") or {}).get(h, 0.0)) for h in _HEADS
            }
            flags = apply_thresholds(scores, policy)
            yhat = int(flags.get(head, 0))
            labeled += 1
            if y == 1 and yhat == 1:
                tp += 1
            elif y == 0 and yhat == 1:
                fp += 1
            elif y == 1 and yhat == 0:
                fn += 1
            else:
                tn += 1
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        out.append(
            {
                "region_code": region,
                "city_code": city,
                "head": head,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "support": labeled,
                "flag_rate": _safe_div(tp + fp, labeled),
            }
        )
    return out


class LabelMetricsStore:
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
                CREATE TABLE IF NOT EXISTS label_metrics (
                  snapshot_id TEXT PRIMARY KEY,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  head TEXT NOT NULL,
                  precision REAL NOT NULL,
                  recall REAL NOT NULL,
                  f1 REAL NOT NULL,
                  support INTEGER NOT NULL,
                  tp INTEGER NOT NULL,
                  fp INTEGER NOT NULL,
                  fn INTEGER NOT NULL,
                  tn INTEGER NOT NULL,
                  flag_rate REAL NOT NULL,
                  payload_json TEXT NOT NULL,
                  computed_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save_snapshots(self, rows: list[dict[str, Any]]) -> list[str]:
        computed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ids: list[str] = []
        with self._connect() as conn:
            for row in rows:
                sid = uuid4().hex
                ids.append(sid)
                conn.execute(
                    """
                    INSERT INTO label_metrics(
                      snapshot_id, region_code, city_code, head,
                      precision, recall, f1, support, tp, fp, fn, tn,
                      flag_rate, payload_json, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        row.get("region_code", ""),
                        row.get("city_code", ""),
                        row["head"],
                        float(row["precision"]),
                        float(row["recall"]),
                        float(row["f1"]),
                        int(row["support"]),
                        int(row["tp"]),
                        int(row["fp"]),
                        int(row["fn"]),
                        int(row["tn"]),
                        float(row.get("flag_rate", 0.0)),
                        json.dumps(row),
                        computed,
                    ),
                )
            conn.commit()
        return ids

    def latest(
        self, *, region_code: str = "", city_code: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        region = (region_code or "").strip().upper()
        city = (city_code or "").strip().upper()
        sql = "SELECT payload_json, computed_at FROM label_metrics WHERE 1=1"
        params: list[Any] = []
        if region:
            sql += " AND region_code=?"
            params.append(region)
        if city:
            sql += " AND city_code=?"
            params.append(city)
        sql += " ORDER BY computed_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item["computed_at"] = row["computed_at"]
            out.append(item)
        return out
