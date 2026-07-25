import json
from pathlib import Path

from scripts.backtest import run_backtest


def test_backtest_produces_labeled_metrics(tmp_path):
    out = tmp_path / "metrics.json"
    metrics = run_backtest(
        orders_path=Path("examples/csv_demo/sample_orders.csv"),
        gps_path=Path("examples/csv_demo/sample_gps.csv"),
        out_path=out,
    )
    assert metrics["order_count"] >= 1
    assert set(metrics["by_head"]) == {
        "cancelled_offline",
        "cancel_abuse",
        "selective_theft",
    }
    for head in metrics["by_head"].values():
        assert {"tp", "fp", "tn", "fn", "precision", "recall"} <= set(head)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["order_count"] == metrics["order_count"]
