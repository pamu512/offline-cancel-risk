from pathlib import Path

from offline_cancel_risk.features.entity_stats import EntityCancelStatsStore
from offline_cancel_risk.features.marketplace_math import (
    accept_cancel_rate,
    completion_rate,
    evaluate_marketplace_signals,
    with_cause_fraction,
)


def test_marketplace_math_definitions():
    assert accept_cancel_rate(10, 5) == 0.5
    assert accept_cancel_rate(0, 3) == 3.0  # degenerate; gated by support
    assert completion_rate(6, 4) == 0.6
    assert with_cause_fraction(2, 8) == 0.25
    assert with_cause_fraction(0, 0) == 1.0


def test_signals_respect_support_gates():
    policy = {
        "marketplace_min_support": 5,
        "accept_cancel_rate_threshold": 0.45,
        "completion_rate_floor": 0.40,
        "with_cause_fraction_floor": 0.35,
    }
    low_n = evaluate_marketplace_signals(
        accepts=2, cancels=2, completes=0, with_cause_cancels=0, policy=policy
    )
    assert low_n["signals"] == []

    hot = evaluate_marketplace_signals(
        accepts=10, cancels=6, completes=2, with_cause_cancels=1, policy=policy
    )
    assert "high_accept_cancel_rate" in hot["signals"]
    assert "low_completion_rate" in hot["signals"]
    assert "cancel_without_cause_heavy" in hot["signals"]


def test_store_funnel_and_stats(tmp_path: Path):
    store = EntityCancelStatsStore(tmp_path / "m.db")
    # 6 accepts, 4 cancels (1 with cause), 2 completes
    for i in range(6):
        store.record_market_event(
            driver_id=1,
            user_id=None,
            order_display_id=f"A{i}",
            event_type="accept",
            event_ts=f"2026-08-01T10:0{i}:00Z",
        )
    for i in range(4):
        store.record_market_event(
            driver_id=1,
            user_id=None,
            order_display_id=f"C{i}",
            event_type="cancel",
            event_ts=f"2026-08-01T11:0{i}:00Z",
            cancel_with_cause=(i == 0),
        )
    for i in range(2):
        store.record_market_event(
            driver_id=1,
            user_id=None,
            order_display_id=f"K{i}",
            event_type="complete",
            event_ts=f"2026-08-01T12:0{i}:00Z",
        )
    st = store.stats(
        driver_id=1,
        user_id=None,
        as_of="2026-08-01T13:00:00Z",
        window_minutes=180,
        abuse_policy={
            "marketplace_min_support": 5,
            "accept_cancel_rate_threshold": 0.45,
            "completion_rate_floor": 0.40,
            "with_cause_fraction_floor": 0.35,
        },
    )
    assert st["accepts"] == 6
    assert st["cancels"] == 4
    assert st["completes"] == 2
    assert st["accept_cancel_rate"] == 4 / 6
    assert st["completion_rate"] == 2 / 6
    assert "high_accept_cancel_rate" in st["signals"]
