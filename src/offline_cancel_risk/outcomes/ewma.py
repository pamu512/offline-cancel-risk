"""Pure EWMA helpers for outcome-driven recoverability."""

from __future__ import annotations

OUTCOME_TYPES = frozenset(
    {"payout_blocked", "clawback_won", "clawback_lost", "account_actioned"}
)

_POSITIVE_OUTCOMES = frozenset({"clawback_won", "payout_blocked", "account_actioned"})


def signal_for_outcome(outcome: str) -> float:
    if outcome not in OUTCOME_TYPES:
        raise ValueError(f"unknown outcome: {outcome}")
    if outcome in _POSITIVE_OUTCOMES:
        return 1.0
    return 0.0


def ewma_update(prev: float, signal: float, alpha: float) -> float:
    return (1.0 - alpha) * prev + alpha * signal


def clamp_recoverability(value: float, head: str, guardrails: dict) -> float:
    key = f"ear.recoverability.{head}"
    bounds = guardrails.get(key) or {}
    lo = float(bounds.get("min", 0.0))
    hi = float(bounds.get("max", 1.0))
    return max(lo, min(hi, value))
