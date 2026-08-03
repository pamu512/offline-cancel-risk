"""Bipartite device ↔ driver/user graph for multi-account signals."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DeviceGraphStore:
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
                CREATE TABLE IF NOT EXISTS device_edges (
                  device_id TEXT NOT NULL,
                  entity_kind TEXT NOT NULL,
                  entity_id INTEGER NOT NULL,
                  sightings INTEGER NOT NULL,
                  first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL,
                  PRIMARY KEY (device_id, entity_kind, entity_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_edges_device_seen
                ON device_edges(device_id, last_seen)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_edges_entity_seen
                ON device_edges(entity_kind, entity_id, last_seen)
                """
            )
            conn.commit()

    def observe(
        self,
        *,
        device_id: str,
        driver_id: int | None,
        user_id: int | None,
        event_ts: str | None = None,
    ) -> None:
        did = (device_id or "").strip()
        if not did:
            return
        ts = _iso(_parse(event_ts) if event_ts else _utc_now())
        rows: list[tuple[str, int]] = []
        if driver_id is not None:
            rows.append(("driver", int(driver_id)))
        if user_id is not None:
            rows.append(("user", int(user_id)))
        if not rows:
            return
        with self._connect() as conn:
            for kind, eid in rows:
                prev = conn.execute(
                    """
                    SELECT sightings, first_seen FROM device_edges
                    WHERE device_id=? AND entity_kind=? AND entity_id=?
                    """,
                    (did, kind, eid),
                ).fetchone()
                if prev is None:
                    conn.execute(
                        """
                        INSERT INTO device_edges(
                          device_id, entity_kind, entity_id,
                          sightings, first_seen, last_seen
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (did, kind, eid, ts, ts),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE device_edges
                        SET sightings=?, last_seen=?
                        WHERE device_id=? AND entity_kind=? AND entity_id=?
                        """,
                        (int(prev["sightings"]) + 1, ts, did, kind, eid),
                    )
            conn.commit()

    def _cutoff(self, as_of: str | None, window_days: int) -> str:
        base = _parse(as_of) if as_of else _utc_now()
        return _iso(base - timedelta(days=int(window_days)))

    def counts(
        self,
        *,
        device_id: str | None,
        driver_id: int | None,
        user_id: int | None = None,
        as_of: str | None = None,
        window_days: int = 30,
    ) -> dict[str, Any]:
        cutoff = self._cutoff(as_of, window_days)
        drivers_on_device = 0
        users_on_device = 0
        device_sightings = 0
        devices_for_driver = 0
        driver_sightings = 0
        driver_edge_sightings = 0
        user_edge_sightings = 0
        did = (device_id or "").strip()
        with self._connect() as conn:
            if did:
                drivers_on_device = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM device_edges
                        WHERE device_id=? AND entity_kind='driver' AND last_seen>=?
                        """,
                        (did, cutoff),
                    ).fetchone()[0]
                )
                users_on_device = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM device_edges
                        WHERE device_id=? AND entity_kind='user' AND last_seen>=?
                        """,
                        (did, cutoff),
                    ).fetchone()[0]
                )
                device_sightings = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(sightings), 0) FROM device_edges
                        WHERE device_id=? AND last_seen>=?
                        """,
                        (did, cutoff),
                    ).fetchone()[0]
                )
                if driver_id is not None:
                    row = conn.execute(
                        """
                        SELECT sightings FROM device_edges
                        WHERE device_id=? AND entity_kind='driver' AND entity_id=?
                          AND last_seen>=?
                        """,
                        (did, int(driver_id), cutoff),
                    ).fetchone()
                    driver_edge_sightings = int(row["sightings"]) if row else 0
                if user_id is not None:
                    row = conn.execute(
                        """
                        SELECT sightings FROM device_edges
                        WHERE device_id=? AND entity_kind='user' AND entity_id=?
                          AND last_seen>=?
                        """,
                        (did, int(user_id), cutoff),
                    ).fetchone()
                    user_edge_sightings = int(row["sightings"]) if row else 0
            if driver_id is not None:
                devices_for_driver = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM device_edges
                        WHERE entity_kind='driver' AND entity_id=? AND last_seen>=?
                        """,
                        (int(driver_id), cutoff),
                    ).fetchone()[0]
                )
                driver_sightings = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(sightings), 0) FROM device_edges
                        WHERE entity_kind='driver' AND entity_id=? AND last_seen>=?
                        """,
                        (int(driver_id), cutoff),
                    ).fetchone()[0]
                )
        shared = (
            did != ""
            and driver_id is not None
            and user_id is not None
            and driver_edge_sightings > 0
            and user_edge_sightings > 0
        )
        return {
            "device_id": did or None,
            "drivers_on_device": drivers_on_device,
            "users_on_device": users_on_device,
            "devices_for_driver": devices_for_driver,
            "device_sightings": device_sightings,
            "driver_sightings": driver_sightings,
            "driver_edge_sightings": driver_edge_sightings,
            "user_edge_sightings": user_edge_sightings,
            "shared_device_pair": shared,
            "window_days": int(window_days),
        }

    def evaluate(
        self,
        *,
        device_id: str | None,
        driver_id: int | None,
        user_id: int | None,
        as_of: str | None,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        cfg = dict(policy.get("device_graph") or {})
        window_days = int(cfg.get("window_days", 30))
        n_min = int(cfg.get("min_support", 3))
        thr_drivers = int(cfg.get("max_drivers_per_device", 3))
        thr_users = int(cfg.get("max_users_per_device", 3))
        thr_devices = int(cfg.get("max_devices_per_driver", 4))
        shared_min = int(cfg.get("shared_pair_min_sightings", 1))

        c = self.counts(
            device_id=device_id,
            driver_id=driver_id,
            user_id=user_id,
            as_of=as_of,
            window_days=window_days,
        )
        signals: list[str] = []
        if c["device_sightings"] >= n_min and c["drivers_on_device"] >= thr_drivers:
            signals.append("multi_account_device")
        if c["device_sightings"] >= n_min and c["users_on_device"] >= thr_users:
            signals.append("multi_user_device")
        if c["driver_sightings"] >= n_min and c["devices_for_driver"] >= thr_devices:
            signals.append("device_hopping")
        if (
            c["shared_device_pair"]
            and c["driver_edge_sightings"] >= shared_min
            and c["user_edge_sightings"] >= shared_min
        ):
            signals.append("shared_device_pair")
        return {**c, "signals": signals}
