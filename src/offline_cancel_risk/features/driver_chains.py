"""Persist recent cancel/reassign events by driver for cross-order abuse chains."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.timeutil import parse_ts


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DriverChainStore:
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
                CREATE TABLE IF NOT EXISTS driver_cancel_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  driver_id INTEGER NOT NULL,
                  order_display_id TEXT NOT NULL,
                  event_ts TEXT NOT NULL,
                  recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_driver_events_driver_ts
                ON driver_cancel_events(driver_id, event_ts)
                """
            )
            conn.commit()

    def record(
        self,
        *,
        driver_id: int,
        order_display_id: str,
        event_ts: str,
    ) -> None:
        recorded = _utc_now().isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO driver_cancel_events(
                  driver_id, order_display_id, event_ts, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (int(driver_id), order_display_id, event_ts, recorded),
            )
            conn.commit()

    def count_recent(
        self,
        driver_id: int,
        *,
        as_of: str,
        window_minutes: int,
        exclude_order_id: str | None = None,
    ) -> int:
        end = parse_ts(as_of)
        start = end - timedelta(minutes=int(window_minutes))
        start_s = start.isoformat().replace("+00:00", "Z")
        end_s = end.isoformat().replace("+00:00", "Z")
        # Store event_ts may be "YYYY-MM-DD HH:MM:SS" — compare via parsed bounds
        # by loading rows in window using string compare when ISO-ish, else scan.
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT order_display_id, event_ts FROM driver_cancel_events
                WHERE driver_id=?
                """,
                (int(driver_id),),
            ).fetchall()
        orders: set[str] = set()
        for row in rows:
            oid = str(row["order_display_id"])
            if exclude_order_id and oid == exclude_order_id:
                continue
            try:
                ts = parse_ts(str(row["event_ts"]))
            except Exception:
                continue
            if start <= ts <= end:
                orders.add(oid)
        del start_s, end_s  # kept for clarity of window intent
        return len(orders)

    def record_from_assess(
        self,
        *,
        driver_id: int,
        order_display_id: str,
        cancel_ts: str,
        reassign_cancel_events: list[dict[str, Any]],
    ) -> None:
        self.record(
            driver_id=driver_id,
            order_display_id=order_display_id,
            event_ts=cancel_ts,
        )
        for event in reassign_cancel_events:
            did = event.get("driver_id", driver_id)
            ts = event.get("ts") or event.get("cancel_ts") or cancel_ts
            self.record(
                driver_id=int(did),
                order_display_id=order_display_id,
                event_ts=str(ts),
            )
