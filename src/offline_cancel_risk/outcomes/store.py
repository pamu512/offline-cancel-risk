"""SQLite store for outcome events and recoverability EWMA state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.outcomes.ewma import (
    OUTCOME_TYPES,
    clamp_recoverability,
    ewma_update,
    signal_for_outcome,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OutcomeStore:
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
                CREATE TABLE IF NOT EXISTS outcomes (
                  order_display_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  head TEXT NOT NULL,
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  amount REAL,
                  occurred_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (order_display_id, outcome, occurred_at)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recoverability_ewma (
                  region_code TEXT NOT NULL,
                  city_code TEXT NOT NULL,
                  head TEXT NOT NULL,
                  value REAL NOT NULL,
                  n_updates INTEGER NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (region_code, city_code, head)
                )
                """
            )
            conn.commit()

    def record_outcome(
        self,
        *,
        order_display_id: str,
        outcome: str,
        head: str,
        region_code: str,
        city_code: str,
        amount: float | None = None,
        occurred_at: str | None = None,
        alpha: float,
        cold_start: dict[str, float],
        guardrails: dict,
    ) -> dict[str, Any]:
        oid = (order_display_id or "").strip()
        if not oid:
            raise ValueError("order_display_id required")
        if outcome not in OUTCOME_TYPES:
            raise ValueError(f"unknown outcome: {outcome}")
        if head not in cold_start:
            raise ValueError(f"unknown head: {head}")

        ts = occurred_at or _utc_now_iso()
        created_at = _utc_now_iso()

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO outcomes(
                  order_display_id, outcome, head, region_code, city_code,
                  amount, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (oid, outcome, head, region_code, city_code, amount, ts, created_at),
            )
            if cur.rowcount == 0:
                ewma_row = conn.execute(
                    """
                    SELECT value, n_updates, updated_at FROM recoverability_ewma
                    WHERE region_code=? AND city_code=? AND head=?
                    """,
                    (region_code, city_code, head),
                ).fetchone()
                recoverability = self._recoverability_map(conn, region_code, city_code)
                if ewma_row is None:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "order_display_id": oid,
                        "head": head,
                        "region_code": region_code,
                        "city_code": city_code,
                        "value": float(cold_start[head]),
                        "n_updates": 0,
                        "recoverability": recoverability,
                    }
                return {
                    "ok": True,
                    "duplicate": True,
                    "order_display_id": oid,
                    "head": head,
                    "region_code": region_code,
                    "city_code": city_code,
                    "value": float(ewma_row["value"]),
                    "n_updates": int(ewma_row["n_updates"]),
                    "recoverability": recoverability,
                }

            ewma_row = conn.execute(
                """
                SELECT value, n_updates FROM recoverability_ewma
                WHERE region_code=? AND city_code=? AND head=?
                """,
                (region_code, city_code, head),
            ).fetchone()

            if ewma_row is None:
                prev = float(cold_start[head])
                n_updates = 0
            else:
                prev = float(ewma_row["value"])
                n_updates = int(ewma_row["n_updates"])

            signal = signal_for_outcome(outcome)
            new_value = clamp_recoverability(
                ewma_update(prev, signal, alpha), head, guardrails
            )
            n_updates += 1
            updated_at = _utc_now_iso()

            conn.execute(
                """
                INSERT INTO recoverability_ewma(
                  region_code, city_code, head, value, n_updates, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(region_code, city_code, head) DO UPDATE SET
                  value=excluded.value,
                  n_updates=excluded.n_updates,
                  updated_at=excluded.updated_at
                """,
                (region_code, city_code, head, new_value, n_updates, updated_at),
            )
            conn.commit()

            recoverability = self._recoverability_map(conn, region_code, city_code)

        return {
            "ok": True,
            "order_display_id": oid,
            "head": head,
            "region_code": region_code,
            "city_code": city_code,
            "value": new_value,
            "n_updates": n_updates,
            "recoverability": recoverability,
        }

    def _recoverability_map(
        self, conn: sqlite3.Connection, region_code: str, city_code: str
    ) -> dict[str, float]:
        rows = conn.execute(
            """
            SELECT head, value FROM recoverability_ewma
            WHERE region_code=? AND city_code=?
            """,
            (region_code, city_code),
        ).fetchall()
        return {str(r["head"]): float(r["value"]) for r in rows}

    def get_recoverability(self, region: str, city: str) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT head, value, n_updates, updated_at FROM recoverability_ewma
                WHERE region_code=? AND city_code=?
                """,
                (region, city),
            ).fetchall()
        return {
            str(r["head"]): {
                "value": float(r["value"]),
                "n_updates": int(r["n_updates"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        }

    def list_outcomes(
        self, *, order_display_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        lim = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            if order_display_id:
                rows = conn.execute(
                    """
                    SELECT order_display_id, outcome, head, region_code, city_code,
                           amount, occurred_at, created_at
                    FROM outcomes
                    WHERE order_display_id=?
                    ORDER BY occurred_at DESC
                    LIMIT ?
                    """,
                    (order_display_id.strip(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT order_display_id, outcome, head, region_code, city_code,
                           amount, occurred_at, created_at
                    FROM outcomes
                    ORDER BY occurred_at DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
        return [dict(r) for r in rows]
