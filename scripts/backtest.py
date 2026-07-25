from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from offline_cancel_risk.adapters.events import CsvOrdersClient
from offline_cancel_risk.adapters.gps import CsvGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import ROOT, load_policy

_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _empty_counts() -> dict[str, int]:
    return {"tp": 0, "fp": 0, "tn": 0, "fn": 0}


def _finalize(counts: dict[str, int]) -> dict[str, float | int]:
    tp, fp, tn, fn = counts["tp"], counts["fp"], counts["tn"], counts["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
    }


def run_backtest(
    orders_path: Path | str,
    gps_path: Path | str,
    out_path: Path | str,
    *,
    policy_path: Path | str | None = None,
) -> dict:
    """Score labeled CSV orders offline and emit per-head confusion metrics."""
    orders_path = Path(orders_path)
    gps_path = Path(gps_path)
    out_path = Path(out_path)
    policy = load_policy(Path(policy_path) if policy_path else ROOT / "config" / "policy.default.yaml")
    labeled = CsvOrdersClient(orders_path).load_labeled()
    gps = CsvGpsClient(gps_path)
    tallies = {head: _empty_counts() for head in _HEADS}

    async def _run() -> int:
        with tempfile.TemporaryDirectory(prefix="ocr-backtest-") as tmp:
            tmp_path = Path(tmp)
            stream = JsonlStreamPublisher(stream_path=tmp_path / "risk_events.jsonl")
            table = SqliteTablePublisher(sqlite_path=tmp_path / "assessments.db")
            n = 0
            for req, labels in labeled:
                result = await assess_order(
                    req, gps, policy, stream=stream, table=table
                )
                flags = result.flags.model_dump()
                for head in _HEADS:
                    label = labels.get(head)
                    if label is None:
                        continue
                    pred = int(flags[head])
                    truth = int(label)
                    bucket = tallies[head]
                    if pred == 1 and truth == 1:
                        bucket["tp"] += 1
                    elif pred == 1 and truth == 0:
                        bucket["fp"] += 1
                    elif pred == 0 and truth == 0:
                        bucket["tn"] += 1
                    else:
                        bucket["fn"] += 1
                n += 1
            return n

    order_count = asyncio.run(_run())
    metrics = {
        "order_count": order_count,
        "by_head": {head: _finalize(tallies[head]) for head in _HEADS},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics


def main(argv: list[str] | None = None) -> int:
    demo = ROOT / "examples" / "csv_demo"
    parser = argparse.ArgumentParser(description="Labeled CSV backtest (no network)")
    parser.add_argument("--orders", type=Path, default=demo / "sample_orders.csv")
    parser.add_argument("--gps", type=Path, default=demo / "sample_gps.csv")
    parser.add_argument("--out", type=Path, default=demo / "backtest_metrics.json")
    parser.add_argument("--policy", type=Path, default=None)
    args = parser.parse_args(argv)
    metrics = run_backtest(
        orders_path=args.orders,
        gps_path=args.gps,
        out_path=args.out,
        policy_path=args.policy,
    )
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
