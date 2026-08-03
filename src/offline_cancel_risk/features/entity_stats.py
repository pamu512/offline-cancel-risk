"""Rolling cancel counts and marketplace funnel events (accept/cancel/complete)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.features.marketplace_math import evaluate_marketplace_signals


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_market_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entity_key TEXT NOT NULL,
                  entity_kind TEXT NOT NULL,
                  driver_id INTEGER,
                  user_id INTEGER,
                  order_display_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  cancel_with_cause INTEGER,
                  cancel_reason_code TEXT,
                  event_ts TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,
                  UNIQUE(entity_key, order_display_id, event_type)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entity_market_key_ts
                ON entity_market_events(entity_key, event_ts)
                """
            )
            conn.commit()

    def _entity_rows(
        self, driver_id: int, user_id: int | None
    ) -> list[tuple[str, str, int | None, int | None]]:
        rows: list[tuple[str, str, int | None, int | None]] = [
            (f"driver:{int(driver_id)}", "driver", int(driver_id), None),
        ]
        if user_id is not None:
            uid = int(user_id)
            rows.append((f"user:{uid}", "user", None, uid))
            rows.append(
                (f"pair:{int(driver_id)}:{uid}", "pair", int(driver_id), uid)
            )
        return rows

    def record_cancel(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        order_display_id: str,
        event_ts: str,
        cancel_with_cause: bool | None = None,
        cancel_reason_code: str | None = None,
    ) -> None:
        recorded = _utc_now().isoformat().replace("+00:00", "Z")
        cause_i = (
            None
            if cancel_with_cause is None
            else (1 if cancel_with_cause else 0)
        )
        with self._connect() as conn:
            for key, kind, did, uid in self._entity_rows(driver_id, user_id):
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
                conn.execute(
                    """
                    INSERT INTO entity_market_events(
                      entity_key, entity_kind, driver_id, user_id,
                      order_display_id, event_type, cancel_with_cause,
                      cancel_reason_code, event_ts, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, 'cancel', ?, ?, ?, ?)
                    ON CONFLICT(entity_key, order_display_id, event_type) DO UPDATE SET
                      cancel_with_cause=excluded.cancel_with_cause,
                      cancel_reason_code=excluded.cancel_reason_code,
                      event_ts=excluded.event_ts,
                      recorded_at=excluded.recorded_at
                    """,
                    (
                        key,
                        kind,
                        did,
                        uid,
                        order_display_id,
                        cause_i,
                        cancel_reason_code,
                        event_ts,
                        recorded,
                    ),
                )
            conn.commit()

    def record_market_event(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        order_display_id: str,
        event_type: str,
        event_ts: str,
        cancel_with_cause: bool | None = None,
        cancel_reason_code: str | None = None,
    ) -> None:
        et = event_type.strip().lower()
        if et not in {"accept", "cancel", "complete"}:
            raise ValueError(f"invalid event_type: {event_type}")
        if et == "cancel":
            self.record_cancel(
                driver_id=driver_id,
                user_id=user_id,
                order_display_id=order_display_id,
                event_ts=event_ts,
                cancel_with_cause=cancel_with_cause,
                cancel_reason_code=cancel_reason_code,
            )
            return
        recorded = _utc_now().isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            for key, kind, did, uid in self._entity_rows(driver_id, user_id):
                conn.execute(
                    """
                    INSERT INTO entity_market_events(
                      entity_key, entity_kind, driver_id, user_id,
                      order_display_id, event_type, cancel_with_cause,
                      cancel_reason_code, event_ts, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(entity_key, order_display_id, event_type) DO UPDATE SET
                      event_ts=excluded.event_ts,
                      recorded_at=excluded.recorded_at
                    """,
                    (
                        key,
                        kind,
                        did,
                        uid,
                        order_display_id,
                        et,
                        event_ts,
                        recorded,
                    ),
                )
            conn.commit()

    def record_assess_funnel(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        order_display_id: str,
        accept_ts: str,
        cancel_ts: str,
        cancel_with_cause: bool | None,
        cancel_reason_code: str | None,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.record_market_event(
            driver_id=driver_id,
            user_id=user_id,
            order_display_id=order_display_id,
            event_type="accept",
            event_ts=accept_ts,
        )
        self.record_cancel(
            driver_id=driver_id,
            user_id=user_id,
            order_display_id=order_display_id,
            event_ts=cancel_ts,
            cancel_with_cause=cancel_with_cause,
            cancel_reason_code=cancel_reason_code,
        )
        for ev in extra_events or []:
            self.record_market_event(
                driver_id=int(ev.get("driver_id", driver_id)),
                user_id=ev.get("user_id", user_id),
                order_display_id=str(ev["order_display_id"]),
                event_type=str(ev["event_type"]),
                event_ts=str(ev["event_ts"]),
                cancel_with_cause=ev.get("cancel_with_cause"),
                cancel_reason_code=ev.get("cancel_reason_code"),
            )

    def stats(
        self,
        *,
        driver_id: int,
        user_id: int | None,
        as_of: str,
        window_minutes: int,
        exclude_order_id: str = "",
        abuse_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        as_dt = _parse(as_of)
        start = (as_dt - timedelta(minutes=int(window_minutes))).isoformat().replace(
            "+00:00", "Z"
        )
        driver_key = f"driver:{int(driver_id)}"
        driver_n = self._count_cancels(driver_key, start, as_of, exclude_order_id)
        hours = max(window_minutes / 60.0, 1e-6)
        cancel_rate = driver_n / max(hours, 1.0)
        pair_n = 0
        if user_id is not None:
            pair_n = self._count_cancels(
                f"pair:{int(driver_id)}:{int(user_id)}",
                start,
                as_of,
                exclude_order_id,
            )
        funnel = self._funnel_counts(driver_key, start, as_of, exclude_order_id)
        market = evaluate_marketplace_signals(
            accepts=funnel["accepts"],
            cancels=funnel["cancels"],
            completes=funnel["completes"],
            with_cause_cancels=funnel["with_cause_cancels"],
            policy=abuse_policy or {},
        )
        return {
            "driver_cancel_count": driver_n,
            "driver_cancel_rate": float(cancel_rate),
            "pair_cancel_count": pair_n,
            **market,
        }

    def _funnel_counts(
        self, entity_key: str, start: str, end: str, exclude_order_id: str
    ) -> dict[str, int]:
        sql = """
            SELECT event_type, cancel_with_cause, COUNT(*) AS n
            FROM entity_market_events
            WHERE entity_key=? AND event_ts >= ? AND event_ts <= ?
        """
        params: list[Any] = [entity_key, start, end]
        if exclude_order_id:
            sql += " AND order_display_id <> ?"
            params.append(exclude_order_id)
        sql += " GROUP BY event_type, cancel_with_cause"
        accepts = cancels = completes = with_cause = 0
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            et = str(row["event_type"])
            n = int(row["n"])
            if et == "accept":
                accepts += n
            elif et == "complete":
                completes += n
            elif et == "cancel":
                cancels += n
                if row["cancel_with_cause"] == 1:
                    with_cause += n
        return {
            "accepts": accepts,
            "cancels": cancels,
            "completes": completes,
            "with_cause_cancels": with_cause,
        }

    def _count_cancels(
        self, entity_key: str, start: str, end: str, exclude_order_id: str
    ) -> int:
        sql = """
            SELECT COUNT(*) AS n FROM entity_cancel_events
            WHERE entity_key=? AND event_ts >= ? AND event_ts <= ?
        """
        params: list[Any] = [entity_key, start, end]
        if exclude_order_id:
            sql += " AND order_display_id <> ?"
            params.append(exclude_order_id)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)
