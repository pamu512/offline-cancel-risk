# Synthetic 500k train (full assess) — design

**Date:** 2026-08-03  
**Status:** draft for review  
**Goal:** Overnight-scale (~500k) full-assess synthetic datasets → train sideloadable joblib bundles; Phase A scenario-injected labels, then Phase B noisy teacher.

## Constraints (locked)

| Decision | Choice |
|---|---|
| Data source | Synthetic (no external 500k corpus) |
| Feature path | Full `assess_order` (not feature-table-only) |
| Labels Phase A | Scenario-injected ground truth |
| Labels Phase B | Noisy teacher (rule flags/scores + flip rate) |
| Runtime | Overnight OK for true ~500k each phase |
| Delivery | Joblib directory bundles + shadow sideload |
| Champion | Not auto-promoted |

## Non-goals

- Redis/SQS training fleet
- ONNX export (joblib only for v1)
- Replacing rule heads as champion
- Chat NLP / new assess features
- Claiming A+++ lift from synth alone (holdout floors still apply)

## Pipeline (Approach A — sharded)

```
templates → (orders + GPS shard)
        → assess_order × N
        → append features + labels
        → train 3-head model
        → bundle (model.json, feature_schema.json, model.joblib, metrics_baseline.json)
        → registry sideload role=shadow
```

Shards (~10k–25k rows each) under:

- `data/train/phase_a/shard_XXXX.npz` (features + labels; no new deps)
- `data/train/phase_b/…`
- Companion `shard_XXXX.meta.jsonl` optional for `order_display_id` / `template` debug

Resume via `--start-shard N`. Manifest `manifest.json` records `n_target`, `n_done`, template weights, seed, git/policy hash.

### Feature vector

Match assess ML keys already used in `score_build._ML_FEATURE_KEYS`:

- `final_stop_confidence`
- `sequence_score`
- `dwell_fraction`
- `abuse_score`
- `theft_score`

`feature_schema.json` version bumps only if keys change. Bundle predictor consumes the same dict shape as shadow scoring today.

### Assess during train

- Use default policy + in-memory / temp publishers (no need to pollute prod `assessments.db`).
- GPS via in-memory `FakeGpsClient` (or equivalent) keyed by `order_display_id`.
- Sequential assess by default (ponytail); optional `--workers` is a later upgrade.
- Skip control-plane / ticket side effects (direct `assess_order`, not HTTP).

## Phase A — scenario-injected (~500k)

### Templates

| Template | Intent | Default weight | Labels (offline, abuse, theft) |
|---|---|---|---|
| `theft_dwell` | Long dwell / selective-theft geometry | 0.20 | (0, 0, 1) |
| `plain_offline` | Offline-style cancel, weak theft | 0.25 | (1, 0, 0) |
| `abuse_chain` | Multi-cancel / abuse-shaped signals | 0.15 | (0, 1, 0) |
| `clean_cancel` | Benign cancel, low all heads | 0.30 | (0, 0, 0) |
| `gps_sparse` | Sparse/unavailable GPS | 0.10 | (0, 0, 0) |

Weights must sum to 1.0; CLI overrides allowed.

Each row:

1. Draw template from weights (+ deterministic RNG seed).
2. Build `AssessRequest` + GPS points consistent with the template (reuse patterns from golden/demo fixtures where possible).
3. Set **fixed** 3-head labels from the template table (not from assess flags).
4. Run assess; persist feature vector + labels + `template` + `order_display_id`.

### Train

- Use **`MultiOutputRegressor`** over a regressor that emits `[0, 1]` scores (e.g. `HistGradientBoostingRegressor` or `Ridge`), matching existing `load_bundle` which calls `.predict()` (not `predict_proba`). Targets are `{0,1}` labels; predictions clamped to `[0, 1]` at blend time as today.
- Bundle contract must satisfy `load_bundle` / shadow path.
- Hold out ~5% of Phase A for metrics in `metrics_baseline.json`.

## Phase B — noisy teacher (~500k)

Separate generation run (new order ids / seed).

1. Generate from the same template geometry library (distribution may match Phase A or be slightly rebalanced).
2. Run assess; teacher label = thresholded rule/flag path already on `AssessmentResult` (per-head flag, or `score >= τ` from policy thresholds).
3. Flip each head independently with probability `ε` (default `0.05`; CLI `--flip-rate`).
4. Train → second bundle directory `…-noisy-teacher`.
5. Sideload as shadow via existing registry (`role=shadow`), which replaces the previous shadow pointer.

## Eval gates

| Check | Phase A | Phase B |
|---|---|---|
| Synth holdout P/R per head | Must beat always-0 / always-1 baselines on pattern-relevant heads | Report teacher-agreement accuracy on unflipped holdout |
| Existing `scripts/eval_holdout.py --check-floors` | Must still pass with rules path; shadow ML must not break assess | Same |
| Smoke | Sideload bundle; one assess returns `ml_scores` | Same |

Do **not** treat synth metrics as production lift proof.

## CLI

```bash
# Phase A overnight
python scripts/train_synth.py --phase a --n 500000 --outdir data/train/phase_a --seed 7

# Resume
python scripts/train_synth.py --phase a --n 500000 --outdir data/train/phase_a --start-shard 12

# Phase B after A
python scripts/train_synth.py --phase b --n 500000 --outdir data/train/phase_b --seed 11 --flip-rate 0.05

# Optional: train-only from existing shards
python scripts/train_synth.py --phase a --outdir data/train/phase_a --train-only
```

Sideload helper (or flag `--sideload-shadow`):

```bash
# via existing API / registry helper
```

## Bundle layout

```
data/models/synth-phase-a-<timestamp>/
  model.json
  feature_schema.json
  model.joblib          # {"model": <estimator>, "heads": [...]}
  metrics_baseline.json
```

Same for Phase B with distinct `model_id`.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Overnight kill mid-run | Shards + manifest resume |
| Model memorizes rules (Phase A) | Scenario labels independent of flags; still possible correlation — report rule-vs-label agreement |
| Phase B useless if ε=0 | Default ε=0.05; document distillation nature |
| Disk blowup from GPS CSV | Don’t persist full GPS long-term; keep feature shards only |
| 500k assess too slow | Shard progress logs ETA; document measured rows/sec on first 1k |

## Success criteria

1. Phase A completes ≥500k assessed labeled rows (or resumes to that count).
2. Phase B same with noisy teacher labels.
3. Both bundles load via `load_bundle` and sideload as shadow.
4. Unit tests cover: template label table, one-shard generate+assess+train (tiny N), flip-rate math, resume manifest.
5. OPS note: how to run overnight + where artifacts land.

## Implementation order (after plan)

1. Scenario GPS/order generators + label table  
2. Shard assess → feature writer  
3. Train + bundle emitter  
4. Phase B flip labeling  
5. Tests (tiny N) + OPS blurb  
6. Kick overnight Phase A, then Phase B  

## Out of scope for first PR

- Multiprocess `--workers`
- Auto-canary / promote
- New serialization deps (npz/jsonl only)
