#!/usr/bin/env python3
"""Run labeled holdout eval and write docs/evals/latest.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from offline_cancel_risk.eval.holdout import assert_floors, run_holdout_eval
from offline_cancel_risk.settings import ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Holdout eval (labeled synthetic+demo)")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "evals" / "latest.json",
    )
    parser.add_argument(
        "--floors",
        type=Path,
        default=ROOT / "docs" / "evals" / "floors.json",
    )
    parser.add_argument(
        "--check-floors",
        action="store_true",
        help="Exit non-zero if floors.json gates fail",
    )
    args = parser.parse_args(argv)
    report = run_holdout_eval(out_path=args.out)
    print(json.dumps(report, indent=2))
    if args.check_floors:
        floors = json.loads(args.floors.read_text(encoding="utf-8"))
        assert_floors(report, floors)
        print(f"floors ok ← {args.floors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
