"""Persist champion vs shadow score pairs for promote gates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ShadowMetricRow:
    order_display_id: str
    champion_model_id: str
    shadow_model_id: str
    champion_scores: dict[str, float]
    shadow_scores: dict[str, float]
    order_value: float
    labels: dict[str, int] | None
    recorded_at: str


class ShadowMetricsStore:
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
                CREATE TABLE IF NOT EXISTS shadow_metrics (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_display_id TEXT NOT NULL,
                  champion_model_id TEXT NOT NULL,
                  shadow_model_id TEXT NOT NULL,
                  champion_scores_json TEXT NOT NULL,
                  shadow_scores_json TEXT NOT NULL,
                  order_value REAL NOT NULL,
                  labels_json TEXT,
                  recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shadow_model
                ON shadow_metrics(shadow_model_id)
                """
            )
            conn.commit()

    def record(
        self,
        *,
        order_display_id: str,
        champion_model_id: str,
        shadow_model_id: str,
        champion_scores: dict[str, float],
        shadow_scores: dict[str, float],
        order_value: float,
        labels: dict[str, int] | None = None,
    ) -> None:
        recorded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO shadow_metrics(
                  order_display_id, champion_model_id, shadow_model_id,
                  champion_scores_json, shadow_scores_json, order_value,
                  labels_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_display_id,
                    champion_model_id,
                    shadow_model_id,
                    json.dumps(champion_scores),
                    json.dumps(shadow_scores),
                    float(order_value),
                    json.dumps(labels) if labels is not None else None,
                    recorded_at,
                ),
            )
            conn.commit()

    def count_for_shadow(self, shadow_model_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM shadow_metrics WHERE shadow_model_id=?",
                (shadow_model_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_for_shadow(self, shadow_model_id: str) -> list[ShadowMetricRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM shadow_metrics
                WHERE shadow_model_id=?
                ORDER BY id ASC
                """,
                (shadow_model_id,),
            ).fetchall()
        out: list[ShadowMetricRow] = []
        for r in rows:
            labels_raw = r["labels_json"]
            out.append(
                ShadowMetricRow(
                    order_display_id=r["order_display_id"],
                    champion_model_id=r["champion_model_id"],
                    shadow_model_id=r["shadow_model_id"],
                    champion_scores=json.loads(r["champion_scores_json"]),
                    shadow_scores=json.loads(r["shadow_scores_json"]),
                    order_value=float(r["order_value"]),
                    labels=json.loads(labels_raw) if labels_raw else None,
                    recorded_at=r["recorded_at"],
                )
            )
        return out

    def aggregate_fp_dollar_proxy(
        self, shadow_model_id: str, thresholds: dict[str, float]
    ) -> dict[str, Any]:
        """Approximate FP$ as sum(order_value) where shadow flags and champion does not."""
        rows = self.list_for_shadow(shadow_model_id)
        fp = 0.0
        catch = 0.0
        for row in rows:
            for head, thr in thresholds.items():
                s_flag = row.shadow_scores.get(head, 0.0) >= thr
                c_flag = row.champion_scores.get(head, 0.0) >= thr
                if s_flag and not c_flag:
                    fp += row.order_value
                if s_flag and c_flag:
                    catch += row.order_value
        return {
            "n": len(rows),
            "fp_dollar_proxy": fp,
            "catch_dollar_proxy": catch,
        }
