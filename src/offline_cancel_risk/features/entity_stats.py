"""Rolling cancel counts / rates and pair density for abuse features."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EntityCancelStatsStore:
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
                CREATE TABLE IF NOT EXISTS entity_cancel_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entity_key TEXT NOT NULL,
                  entity_kind TEXT NOT NULL,
                  driver_id INTEGER,
                  user_id INTEGER,
                  order_display_id TEXT NOT NULL,
                  event_ts TEXT NOT NULL,
                  recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entity_cancel_key_ts
                ON entity_cancel_events(entity_key, event_ts)
                """
            )
            conn.commit()

    def record_cancel(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        order_display_id: str,
        event_ts: str,
    ) -> None:
        recorded = _utc_now().isoformat().replace("+00:00", "Z")
        rows = [
            (f"driver:{int(driver_id)}", "driver", int(driver_id), None),
        ]
        if user_id is not None:
            uid = int(user_id)
            rows.append((f"user:{uid}", "user", None, uid))
            rows.append(
                (f"pair:{int(driver_id)}:{uid}", "pair", int(driver_id), uid)
            )
        with self._connect() as conn:
            for key, kind, did, uid in rows:
                conn.execute(
                    """
                    INSERT INTO entity_cancel_events(
                      entity_key, entity_kind, driver_id, user_id,
                      order_display_id, event_ts, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        kind,
                        did,
                        uid,
                        order_display_id,
                        event_ts,
                        recorded,
                    ),
                )
            conn.commit()

    def stats(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        as_of: str,
        window_minutes: int,
        exclude_order_id: str = "",
    ) -> dict[str, Any]:
        as_dt = _parse(as_of)
        start = (as_dt - timedelta(minutes=int(window_minutes))).isoformat().replace(
            "+00:00", "Z"
        )
        driver_key = f"driver:{int(driver_id)}"
        driver_n = self._count(driver_key, start, as_of, exclude_order_id)
        # Rate proxy: cancels in window / window hours (capped display 0..1 via thresholds in abuse)
        hours = max(window_minutes / 60.0, 1e-6)
        cancel_rate = driver_n / max(hours, 1.0)
        pair_n = 0
        if user_id is not None:
            pair_n = self._count(
                f"pair:{int(driver_id)}:{int(user_id)}",
                start,
                as_of,
                exclude_order_id,
            )
        return {
            "driver_cancel_count": driver_n,
            "driver_cancel_rate": float(cancel_rate),
            "pair_cancel_count": pair_n,
        }

    def _count(
        self, entity_key: str, start: str, end: str, exclude_order_id: str
    ) -> int:
        sql = """
            SELECT COUNT(*) AS n FROM entity_cancel_events
            WHERE entity_key=? AND event_ts > ? AND event_ts <= ?
        """
        params: list[Any] = [entity_key, start, end]
        if exclude_order_id:
            sql += " AND order_display_id <> ?"
            params.append(exclude_order_id)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
