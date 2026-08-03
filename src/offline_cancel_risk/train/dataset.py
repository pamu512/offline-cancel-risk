from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.pipeline.score_build import ML_FEATURE_KEYS
from offline_cancel_risk.train.labels import flip_labels, teacher_labels_from_flags
from offline_cancel_risk.train.scenarios import HEADS, build_scenario, draw_template
from offline_cancel_risk.train.shards import read_manifest, write_manifest, write_shard


async def generate_shard(
    *,
    phase: str,
    n: int,
    start_index: int,
    seed: int,
    weights: dict[str, float],
    flip_rate: float,
    policy: dict,
    outdir: Path | str,
    shard_idx: int,
) -> dict:
    outdir = Path(outdir)
    rng = np.random.default_rng(seed + shard_idx)
    Xs: list[list[float]] = []
    ys: list[list[int]] = []
    metas: list[dict] = []

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        stream = JsonlStreamPublisher(stream_path=tmp_path / "events.jsonl")
        table = SqliteTablePublisher(sqlite_path=tmp_path / "assessments.db")
        t0 = time.perf_counter()
        for i in range(n):
            idx = start_index + i
            template = draw_template(rng, weights)
            row = build_scenario(
                template,
                order_display_id=f"SYN-{phase}-{idx}",
                driver_id=1000 + (idx % 50000),
                rng=rng,
            )
            sink: dict[str, float] = {}
            result = await assess_order(
                row.request,
                FakeGpsClient(row.points),
                policy,
                stream=stream,
                table=table,
                feature_sink=sink,
            )
            if phase == "a":
                labels = row.labels
            else:
                labels = flip_labels(
                    teacher_labels_from_flags(result.flags.model_dump()),
                    rng,
                    flip_rate=flip_rate,
                )
            Xs.append([sink[k] for k in ML_FEATURE_KEYS])
            ys.append([labels[h] for h in HEADS])
            metas.append(
                {"order_display_id": row.request.order_display_id, "template": template}
            )
        elapsed = time.perf_counter() - t0

    X = np.array(Xs, dtype=np.float32)
    y = np.array(ys, dtype=np.float32)
    path = write_shard(outdir, shard_idx, X, y, meta_lines=metas)

    manifest = read_manifest(outdir)
    manifest["n_done"] = int(manifest.get("n_done", 0)) + n
    shards = list(manifest.get("shards", []))
    shards.append(path.name)
    manifest["shards"] = shards
    write_manifest(outdir, manifest)

    return {"n": n, "path": str(path), "seconds": elapsed}
