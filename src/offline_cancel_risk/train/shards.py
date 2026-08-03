from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from offline_cancel_risk.pipeline.score_build import ML_FEATURE_KEYS
from offline_cancel_risk.train.scenarios import HEADS

FEATURE_KEYS = ML_FEATURE_KEYS

MANIFEST_NAME = "manifest.json"


def _shard_path(outdir: Path, shard_idx: int) -> Path:
    return outdir / f"shard_{shard_idx:05d}.npz"


def write_shard(
    outdir: Path | str,
    shard_idx: int,
    X: np.ndarray,
    y: np.ndarray,
    meta_lines: list[dict] | None = None,
) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = _shard_path(outdir, shard_idx)
    np.savez_compressed(
        path,
        X=X.astype(np.float32),
        y=y.astype(np.float32),
        feature_names=np.array(FEATURE_KEYS),
        head_names=np.array(HEADS),
    )
    if meta_lines is not None:
        meta_path = path.with_suffix(".meta.jsonl")
        with meta_path.open("w", encoding="utf-8") as f:
            for line in meta_lines:
                f.write(json.dumps(line) + "\n")
    return path


def load_all_shards(outdir: Path | str) -> tuple[np.ndarray, np.ndarray]:
    outdir = Path(outdir)
    manifest = read_manifest(outdir)
    X_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    for name in manifest.get("shards", []):
        path = outdir / name
        with np.load(path) as data:
            X_parts.append(data["X"])
            y_parts.append(data["y"])
    if not X_parts:
        return np.empty((0, 5), dtype=np.float32), np.empty((0, 3), dtype=np.float32)
    return np.concatenate(X_parts, axis=0), np.concatenate(y_parts, axis=0)


def read_manifest(outdir: Path | str) -> dict:
    path = Path(outdir) / MANIFEST_NAME
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_manifest(outdir: Path | str, data: dict) -> None:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / MANIFEST_NAME
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def next_shard_index(manifest: dict) -> int:
    shards = manifest.get("shards", [])
    if not shards:
        return 0
    indices = [int(Path(name).stem.split("_", 1)[1]) for name in shards]
    return max(indices) + 1
