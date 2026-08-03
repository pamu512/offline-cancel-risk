"""SQLite store for entity behavior baselines."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.baselines.gate import (
    above_consistent,
    candidate_baseline,
    prune_window,
    under_consistent,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EntityBaselineStore:
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
                CREATE TABLE IF NOT EXISTS entity_baselines (
                  entity_key TEXT NOT NULL,
                  entity_kind TEXT NOT NULL,
                  driver_id INTEGER,
                  user_id INTEGER,
                  head TEXT NOT NULL,
                  samples INTEGER NOT NULL,
                  ewma REAL NOT NULL,
                  baseline REAL,
                  armed_thr REAL,
                  under_consistent INTEGER NOT NULL,
                  above_consistent INTEGER NOT NULL,
                  discount_active INTEGER NOT NULL,
                  window_json TEXT NOT NULL,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (entity_key, head)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entity_baselines_updated
                ON entity_baselines(updated_at, entity_key, head)
                """
            )
            conn.commit()

    def get(self, entity_key: str, head: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM entity_baselines WHERE entity_key=? AND head=?",
                (entity_key, head),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_entity(self, entity_key: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM entity_baselines
                WHERE entity_key=?
                ORDER BY head
                """,
                (entity_key,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def query(
        self,
        *,
        entity_kind: str = "",
        driver_id: int | None = None,
        user_id: int | None = None,
        head: str = "",
        discount_active: bool | None = None,
        updated_since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entity_baselines WHERE 1=1"
        params: list[Any] = []
        if entity_kind:
            sql += " AND entity_kind=?"
            params.append(entity_kind.strip().lower())
        if driver_id is not None:
            sql += " AND driver_id=?"
            params.append(int(driver_id))
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(int(user_id))
        if head:
            sql += " AND head=?"
            params.append(head)
        if discount_active is not None:
            sql += " AND discount_active=?"
            params.append(1 if discount_active else 0)
        if updated_since:
            sql += " AND updated_at > ?"
            params.append(updated_since)
        sql += " ORDER BY updated_at ASC, entity_key ASC, head ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_observation(
        self,
        *,
        entity_key: str,
        entity_kind: str,
        driver_id: int | None,
        user_id: int | None,
        head: str,
        score: float,
        live_thr: float,
        cfg: dict[str, Any],
        max_len: int,
        region_code: str = "",
        city_code: str = "",
        observed_at: str,
        now: datetime,
    ) -> dict[str, Any]:
        prev = self.get(entity_key, head)
        window = list((prev or {}).get("window") or [])
        window.append({"s": float(score), "t": observed_at})
        window = prune_window(
            window,
            max_age_days=int(cfg["max_age_days"]),
            max_len=max_len,
            now=now,
        )
        alpha = float(cfg["ewma_alpha"])
        if prev is None or int(prev.get("samples") or 0) == 0:
            ewma = float(score)
            samples = 1
        else:
            ewma = alpha * float(score) + (1.0 - alpha) * float(prev["ewma"])
            samples = int(prev["samples"]) + 1

        baseline = prev.get("baseline") if prev else None
        armed_thr = prev.get("armed_thr") if prev else None
        # Consistency vs armed_thr once baseline exists; else live thr.
        thr_for_under = float(armed_thr) if baseline is not None and armed_thr is not None else float(live_thr)
        is_under = under_consistent(
            window,
            ewma,
            samples,
            thr=thr_for_under,
            cfg=cfg,
            max_len=max_len,
        )
        if is_under:
            cand = candidate_baseline(window, thr_for_under, ewma)
            # Arm once; only refresh if candidate stays near/below baseline
            # (elevated-but-under-thr windows must not raise the floor).
            if baseline is None:
                baseline = cand
                armed_thr = float(live_thr)
            elif cand <= float(baseline) + float(cfg["refresh_epsilon"]):
                if abs(cand - float(baseline)) > float(cfg["refresh_epsilon"]):
                    baseline = cand
                    armed_thr = float(live_thr)

        is_above = False
        if baseline is not None:
            is_above = above_consistent(
                window,
                ewma,
                samples,
                baseline=float(baseline),
                cfg=cfg,
                max_len=max_len,
            )
        discount_active = bool(is_above)

        row = {
            "entity_key": entity_key,
            "entity_kind": entity_kind,
            "driver_id": driver_id,
            "user_id": user_id,
            "head": head,
            "samples": samples,
            "ewma": ewma,
            "baseline": baseline,
            "armed_thr": armed_thr,
            "under_consistent": bool(is_under),
            "above_consistent": bool(is_above),
            "discount_active": discount_active,
            "window": window,
            "region_code": (region_code or "").strip().upper(),
            "city_code": (city_code or "").strip().upper(),
            "updated_at": _utc_now_iso(),
        }
        self._upsert(row)
        return row

    def _upsert(self, row: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO entity_baselines(
                  entity_key, entity_kind, driver_id, user_id, head,
                  samples, ewma, baseline, armed_thr,
                  under_consistent, above_consistent, discount_active,
                  window_json, region_code, city_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_key, head) DO UPDATE SET
                  entity_kind=excluded.entity_kind,
                  driver_id=excluded.driver_id,
                  user_id=excluded.user_id,
                  samples=excluded.samples,
                  ewma=excluded.ewma,
                  baseline=excluded.baseline,
                  armed_thr=excluded.armed_thr,
                  under_consistent=excluded.under_consistent,
                  above_consistent=excluded.above_consistent,
                  discount_active=excluded.discount_active,
                  window_json=excluded.window_json,
                  region_code=excluded.region_code,
                  city_code=excluded.city_code,
                  updated_at=excluded.updated_at
                """,
                (
                    row["entity_key"],
                    row["entity_kind"],
                    row["driver_id"],
                    row["user_id"],
                    row["head"],
                    int(row["samples"]),
                    float(row["ewma"]),
                    row["baseline"],
                    row["armed_thr"],
                    1 if row["under_consistent"] else 0,
                    1 if row["above_consistent"] else 0,
                    1 if row["discount_active"] else 0,
                    json.dumps(row["window"]),
                    row["region_code"],
                    row["city_code"],
                    row["updated_at"],
                ),
            )
            conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["window"] = json.loads(d.pop("window_json") or "[]")
        d["under_consistent"] = bool(d["under_consistent"])
        d["above_consistent"] = bool(d["above_consistent"])
        d["discount_active"] = bool(d["discount_active"])
        return d
