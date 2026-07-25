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

_DEMO_DIR = Path(__file__).resolve().parent
_DEFAULT_ORDERS = _DEMO_DIR / "sample_orders.csv"
_DEFAULT_GPS = _DEMO_DIR / "sample_gps.csv"
_DEFAULT_OUT = _DEMO_DIR / "out.json"


def run_demo(
    orders_path: Path | str,
    gps_path: Path | str,
    out_path: Path | str,
    *,
    policy_path: Path | str | None = None,
) -> dict:
    """Assess sample CSV orders offline (CsvGpsClient + CsvOrdersClient). No network."""
    orders_path = Path(orders_path)
    gps_path = Path(gps_path)
    out_path = Path(out_path)
    policy = load_policy(Path(policy_path) if policy_path else ROOT / "config" / "policy.default.yaml")
    orders = CsvOrdersClient(orders_path).load()
    gps = CsvGpsClient(gps_path)

    async def _run() -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="ocr-csv-demo-") as tmp:
            tmp_path = Path(tmp)
            stream = JsonlStreamPublisher(stream_path=tmp_path / "risk_events.jsonl")
            table = SqliteTablePublisher(sqlite_path=tmp_path / "assessments.db")
            results = []
            for req in orders:
                result = await assess_order(
                    req, gps, policy, stream=stream, table=table
                )
                results.append(result.model_dump(mode="json"))
            return results

    payload = asyncio.run(_run())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"order_count": len(payload), "out_path": str(out_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline CSV cancel-risk demo (no network)")
    parser.add_argument("--orders", type=Path, default=_DEFAULT_ORDERS)
    parser.add_argument("--gps", type=Path, default=_DEFAULT_GPS)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--policy", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = run_demo(
        orders_path=args.orders,
        gps_path=args.gps,
        out_path=args.out,
        policy_path=args.policy,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
