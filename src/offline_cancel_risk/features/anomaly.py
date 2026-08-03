"""Light entity anomaly watch: MAD z-score vs self + peer cohort."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


_DEFAULT_FEATURES = ("accept_cancel_rate", "cancel_rate", "cancel_abuse")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cohort_key(*, city_code: str | None, region_code: str | None) -> str:
    city = (city_code or "").strip()
    if city:
        return f"city:{city}"
    region = (region_code or "").strip()
    if region:
        return f"region:{region}"
    return "cohort:global"


def mad_zscore(x: float, sample: list[float], *, epsilon: float = 1e-3) -> float | None:
    """Robust z using MAD; None if empty sample."""
    if not sample:
        return None
    med = float(median(sample))
    mad = float(median([abs(float(v) - med) for v in sample]))
    scale = max(mad, float(epsilon))
    return 0.6745 * (float(x) - med) / scale


class EntityAnomalyStore:
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
                CREATE TABLE IF NOT EXISTS entity_feature_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entity_key TEXT NOT NULL,
                  feature TEXT NOT NULL,
                  value REAL NOT NULL,
                  cohort_key TEXT NOT NULL,
                  order_display_id TEXT,
                  event_ts TEXT NOT NULL,
                  recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_anom_entity_feat
                ON entity_feature_events(entity_key, feature, id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_anom_cohort_feat
                ON entity_feature_events(cohort_key, feature, id)
                """
            )
            conn.commit()

    def record(
        self,
        *,
        entity_key: str,
        feature: str,
        value: float,
        cohort_key: str,
        order_display_id: str | None,
        event_ts: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO entity_feature_events(
                  entity_key, feature, value, cohort_key,
                  order_display_id, event_ts, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_key,
                    feature,
                    float(value),
                    cohort_key,
                    order_display_id,
                    event_ts,
                    _utc_now_iso(),
                ),
            )
            conn.commit()

    def recent_entity_values(
        self,
        entity_key: str,
        feature: str,
        *,
        limit: int,
        exclude_order_id: str | None = None,
    ) -> list[float]:
        sql = """
            SELECT value, order_display_id FROM entity_feature_events
            WHERE entity_key=? AND feature=?
            ORDER BY id DESC LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql, (entity_key, feature, max(1, int(limit) * 2))
            ).fetchall()
        out: list[float] = []
        for row in rows:
            if exclude_order_id and row["order_display_id"] == exclude_order_id:
                continue
            out.append(float(row["value"]))
            if len(out) >= int(limit):
                break
        return out

    def recent_peer_values(
        self,
        cohort_key: str,
        feature: str,
        *,
        limit: int,
        exclude_entity_key: str,
        exclude_order_id: str | None = None,
    ) -> list[float]:
        sql = """
            SELECT value, entity_key, order_display_id FROM entity_feature_events
            WHERE cohort_key=? AND feature=?
            ORDER BY id DESC LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql, (cohort_key, feature, max(1, int(limit) * 3))
            ).fetchall()
        out: list[float] = []
        for row in rows:
            if row["entity_key"] == exclude_entity_key:
                continue
            if exclude_order_id and row["order_display_id"] == exclude_order_id:
                continue
            out.append(float(row["value"]))
            if len(out) >= int(limit):
                break
        return out


def evaluate_entity_anomaly(
    *,
    store: EntityAnomalyStore,
    entity_key: str,
    cohort: str,
    features: dict[str, float],
    order_display_id: str,
    event_ts: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Record features then score self/peer MAD z. Does not mutate abuse itself."""
    cfg = dict(policy.get("anomaly") or {})
    mode = str(cfg.get("mode", "shadow")).strip().lower()
    if mode not in {"shadow", "apply", "off"}:
        mode = "shadow"
    if mode == "off":
        return {
            "mode": mode,
            "fires": False,
            "signals": [],
            "details": [],
            "abuse_bonus": 0.0,
            "reasons": [],
        }

    window_n = int(cfg.get("window_n", 20))
    peer_n = int(cfg.get("peer_window_n", 200))
    n_min = int(cfg.get("min_support", 8))
    tau = float(cfg.get("z_threshold", 3.0))
    eps = float(cfg.get("epsilon", 1e-3))
    bonus = float(cfg.get("abuse_bonus", 0.15))
    allowed = list(cfg.get("features") or _DEFAULT_FEATURES)

    details: list[dict[str, Any]] = []
    signals: list[str] = []

    for feat, raw in features.items():
        if feat not in allowed:
            continue
        if raw is None:
            continue
        x = float(raw)
        # Score against history *before* inserting current point.
        self_vals = store.recent_entity_values(
            entity_key,
            feat,
            limit=window_n,
            exclude_order_id=order_display_id,
        )
        peer_vals = store.recent_peer_values(
            cohort,
            feat,
            limit=peer_n,
            exclude_entity_key=entity_key,
            exclude_order_id=order_display_id,
        )
        z_self = mad_zscore(x, self_vals, epsilon=eps) if len(self_vals) >= n_min else None
        z_peer = mad_zscore(x, peer_vals, epsilon=eps) if len(peer_vals) >= n_min else None
        store.record(
            entity_key=entity_key,
            feature=feat,
            value=x,
            cohort_key=cohort,
            order_display_id=order_display_id,
            event_ts=event_ts,
        )
        row = {
            "feature": feat,
            "value": x,
            "z_self": z_self,
            "z_peer": z_peer,
            "n_self": len(self_vals),
            "n_peer": len(peer_vals),
        }
        details.append(row)
        if z_self is not None and z_self >= tau:
            signals.append("anomaly_self")
        if z_peer is not None and z_peer >= tau:
            signals.append("anomaly_peer")

    # Dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    fires = bool(uniq)
    reasons = list(uniq)
    abuse_bonus = bonus if (fires and mode == "apply") else 0.0
    if fires and mode == "shadow":
        reasons = [f"anomaly_shadow:{r}" for r in uniq]
    return {
        "mode": mode,
        "fires": fires,
        "signals": uniq,
        "details": details,
        "abuse_bonus": abuse_bonus,
        "reasons": reasons,
        "cohort_key": cohort,
        "entity_key": entity_key,
    }
