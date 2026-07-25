from pathlib import Path

from offline_cancel_risk.models.gates import evaluate_promotion
from offline_cancel_risk.models.metrics import ShadowMetricsStore


def test_promotion_ready_when_gates_pass(tmp_path: Path):
    store = ShadowMetricsStore(tmp_path / "m.db")
    # Challenger catches more (higher scores) without increasing FP vs champion
    for i in range(10):
        store.record(
            order_display_id=f"O{i}",
            champion_model_id="champ",
            shadow_model_id="chal",
            champion_scores={
                "cancelled_offline": 0.8 if i < 5 else 0.1,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            shadow_scores={
                "cancelled_offline": 0.8 if i < 8 else 0.1,
                "cancel_abuse": 0.1,
                "selective_theft": 0.1,
            },
            order_value=10.0,
        )
    gates = {
        "min_shadow_assessments": 10,
        # Unlabeled FP proxy is coarse; allow disagreement cost for this unit test.
        "max_fp_dollar_lift": 0.0,
        "max_fp_dollar_abs": 100.0,
        "min_catch_lift": 0.02,
    }
    thr = {
        "cancelled_offline": 0.75,
        "cancel_abuse": 0.75,
        "selective_theft": 0.75,
    }
    status = evaluate_promotion(
        challenger_model_id="chal",
        champion_model_id="champ",
        store=store,
        thresholds=thr,
        gates=gates,
    )
    assert status.promotion_ready == 1
    assert status.recommended_action == "start_canary"


def test_promotion_blocked_when_too_few(tmp_path: Path):
    store = ShadowMetricsStore(tmp_path / "m.db")
    store.record(
        order_display_id="O1",
        champion_model_id="champ",
        shadow_model_id="chal",
        champion_scores={
            "cancelled_offline": 0.1,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        shadow_scores={
            "cancelled_offline": 0.9,
            "cancel_abuse": 0.1,
            "selective_theft": 0.1,
        },
        order_value=10.0,
    )
    status = evaluate_promotion(
        challenger_model_id="chal",
        champion_model_id="champ",
        store=store,
        thresholds={
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.75,
            "selective_theft": 0.75,
        },
        gates={"min_shadow_assessments": 500, "min_catch_lift": 0.02},
    )
    assert status.promotion_ready == 0
    assert any("min_shadow_assessments" in b for b in status.promotion_blockers)
