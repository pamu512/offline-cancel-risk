import json
from pathlib import Path

from examples.csv_demo.__main__ import run_demo


def test_csv_demo_produces_three_heads(tmp_path):
    out = tmp_path / "out.json"
    summary = run_demo(
        orders_path=Path("examples/csv_demo/sample_orders.csv"),
        gps_path=Path("examples/csv_demo/sample_gps.csv"),
        out_path=out,
    )
    assert summary["order_count"] >= 1
    payload = json.loads(out.read_text())
    row = payload[0]
    assert set(row["scores"]) == {"cancelled_offline", "cancel_abuse", "selective_theft"}
