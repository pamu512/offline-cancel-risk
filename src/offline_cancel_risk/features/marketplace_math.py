"""Marketplace funnel metrics — accept→cancel, completion, with-cause.

Math notes:
- ACR = C/max(A,1) — prefer ratio to accepts over cancels/hour (volume confound).
- CR  = K/max(K+C,1) — completion among terminal outcomes.
- WCF = W/max(C,1) — with-cause share of cancels.
- Support gates required before flagging (small-sample protection).
"""

from __future__ import annotations

from typing import Any


def accept_cancel_rate(accepts: int, cancels: int) -> float:
    a = max(int(accepts), 0)
    c = max(int(cancels), 0)
    return float(c) / float(max(a, 1))


def completion_rate(completes: int, cancels: int) -> float:
    k = max(int(completes), 0)
    c = max(int(cancels), 0)
    return float(k) / float(max(k + c, 1))


def with_cause_fraction(with_cause_cancels: int, cancels: int) -> float:
    c = max(int(cancels), 0)
    w = max(0, min(int(with_cause_cancels), c))
    if c == 0:
        return 1.0  # undefined → treat as benign
    return float(w) / float(c)


def evaluate_marketplace_signals(
    *,
    accepts: int,
    cancels: int,
    completes: int,
    with_cause_cancels: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Return metrics + which abuse signals fire under support gates."""
    acr = accept_cancel_rate(accepts, cancels)
    cr = completion_rate(completes, cancels)
    wcf = with_cause_fraction(with_cause_cancels, cancels)
    n_min = int(policy.get("marketplace_min_support", 5))
    tau_acr = float(policy.get("accept_cancel_rate_threshold", 0.45))
    tau_cr = float(policy.get("completion_rate_floor", 0.40))
    tau_wcf = float(policy.get("with_cause_fraction_floor", 0.35))

    a, c, k = int(accepts), int(cancels), int(completes)
    terminal = k + c
    signals: list[str] = []
    if a >= n_min and acr >= tau_acr:
        signals.append("high_accept_cancel_rate")
    if terminal >= n_min and cr <= tau_cr:
        signals.append("low_completion_rate")
    if c >= n_min and wcf <= tau_wcf and acr >= tau_acr * 0.8:
        # Without-cause heavy only when cancel rate also elevated
        signals.append("cancel_without_cause_heavy")

    return {
        "accepts": a,
        "cancels": c,
        "completes": k,
        "with_cause_cancels": int(with_cause_cancels),
        "accept_cancel_rate": acr,
        "completion_rate": cr,
        "with_cause_fraction": wcf,
        "support_accepts": a >= n_min,
        "support_terminal": terminal >= n_min,
        "signals": signals,
    }
