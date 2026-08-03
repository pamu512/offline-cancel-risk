from pathlib import Path

from offline_cancel_risk.features.abuse import abuse_feature_score
from offline_cancel_risk.features.chat_signals import (
    ChatSignalStore,
    evaluate_chat_signals,
    instant_chat_risk,
    merge_chat_signals,
    normalize_chat_signals,
)


def _policy() -> dict:
    return {
        "chat_signals": {
            "risk_threshold": 0.55,
            "abuse_bonus_scale": 0.40,
            "stall_combo_bonus": 0.15,
            "repeat_min_count": 3,
            "repeat_bonus": 0.20,
        }
    }


def test_instant_risk_is_max_not_sum():
    n = normalize_chat_signals(
        {
            "rider_forced_cancel": True,
            "persuasion_suspected": True,
            "signal_score": 0.4,
        }
    )
    assert instant_chat_risk(n) == 0.90


def test_fires_with_scaled_bonus_and_stall_combo():
    hot = evaluate_chat_signals(
        {"cash_offline_suggested": True},
        no_progress=True,
        policy=_policy(),
    )
    assert hot["fires"]
    assert "chat_force_cancel" in hot["reasons"]
    assert "force_cancel_with_stall" in hot["reasons"]
    assert abs(hot["abuse_bonus"] - (0.40 * 0.85 + 0.15)) < 1e-9


def test_merge_ors_flags():
    m = merge_chat_signals(
        {"persuasion_suspected": True, "signal_score": 0.6},
        {"cash_offline_suggested": True, "signal_score": 0.4},
    )
    assert m["persuasion_suspected"] and m["cash_offline_suggested"]
    assert m["signal_score"] == 0.6


def test_store_and_repeat_count(tmp_path: Path):
    store = ChatSignalStore(tmp_path / "c.db")
    for i in range(3):
        store.upsert(
            order_display_id=f"O{i}",
            driver_id=5,
            user_id=None,
            flags=normalize_chat_signals({"rider_forced_cancel": True}),
            risk=0.9,
            event_ts=f"2026-08-01T10:0{i}:00Z",
        )
    n = store.driver_signal_count(
        5, as_of="2026-08-01T12:00:00Z", window_minutes=10080, min_risk=0.55
    )
    assert n == 3
    ev = evaluate_chat_signals(
        {}, driver_signal_count=n, policy=_policy()
    )
    assert "repeat_force_cancel" in ev["reasons"]
    assert ev["abuse_bonus"] >= 0.20


def test_abuse_applies_chat_eval():
    score, reasons = abuse_feature_score(
        {
            "order_still_active": False,
            "cancel_event_count": 1,
            "driver_chain_count": 1,
            "cancel_near_destination": False,
            "chat_eval": {
                "abuse_bonus": 0.49,
                "reasons": ["chat_force_cancel", "chat_cash_offline_suggested"],
            },
        },
        {},
    )
    assert score >= 0.49
    assert "chat_force_cancel" in reasons
