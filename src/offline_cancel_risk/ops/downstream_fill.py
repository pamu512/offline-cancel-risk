"""Fill-rate for Downstream optional intel (chat / device_risk)."""

from __future__ import annotations

from typing import Any

from offline_cancel_risk.api.schemas import AssessmentResult


def downstream_intel_fill_report(
    results: list[AssessmentResult],
) -> dict[str, Any]:
    """How often Downstream sent chat_signals / device_risk on assess."""
    n = len(results)
    chat_n = 0
    device_n = 0
    for r in results:
        gw = r.gps_window or {}
        if gw.get("downstream_chat_signals"):
            chat_n += 1
        if gw.get("downstream_device_risk"):
            device_n += 1
    return {
        "n": n,
        "chat_signals_present": chat_n,
        "device_risk_present": device_n,
        "chat_fill_rate": (chat_n / n) if n else 0.0,
        "device_fill_rate": (device_n / n) if n else 0.0,
        "contract": {
            "chat_signals": "optional AssessRequest.chat_signals or POST /v1/chat-signals",
            "device_risk": "optional AssessRequest.device_risk (spoof/root/risk_score)",
            "skip_behavior": "omitted → no chat/device bonus; scores still computed",
            "out_of_scope": "NLP, GPS SDK root checks, enforcement",
        },
    }
