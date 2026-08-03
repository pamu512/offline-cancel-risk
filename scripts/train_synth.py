#!/usr/bin/env python3
"""Generate synthetic assess shards and train a sideloadable joblib bundle."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from offline_cancel_risk.models.registry import ModelRegistry
from offline_cancel_risk.settings import ROOT, get_settings, load_policy
from offline_cancel_risk.train.dataset import generate_shard
from offline_cancel_risk.train.fit import fit_bundle
from offline_cancel_risk.train.scenarios import DEFAULT_WEIGHTS
from offline_cancel_risk.train.shards import next_shard_index, read_manifest, write_manifest

log = logging.getLogger(__name__)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_manifest(
    outdir: Path,
    *,
    phase: str,
    n_target: int,
    shard_size: int,
    seed: int,
    flip_rate: float,
    policy_path: str,
) -> dict:
    manifest = read_manifest(outdir)
    if manifest:
        return manifest
    manifest = {
        "phase": phase,
        "n_target": n_target,
        "n_done": 0,
        "shard_size": shard_size,
        "seed": seed,
        "weights": dict(DEFAULT_WEIGHTS),
        "flip_rate": flip_rate,
        "policy_path": policy_path,
        "shards": [],
    }
    write_manifest(outdir, manifest)
    return manifest


def _format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "?"
    m, s = divmod(int(seconds + 0.5), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


async def main_async(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    policy_path = str(args.policy or settings.policy_path)
    policy = load_policy(policy_path)

    manifest = _ensure_manifest(
        outdir,
        phase=args.phase,
        n_target=args.n,
        shard_size=args.shard_size,
        seed=args.seed,
        flip_rate=args.flip_rate,
        policy_path=policy_path,
    )

    n_target = int(args.n if args.n is not None else manifest.get("n_target", 0))
    if not args.train_only:
        expected_shard = next_shard_index(manifest)
        if args.start_shard is not None and args.start_shard != expected_shard:
            raise SystemExit(
                f"--start-shard {args.start_shard} != next shard index {expected_shard}; "
                "omit --start-shard to resume from manifest, or set it equal to the next index"
            )
        n_done = int(manifest.get("n_done", 0))
        shard_idx = expected_shard if args.start_shard is None else args.start_shard
        weights = manifest.get("weights", DEFAULT_WEIGHTS)
        flip_rate = float(manifest.get("flip_rate", args.flip_rate))
        seed = int(manifest.get("seed", args.seed))

        t_loop = time.perf_counter()
        while n_done < n_target:
            batch = min(args.shard_size, n_target - n_done)
            result = await generate_shard(
                phase=args.phase,
                n=batch,
                start_index=n_done,
                seed=seed,
                weights=weights,
                flip_rate=flip_rate,
                policy=policy,
                outdir=outdir,
                shard_idx=shard_idx,
            )
            manifest = read_manifest(outdir)
            n_done = int(manifest["n_done"])
            shard_idx += 1
            rows_per_sec = batch / result["seconds"] if result["seconds"] > 0 else 0.0
            remaining = n_target - n_done
            eta = remaining / rows_per_sec if rows_per_sec > 0 else float("inf")
            log.info(
                "shard %s: %d rows in %.1fs (%.1f rows/s) n_done=%d/%d ETA %s",
                Path(result["path"]).name,
                batch,
                result["seconds"],
                rows_per_sec,
                n_done,
                n_target,
                _format_eta(eta),
            )
        log.info(
            "generation done: %d rows in %.1fs",
            n_done,
            time.perf_counter() - t_loop,
        )

    bundle_dir = args.bundle_dir
    if bundle_dir is None:
        bundle_dir = ROOT / "data" / "models" / f"synth-phase-{args.phase}-{_utc_stamp()}"
    else:
        bundle_dir = Path(bundle_dir)

    metrics = fit_bundle(outdir, bundle_dir, seed=args.seed)
    log.info("bundle → %s", bundle_dir)
    print(json.dumps(metrics, indent=2))

    if args.sideload_shadow:
        reg = ModelRegistry(settings.models_sqlite_path, settings.models_root)
        rec = reg.sideload(bundle_dir, role="shadow")
        log.info("sideloaded shadow model_id=%s", rec.model_id)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("a", "b"), required=True)
    parser.add_argument(
        "--n",
        type=int,
        default=None,
        help="Target row count (required unless --train-only)",
    )
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--shard-size", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--start-shard",
        type=int,
        default=None,
        help="Override next shard index for resume",
    )
    parser.add_argument(
        "--flip-rate",
        type=float,
        default=None,
        help="Label flip rate (phase b default 0.05; phase a default 0.0)",
    )
    parser.add_argument(
        "--train-only",
        action="store_true",
        help="Skip generation; fit bundle from existing shards",
    )
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument(
        "--sideload-shadow",
        action="store_true",
        help="Register trained bundle as shadow model",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Policy YAML (default: OCR_POLICY_PATH)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="INFO logging",
    )
    args = parser.parse_args(argv)

    if args.n is None and not args.train_only:
        parser.error("--n is required unless --train-only")
    if args.n is None:
        args.n = read_manifest(args.outdir).get("n_target", 0)
    if args.flip_rate is None:
        args.flip_rate = 0.05 if args.phase == "b" else 0.0

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    # Always show shard progress lines.
    log.setLevel(logging.INFO)

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
