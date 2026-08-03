"""Force-cancel / chat persuasion signals from Downstream (no NLP here)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_FLAG_WEIGHTS = (
    ("rider_forced_cancel", 0.90),
    ("cash_offline_suggested", 0.85),
    ("persuasion_suspected", 0.70),
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_chat_signals(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(raw or {})
    out: dict[str, Any] = {
        "persuasion_suspected": _as_bool(raw.get("persuasion_suspected")),
        "cash_offline_suggested": _as_bool(
            raw.get("cash_offline_suggested") or raw.get("pay_cash_suggested")
        ),
        "rider_forced_cancel": _as_bool(
            raw.get("rider_forced_cancel") or raw.get("force_cancel")
        ),
        "signal_score": None,
        "source": raw.get("source"),
    }
    if raw.get("signal_score") is not None and raw.get("signal_score") != "":
        out["signal_score"] = max(0.0, min(1.0, float(raw["signal_score"])))
    return out


def merge_chat_signals(
    a: dict[str, Any] | None, b: dict[str, Any] | None
) -> dict[str, Any]:
    """OR bools, max vendor score."""
    na = normalize_chat_signals(a)
    nb = normalize_chat_signals(b)
    score_vals = [x for x in (na.get("signal_score"), nb.get("signal_score")) if x is not None]
    return {
        "persuasion_suspected": bool(
            na["persuasion_suspected"] or nb["persuasion_suspected"]
        ),
        "cash_offline_suggested": bool(
            na["cash_offline_suggested"] or nb["cash_offline_suggested"]
        ),
        "rider_forced_cancel": bool(
            na["rider_forced_cancel"] or nb["rider_forced_cancel"]
        ),
        "signal_score": max(score_vals) if score_vals else None,
        "source": na.get("source") or nb.get("source"),
    }


def instant_chat_risk(normalized: dict[str, Any]) -> float:
    flag_f = 0.0
    for key, weight in _FLAG_WEIGHTS:
        if normalized.get(key):
            flag_f = max(flag_f, float(weight))
    vendor = normalized.get("signal_score")
    v = float(vendor) if vendor is not None else 0.0
    return max(0.0, min(1.0, max(v, flag_f)))


def evaluate_chat_signals(
    raw: dict[str, Any] | None,
    *,
    driver_signal_count: int = 0,
    no_progress: bool = False,
    wrong_direction: bool = False,
    policy: dict[str, Any],
) -> dict[str, Any]:
    cfg = dict(policy.get("chat_signals") or {})
    thr = float(cfg.get("risk_threshold", 0.55))
    beta = float(cfg.get("abuse_bonus_scale", 0.4))
    stall_bonus = float(cfg.get("stall_combo_bonus", 0.15))
    repeat_min = int(cfg.get("repeat_min_count", 3))
    repeat_bonus = float(cfg.get("repeat_bonus", 0.2))

    norm = normalize_chat_signals(raw)
    risk = instant_chat_risk(norm)
    fires = risk >= thr
    reasons: list[str] = []
    abuse_bonus = 0.0
    if fires:
        reasons.append("chat_force_cancel")
        for key, _ in _FLAG_WEIGHTS:
            if norm.get(key):
                reasons.append(f"chat_{key}")
        abuse_bonus = beta * risk
        if no_progress or wrong_direction:
            abuse_bonus += stall_bonus
            reasons.append("force_cancel_with_stall")
    if int(driver_signal_count) >= repeat_min:
        abuse_bonus += repeat_bonus
        reasons.append("repeat_force_cancel")
    return {
        "normalized": norm,
        "risk": risk,
        "fires": fires,
        "abuse_bonus": abuse_bonus,
        "reasons": reasons,
        "driver_signal_count": int(driver_signal_count),
        "threshold": thr,
    }


class ChatSignalStore:
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
                CREATE TABLE IF NOT EXISTS chat_signals (
                  order_display_id TEXT PRIMARY KEY,
                  driver_id INTEGER,
                  user_id INTEGER,
                  flags_json TEXT NOT NULL,
                  risk REAL NOT NULL,
                  event_ts TEXT NOT NULL,
                  recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_signals_driver_ts
                ON chat_signals(driver_id, event_ts)
                """
            )
            conn.commit()

    def get(self, order_display_id: str) -> dict[str, Any] | None:
        oid = (order_display_id or "").strip()
        if not oid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_signals WHERE order_display_id=?", (oid,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["flags"] = json.loads(d.pop("flags_json") or "{}")
        return d

    def upsert(
        self,
        *,
        order_display_id: str,
        driver_id: int | None,
        user_id: int | None,
        flags: dict[str, Any],
        risk: float,
        event_ts: str | None = None,
    ) -> dict[str, Any]:
        oid = (order_display_id or "").strip()
        if not oid:
            raise ValueError("order_display_id required")
        prev = self.get(oid)
        merged = merge_chat_signals(prev.get("flags") if prev else None, flags)
        risk_m = max(float(risk), instant_chat_risk(merged))
        ts = event_ts or (prev["event_ts"] if prev else _iso(_utc_now()))
        row = {
            "order_display_id": oid,
            "driver_id": driver_id if driver_id is not None else (prev or {}).get("driver_id"),
            "user_id": user_id if user_id is not None else (prev or {}).get("user_id"),
            "flags": merged,
            "risk": risk_m,
            "event_ts": ts,
            "recorded_at": _iso(_utc_now()),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_signals(
                  order_display_id, driver_id, user_id, flags_json,
                  risk, event_ts, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_display_id) DO UPDATE SET
                  driver_id=COALESCE(excluded.driver_id, chat_signals.driver_id),
                  user_id=COALESCE(excluded.user_id, chat_signals.user_id),
                  flags_json=excluded.flags_json,
                  risk=excluded.risk,
                  event_ts=excluded.event_ts,
                  recorded_at=excluded.recorded_at
                """,
                (
                    row["order_display_id"],
                    row["driver_id"],
                    row["user_id"],
                    json.dumps(row["flags"]),
                    row["risk"],
                    row["event_ts"],
                    row["recorded_at"],
                ),
            )
            conn.commit()
        return row

    def driver_signal_count(
        self,
        driver_id: int,
        *,
        as_of: str | None = None,
        window_minutes: int = 10080,
        min_risk: float = 0.55,
    ) -> int:
        base = _parse(as_of) if as_of else _utc_now()
        cutoff = _iso(base - timedelta(minutes=int(window_minutes)))
        with self._connect() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) FROM chat_signals
                WHERE driver_id=? AND event_ts>=? AND risk>=?
                """,
                (int(driver_id), cutoff, float(min_risk)),
            ).fetchone()[0]
        return int(n)
