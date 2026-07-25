"""Canary lifecycle: start, cohort check, promote, rollback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from offline_cancel_risk.models.gates import PromotionStatus, evaluate_promotion
from offline_cancel_risk.models.metrics import ShadowMetricsStore
from offline_cancel_risk.models.registry import ModelRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def in_canary_cohort(order_display_id: str, canary_pct: int) -> bool:
    digest = hashlib.sha256(order_display_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < int(canary_pct)


@dataclass
class CanaryState:
    challenger_model_id: str
    started_at: str
    canary_pct: int
    canary_hours: float
    status: str  # active|promoted|rolled_back|aborted


class CanaryController:
    def __init__(
        self,
        sqlite_path: Path | str,
        registry: ModelRegistry,
        metrics: ShadowMetricsStore,
        gates: dict[str, Any],
        thresholds: dict[str, float],
    ) -> None:
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.metrics = metrics
        self.gates = gates
        self.thresholds = thresholds
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS canary_state (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  challenger_model_id TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  canary_pct INTEGER NOT NULL,
                  canary_hours REAL NOT NULL,
                  status TEXT NOT NULL,
                  last_status_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promotion_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  challenger_model_id TEXT NOT NULL,
                  champion_model_id TEXT NOT NULL,
                  promotion_ready INTEGER NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def active(self) -> CanaryState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM canary_state WHERE id=1 AND status='active'"
            ).fetchone()
        if row is None:
            return None
        return CanaryState(
            challenger_model_id=row["challenger_model_id"],
            started_at=row["started_at"],
            canary_pct=int(row["canary_pct"]),
            canary_hours=float(row["canary_hours"]),
            status=row["status"],
        )

    def record_promotion_status(self, status: PromotionStatus) -> None:
        created = _utc_now().isoformat().replace("+00:00", "Z")
        payload = {
            "challenger_model_id": status.challenger_model_id,
            "champion_model_id": status.champion_model_id,
            "promotion_ready": status.promotion_ready,
            "promotion_blockers": status.promotion_blockers,
            "metrics": status.metrics,
            "recommended_action": status.recommended_action,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO promotion_events(
                  challenger_model_id, champion_model_id, promotion_ready,
                  payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    status.challenger_model_id,
                    status.champion_model_id,
                    status.promotion_ready,
                    json.dumps(payload),
                    created,
                ),
            )
            conn.commit()

    def latest_promotion_status(self, challenger_model_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM promotion_events
                WHERE challenger_model_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (challenger_model_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def evaluate_and_maybe_start_canary(self, challenger_model_id: str) -> PromotionStatus:
        champ = self.registry.get_champion()
        champ_id = champ.model_id if champ else "none"
        status = evaluate_promotion(
            challenger_model_id=challenger_model_id,
            champion_model_id=champ_id,
            store=self.metrics,
            thresholds=self.thresholds,
            gates=self.gates,
        )
        self.record_promotion_status(status)
        if (
            status.promotion_ready == 1
            and bool(self.gates.get("auto_canary", True))
            and self.active() is None
        ):
            self.start_canary(challenger_model_id)
        return status

    def start_canary(self, challenger_model_id: str) -> CanaryState:
        self.registry.set_role(challenger_model_id, "canary")
        started = _utc_now().isoformat().replace("+00:00", "Z")
        state = CanaryState(
            challenger_model_id=challenger_model_id,
            started_at=started,
            canary_pct=int(self.gates.get("canary_pct", 5)),
            canary_hours=float(self.gates.get("canary_hours", 24)),
            status="active",
        )
        with self._connect() as conn:
            conn.execute("DELETE FROM canary_state WHERE id=1")
            conn.execute(
                """
                INSERT INTO canary_state(
                  id, challenger_model_id, started_at, canary_pct, canary_hours, status
                ) VALUES (1, ?, ?, ?, ?, 'active')
                """,
                (
                    state.challenger_model_id,
                    state.started_at,
                    state.canary_pct,
                    state.canary_hours,
                ),
            )
            conn.commit()
        return state

    def abort(self) -> None:
        active = self.active()
        if active is None:
            return
        self.registry.set_role(active.challenger_model_id, "failed_canary")
        with self._connect() as conn:
            conn.execute(
                "UPDATE canary_state SET status='aborted' WHERE id=1"
            )
            conn.commit()

    def tick(self) -> str:
        """Re-check gates; promote or rollback when canary window ends."""
        active = self.active()
        if active is None:
            return "idle"
        champ = self.registry.get_champion()
        champ_id = champ.model_id if champ else "none"
        # During canary, challenger may have role=canary; metrics still keyed by id
        status = evaluate_promotion(
            challenger_model_id=active.challenger_model_id,
            champion_model_id=champ_id,
            store=self.metrics,
            thresholds=self.thresholds,
            gates=self.gates,
        )
        self.record_promotion_status(status)
        if status.promotion_ready == 0:
            self.registry.set_role(active.challenger_model_id, "failed_canary")
            with self._connect() as conn:
                conn.execute(
                    "UPDATE canary_state SET status='rolled_back' WHERE id=1"
                )
                conn.commit()
            return "rolled_back"

        elapsed = _utc_now() - _parse_iso(active.started_at)
        if elapsed >= timedelta(hours=active.canary_hours):
            # Full promote
            if champ is not None:
                self.registry.set_role(champ.model_id, "retired")
            self.registry.set_role(active.challenger_model_id, "champion")
            with self._connect() as conn:
                conn.execute(
                    "UPDATE canary_state SET status='promoted' WHERE id=1"
                )
                conn.commit()
            return "promoted"
        return "active"
