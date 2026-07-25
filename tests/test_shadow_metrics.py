from pathlib import Path

from offline_cancel_risk.models.metrics import ShadowMetricsStore


def test_shadow_metrics_record_and_aggregate(tmp_path: Path):
    store = ShadowMetricsStore(tmp_path / "m.db")
    store.record(
        order_display_id="O1",
        champion_model_id="champ",
        shadow_model_id="shadow",
        champion_scores={
            "cancelled_offline": 0.2,
            "cancel_abuse": 0.2,
            "selective_theft": 0.2,
        },
        shadow_scores={
            "cancelled_offline": 0.9,
            "cancel_abuse": 0.2,
            "selective_theft": 0.2,
        },
        order_value=50.0,
    )
    assert store.count_for_shadow("shadow") == 1
    agg = store.aggregate_fp_dollar_proxy(
        "shadow",
        {
            "cancelled_offline": 0.75,
            "cancel_abuse": 0.75,
            "selective_theft": 0.75,
        },
    )
    assert agg["n"] == 1
    assert agg["fp_dollar_proxy"] == 50.0
