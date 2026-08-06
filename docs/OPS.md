# Ops & engineering manual

How to run, tune, use, and maintain **offline-cancel-risk** in production-shaped deployments.

Audience: ops/platform engineers integrating the service, and product teams wiring a tuning UI. For a one-page overview, see the [README](../README.md). For bring-up, minimum requirements, and tuning order, see the [MANUAL](MANUAL.md).

---

## 1. System map

| Piece | Role |
|---|---|
| Assess pipeline | Cancel + GPS → scores, flags, EAR, routing, reasons |
| Publishers | JSONL risk stream + SQLite assessments / feedback |
| Policy | Default YAML + region/city overlays inside guardrails |
| Feedback sampler | Daily label-ticket quota (inline + batch) |
| Control plane | Pattern-cohort precision/recall, supply operating point, hardgates, tuner, audit |
| Entity baselines | Rolling+EWMA FP dampener (`mode: apply` for offline/abuse; theft head stays `shadow`) |
| Platform patterns | Cancel stage, heading-aware A→B progress, teleport dampen, cancel-rate/pair density, evidence pack |
| Models | Optional joblib/ONNX champion / shadow / canary |

**Ownership boundary**

| Concern | Owner |
|---|---|
| Tuning UI | Product |
| Enforce / clawback actions | Downstream |
| Supply/demand forecast | Driver Ops / Platform S&D (ingested here) |
| Local enforcement volume caps | Local ops (ingested here) |
| Score computation + overlay apply | This service |

---

## 2. Install & run

### 2.1 Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### 2.2 HTTP API

```bash
# Local: run assess inline (no background worker)
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --host 0.0.0.0 --port 8000

# Production-shaped: async worker inside the process
uvicorn offline_cancel_risk.main:app --host 0.0.0.0 --port 8000
```

Health: `GET /v1/health` → `{"status":"ok"}` (liveness).  
Ready: `GET /v1/ready` → 200 when auth/GPS config is sane for the profile; 503 otherwise (empty Fake GPS under prod).

### 2.3 Important environment variables (`OCR_*`)

| Variable | Default (conceptually) | Purpose |
|---|---|---|
| `OCR_PROFILE` | `demo` | `prod` forces auth, requires `OCR_API_KEYS`, and requires `OCR_GPS_BASE_URL` (or injected GPS client) |
| `OCR_SYNC_ASSESS` | `false` | `true` = assess in request path (dev) |
| `OCR_AUTH_REQUIRED` | `false` | Require API key / bearer on assess + control routes (`true` under `OCR_PROFILE=prod`) |
| `OCR_API_KEYS` | empty | Comma-separated keys when auth on |
| `OCR_QUEUE_BACKEND` | `memory` | `sqlite` = durable multi-process assess job queue |
| `OCR_ASSESS_QUEUE_PATH` | `data/assess_queue.db` | SQLite path when `OCR_QUEUE_BACKEND=sqlite` |
| `OCR_CONTROL_PLANE_LOCK_PATH` | _(sibling of control-plane db)_ | File flock so only one replica runs control-plane ticks |
| `OCR_GPS_BASE_URL` / `OCR_GPS_API_KEY` | empty | HTTP GPS adapter; empty → fake/empty GPS |
| `OCR_STREAM_URL` / `OCR_STREAM_API_KEY` | empty | When set, fan-out each assessment JSON to HTTP webhook + local JSONL |
| `OCR_STREAM_TIMEOUT_S` | `5` | HTTP stream POST timeout |
| `OCR_POLICY_PATH` | `config/policy.default.yaml` | Base policy |
| `OCR_POLICY_GUARDRAILS_PATH` | `config/policy_guardrails.default.yaml` | Overlay bounds |
| `OCR_POLICY_OVERLAYS_PATH` | `data/policy_overlays.db` | Market overlays |
| `OCR_SQLITE_PATH` | `data/assessments.db` | Assessments + feedback |
| `OCR_STREAM_PATH` | `data/risk_events.jsonl` | Risk event stream |
| `OCR_CONTROL_PLANE_SQLITE_PATH` | `data/control_plane.db` | Forecast, hardgates, metrics, audit, DBSCAN retune runs |
| `OCR_ASSESS_GPS_CACHE_PATH` | `data/assess_gps_cache.db` | Assess-time GPS replay cache for market DBSCAN retune |
| `OCR_OPERATING_POINT_PATH` | `config/operating_point.default.yaml` | Peak/surplus P/R bands |
| `OCR_LABEL_TICKETS_PATH` | `data/label_tickets.db` | Review tickets |
| `OCR_LABEL_TICKETS_STREAM_PATH` | `data/label_tickets.jsonl` | Ticket stream |
| `OCR_DRIVER_CHAINS_PATH` | `data/driver_chains.db` | Cross-order cancel chains |
| `OCR_ENTITY_BASELINES_PATH` | `data/entity_baselines.db` | Driver/user/pair score baselines |
| `OCR_CALIBRATORS_PATH` | `data/calibrators.db` | Per head×market score calibrators |
| `OCR_ENTITY_CANCEL_STATS_PATH` | `data/entity_cancel_stats.db` | Cancel-rate / pair density events |
| `OCR_DEVICE_INTEGRITY_PATH` | `data/device_integrity.db` | Device integrity EWMA / last flags |
| `OCR_DEVICE_GRAPH_PATH` | `data/device_graph.db` | Device↔driver/user graph edges |
| `OCR_CHAT_SIGNALS_PATH` | `data/chat_signals.db` | Force-cancel / chat persuasion flags |
| `OCR_ENTITY_ANOMALY_PATH` | `data/entity_anomaly.db` | Peer/self feature anomaly samples |
| `OCR_OUTCOMES_PATH` | `data/outcomes.db` | Downstream outcome events + per-market recoverability EWMA |
| `OCR_DATABASE_URL` | _(empty)_ | Postgres URL for assessments/feedback (`pip install -e ".[pg]"`); empty → SQLite |
| `OCR_MODELS_*` | under `data/` | Registry, shadow metrics, canary |
| `OCR_TUNER_MIN_LABELED` | `30` | Min labels before auto-apply |
| `OCR_TUNER_COOLDOWN_MINUTES` | `60` | Min gap between applies per head/market |
| `OCR_TUNER_MIN_F1_LIFT` | `0.01` | Min holdout **pattern-cohort recall** lift to apply |
| `OCR_METRICS_DEBOUNCE_SECONDS` | `30` | Coalesce feedback → tune |
| `OCR_CONTROL_PLANE_TICK_SECONDS` | `0` | Periodic sample+tune (`0` = off) |

Persist `data/` (or your mounted volume) across deploys.

### Synthetic model train (overnight)

A++ multi-replica env vars (`OCR_PROFILE` / `OCR_QUEUE_BACKEND` / lock) require the corresponding uncommitted/ops PR — this section's overnight train commands work on the train branch alone.

Phase A then B (~500k full assess each):

```bash
python scripts/train_synth.py --phase a --n 500000 --outdir data/train/phase_a --seed 7
python scripts/train_synth.py --phase b --n 500000 --outdir data/train/phase_b --seed 11 --flip-rate 0.05 --sideload-shadow
```

Resume with `--start-shard N` or re-run same `--outdir` (manifest `n_done`).

---

## 3. Day-to-day use

### 3.1 Assess a cancel

`POST /v1/assess` with order context (`order_display_id`, `driver_id`, timestamps, stops `latlong`, value, category, optional replacement / reassign events / `next_driver_no_order`, **`region_code` / `city_code`**).

```bash
curl -X POST localhost:8000/v1/assess \
  -H 'content-type: application/json' \
  -d @order.json
# → {"job_id":"...","status":"queued"|"done"}
curl localhost:8000/v1/assess/{job_id}
curl localhost:8000/v1/orders/{order_display_id}/latest
```

Batch: `POST /v1/assess:batch`.

**Late evidence:** set `"force_reassess": true` on a new assess with updated fields. Generation increments; prior generations are marked `provisional=true`. History: `GET /v1/orders/{id}/generations`.

### 3.2 Read results

Each assessment includes:

- `scores` / `flags` — three heads  
- `rule_scores` / `ml_scores` — blend inputs  
- `expected_revenue_at_risk` + `attention_score`  
- `routing` — `{priority, queue, ...}` for review queues  
- `reasons` — explainability codes  
- `gps_window` — window used / sparsity  
- `policy_hash`, `model_version`, `assessment_generation`

Downstream should key actions off **flags + EAR/attention**, not raw feature dumps alone. Prefer consuming the stream (`risk_events.jsonl` or your bound publisher) plus the table for lookups.

### 3.3 Label feedback (ML loop)

1. Pull open tickets: `GET /v1/feedback/tickets?status=open`  
2. Reviewer labels offline/abuse/theft  
3. `POST /v1/feedback` with `order_display_id` + `labels` → closes open tickets and **debounces** a metrics+tune cycle for that market  

Fill the day’s quota:

```bash
curl -X POST localhost:8000/v1/feedback/sample \
  -H 'content-type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL"}'
# or
python scripts/sample_feedback_tickets.py --region-code PH --city-code MNL
```

### 3.4 Supply & enforcement inputs

**Forecast** (S&D) — drives precision/recall operating point:

```bash
curl -X PUT localhost:8000/v1/supply/forecast \
  -H 'content-type: application/json' \
  -d '{
    "rows":[{
      "region_code":"PH","city_code":"MNL",
      "period_start":"2026-08-03T00:00:00Z",
      "period_end":"2026-08-04T00:00:00Z",
      "forecast_supply":120,"forecast_demand":100,
      "source":"driver_ops"
    }]
  }'
```

**Hardgates** (local ops) — max projected enforcements per window:

```bash
curl -X PUT localhost:8000/v1/enforcement/hardgates \
  -H 'content-type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","window":"hour","max_enforcements":50}'
```

**Clawback signal** (Downstream posts when they claw back) — biases tuner toward peak band and **halves** effective hardgates for `ttl_minutes`:

```bash
curl -X POST localhost:8000/v1/enforcement/clawback \
  -H 'content-type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","ttl_minutes":60,"reason":"supply_dip"}'
```

### 3.5 Outcome → EAR recoverability (Downstream)

When Downstream resolves an enforcement action, post the outcome so per-market recoverability EWMA can learn from clawback/payout results. Persisted at `OCR_OUTCOMES_PATH` (default `data/outcomes.db`).

**Outcome types** (`outcome` field):

| Value | EWMA signal | Meaning |
|---|---|---|
| `clawback_won` | 1.0 | Recovered funds / successful clawback |
| `payout_blocked` | 1.0 | Payout blocked before loss |
| `account_actioned` | 1.0 | Account action taken (suspension, etc.) |
| `clawback_lost` | 0.0 | Clawback failed or write-off |

`head`, `region_code`, and `city_code` default from the latest assessment when omitted. Idempotent on `(order_display_id, outcome, occurred_at)`.

```bash
curl -X POST localhost:8000/v1/outcomes \
  -H 'content-type: application/json' \
  -d '{
    "order_display_id":"ORD-123",
    "outcome":"clawback_won",
    "occurred_at":"2026-08-03T12:00:00Z"
  }'
# → {"ok":true,"head":"selective_theft","region_code":"PH","city_code":"MNL",
#    "recoverability":{...},"n_updates":1}

curl 'localhost:8000/v1/outcomes/recoverability?region_code=PH&city_code=MNL'
# → includes ear_shadow: static vs learned delta, apply_ready per head, recommendation
curl 'localhost:8000/v1/outcomes?order_display_id=ORD-123&limit=100'
```

**Shadow vs apply** (`policy.ear.mode`, default `shadow`):

- **Shadow** — live `expected_revenue_at_risk` / `attention_score` use static policy recoverability (unchanged golden scores). Assessments include `ear_meta` with learned weights for monitoring. Use `ear_shadow.recommendation == consider_apply` when all heads have `n_updates ≥ min_updates_apply`.
- **Apply** — after `min_updates_apply` (default 5) EWMA updates per head, live EAR uses learned recoverability; cold heads still fall back to static defaults.

Knobs: `policy.ear.outcome_ewma_alpha` (default `0.05`), `policy.ear.min_updates_apply`. Auth required when `OCR_AUTH_REQUIRED=1` or `OCR_PROFILE=prod`.

See [outcome-ear-loop design](superpowers/specs/2026-08-03-outcome-ear-loop-design.md).

### 3.6 Place / vehicle class (Downstream-only)

Optional assess fields `place_class` / `vehicle_class` scale target dwell \(D\). Omitted → `unknown` (factor 1.0). No POI inference here.

| Enum | Examples |
|---|---|
| `place_class` | `curb`, `residential`, `apartment`, `commercial`, `unknown` |
| `vehicle_class` | `walker`, `cycle`, `two_wheel`, `van_pickup`, `large_4w`, `box_truck`, `semi`, `unknown` |

Fill-rate: `GET /v1/ops/presence-fill` over latest assessments. Tune factors via market overlays on `dwell.place_factors` / `dwell.vehicle_factors` / `dwell.min_dwell_seconds`.

After labels exist and place fill-rate is healthy, `GET /v1/ops/dwell-factor-nudges` suggests factor changes from offline FP/FN by `place_class` (review → overlay; never auto-applied). Chat/device fill: `GET /v1/ops/downstream-fill`.

### 3.7 Label feedback SLO

Aim to close ≥ `feedback.per_head_min` (default 5) tickets **per head per UTC day** within `daily_review_quota`. Holdout reports `sampler_strata` tags for cohort coverage (`pattern_mass`, `coverage`, transit slice helpers).

---

## 4. Tuning manual

### 4.1 Policy layers

```
config/policy.default.yaml          ← global defaults
        ↑ merge
region overlay (city_code="")       ← PUT /v1/policy/overlays
        ↑ merge
city overlay                        ← same API; city wins
```

All numeric overlay fields must fall inside `config/policy_guardrails.default.yaml`. Server returns `400` if not.

```bash
curl localhost:8000/v1/policy/guardrails
curl 'localhost:8000/v1/policy/resolved?region_code=PH&city_code=MNL'
curl -X PUT localhost:8000/v1/policy/overlays \
  -H 'content-type: application/json' \
  -d '{
    "region_code":"PH","city_code":"MNL",
    "overlay":{
      "thresholds":{"cancelled_offline":0.82},
      "sequence":{"offline_weight":1.8},
      "routing":{"p1_attention_min":150}
    }
  }'
```

Manual overlay writes are audited as `manual_overlay`.

### 4.2 Knobs that matter most

| Knob | Effect | When to touch |
|---|---|---|
| `thresholds.*` | Soft score → flag | Primary precision/recall lever |
| `sequence.offline_weight` | Weight of route-sequence evidence in offline rule | Offline FPs/FNs from path mismatch |
| `dbscan.*` / `dwell.*` | Stop clustering / dwell | GPS noise or sparse markets |
| `dbscan.autoscale_min_pts` + `autoscale_ref_gap_seconds` | Scale `min_pts` from median ping gap | Mixed 1s–30s ping platforms |
| `dwell.gap_seconds_min` / `gap_seconds_max` | Clamp τ before autoscale | Bursty or batched GPS skewing median gap |
| `dwell.place_factors` / `vehicle_factors` | Target dwell \(D\) multipliers (optional assess fields) | Market/mode mix; skip fields → 1.0 |
| `dwell.radius_m` / `max_run_displacement_m` | Tighter pin + reject traffic crawl | Signal/queue FPs near stop |
| `blend.*.rule_weight` / `ml_weight` | Rules vs ML | After shadow model is healthy |
| `theft.high_value_amount` / `food_categories` | Theft head sensitivity | Category mix changes |
| `abuse.near_dest_radius_m` / `chain_lookback_minutes` | Abuse geography & chains | Reassign-heavy markets |
| `routing.p1_attention_min` / `p2_*` | Review priority cutoffs | Queue capacity |
| `feedback.daily_review_quota` + per-head min/max | Label budget | Reviewer capacity |
| `ear.*` | $ attention ranking | Ops prioritization, not flags |

### 4.3 Entity anomaly watch (peer / self)

`policy.anomaly` (default `mode: apply`) records rolling features (`accept_cancel_rate`, `cancel_rate`, `cancel_abuse`) and flags MAD z-score spikes vs self-history / city·region peers. Apply adds the abuse bonus; set `mode: shadow` for `anomaly_shadow:*` reasons only.

### 4.4 Entity baselines (driver / user / pair)

`policy.baselines` tracks rolling-window + EWMA score baselines per entity×head.

- **Default `mode: apply`** for offline/abuse; **`selective_theft` head stays `shadow`**. Per-head overrides allowed.
- Discount (apply) only when score is in `(baseline + above_epsilon, armed_thr)` — never damps absolute threshold crossings.
- Assessments store `scores_raw` (pre-discount) for learning joins; warehouse pulls `GET /v1/baselines?updated_since=...`.
- Pair uses `pair_window_n` (smaller); backoff prefers pair → driver → user.

See [entity-baseline-gate design](superpowers/specs/2026-08-03-entity-baseline-gate-design.md).

### 4.4 Learning objective (pattern precision)

Primary goal: **detect recurring behavioral patterns** that make up ~98% of labelable mass. Explicitly deprioritize novel/exotic fraud and latency.

Interpretation of “98%”: aim ~**0.98 precision on the pattern cohort** \(S\) (common strata in `policy.learning.pattern_strata`), then maximize **recall on \(S\)**. This is **not** global fraud recall and **not** global F1.

| Metric | Use |
|---|---|
| Precision / recall on \(S\) | Tuner objective and apply gates |
| Global P/R/F1 on all labels | Monitoring / dashboards only |
| Novel / long-tail cases | Out of scope until \(S\) is stable |

Config: `policy.learning` (`target_precision`, `min_pattern_support`, `min_pattern_recall`, `pattern_mass_fraction`, `blend_search_min_support`, `pattern_strata`).

### 4.5 Supply-aware operating point

`config/operating_point.default.yaml` maps `supply_ratio = forecast_supply / forecast_demand`:

- **Peak / tight supply** (low ratio) → higher min precision, lower min recall  
- **Surplus** (high ratio) → modestly higher min recall, looser precision floor (soft; must not force a 2% chase)  
- Mid ratios interpolate  

Pattern-cohort precision remains the tuner’s hard learning gate; supply bands are ops context, not a license to over-recall the tail.

### 4.6 Auto-tuner (when to trust it)

Trigger:

- Debounced after feedback (`OCR_METRICS_DEBOUNCE_SECONDS`)  
- `POST /v1/tuning/run` `{ "region_code","city_code" }`  
- CLI: `python scripts/run_tuner.py --region-code PH --city-code MNL`  
- Optional tick: `OCR_CONTROL_PLANE_TICK_SECONDS > 0`  

Behavior:

1. Join assessments ↔ labels → train / holdout split  
2. Restrict metrics to **pattern cohort** \(S\) per head  
3. Grid-search thresholds (blend / `p1_attention_min` only if pattern support ≥ `blend_search_min_support`)  
4. Candidate must hit holdout \(\mathrm{Precision}_S \ge\) `target_precision` and \(\mathrm{Recall}_S \ge\) `min_pattern_recall`, plus hardgates / guardrails  
5. Among candidates, maximize holdout \(\mathrm{Recall}_S\) (tie-break: higher precision)  
6. Apply overlay only if holdout **pattern recall** lifts by ≥ `OCR_TUNER_MIN_F1_LIFT` and cooldown elapsed  

Inspect:

```bash
curl 'localhost:8000/v1/metrics/labels?region_code=PH&city_code=MNL'
curl 'localhost:8000/v1/tuning/suggestions?limit=20'
curl 'localhost:8000/v1/audit/policy?limit=50'
```

**Do not** rely on auto-tune with &lt; `learning.min_pattern_support` labeled pattern-cohort examples per head/market—it will reject with `insufficient_pattern_labels`.

### 4.6a DBSCAN market retune (eps / min_pts)

Hybrid loop: assess writes GPS tracks + request into `OCR_ASSESS_GPS_CACHE_PATH` → offline grid over `(clustering_radius_m, min_pts)` ∩ guardrails → re-assess with cached points → pattern Precision_S / Recall_S on `cancelled_offline`.

Config: `policy.dbscan_retune` (default `mode: shadow`). When `mode: apply` (or `POST` with `"mode":"apply"`), overlay writes automatically if gates + cooldown pass (same learning gates as the threshold tuner). Autoscale / dwell factors still apply on top of retuned refs.

```bash
# Shadow first (no overlay write)
curl -X POST localhost:8000/v1/tuning/dbscan-retune \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL"}'
curl 'localhost:8000/v1/tuning/dbscan-retune/latest?region_code=PH&city_code=MNL'

# After review: apply once (or set policy.dbscan_retune.mode=apply)
curl -X POST localhost:8000/v1/tuning/dbscan-retune \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","mode":"apply"}'
```

Optional tick: `dbscan_retune.on_tick: true` with `OCR_CONTROL_PLANE_TICK_SECONDS > 0`.

Does **not** add a new clusterer or cross-order geo clustering — only retunes v5 per-trip DBSCAN refs.

### 4.6b Score calibration (Platt / isotonic)

Offline fit on pattern cohort \(S\) using `scores_raw` (pre-baseline). Assess runs calibration after baselines / before thresholds.

**Two separate knobs:**

| Knob | What it does |
|---|---|
| `POST /v1/tuning/calibrate` `mode` | Fit control only. Default/shadow/apply all **upsert calibrators** to `OCR_CALIBRATORS_PATH` when ECE + cooldown pass. `mode: off` skips fit. **`mode: apply` on POST does not turn on live score replacement.** |
| `policy.calibration.mode` (YAML or market overlay) | Assess-time behavior. Default `shadow` fills `calibration_meta` with `p` but leaves live `scores[h]` unchanged. **`apply`** replaces `scores[h]=p` (raw stays in `scores_raw`). |

```bash
# Fit (default mode=shadow; upserts calibrators when ECE gate passes)
curl -X POST localhost:8000/v1/tuning/calibrate \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL"}'
curl 'localhost:8000/v1/tuning/calibrate/latest?region_code=PH&city_code=MNL'

# Re-fit with explicit mode (still only persists calibrators — not live apply)
curl -X POST localhost:8000/v1/tuning/calibrate \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","mode":"apply"}'

# After reviewing holdout ECE: enable live apply via policy overlay, then retune
curl -X PUT localhost:8000/v1/policy/overlays \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL","overlay":{"calibration":{"mode":"apply"}}}'
curl -X POST localhost:8000/v1/tuning/run \
  -H 'Content-Type: application/json' \
  -d '{"region_code":"PH","city_code":"MNL"}'
```

Workflow: **fit → review ECE → set `policy.calibration.mode: apply` → retune thresholds** (calibrated scores shift the operating point).

After flipping apply or refreshing calibrators, re-assess orders that may be idempotency-cached: set `"force_reassess": true` on `POST /v1/assess` (bumps `assessment_generation`; see §3.1).

Optional tick: `calibration.on_tick: true` with `OCR_CONTROL_PLANE_TICK_SECONDS > 0`.

### 4.7 Recommended tuning loop (human + auto)

1. Set forecast + hardgates for the market week.  
2. Run assess at volume; let sampler fill review quota.  
3. Label tickets promptly (`POST /v1/feedback`).  
4. Review audit applies; spot-check `resolved` policy.  
5. If Downstream claws back, post clawback; expect tighter gates for the TTL.  
6. Revisit guardrail ceilings only when Product + Risk agree (absolute safety bounds).

### 4.8 Models (optional)

Bundle: `model.json` (`joblib`|`onnx`) + artifact + `feature_schema.json` + `metrics_baseline.json`.

```bash
curl -X POST localhost:8000/v1/models \
  -H 'content-type: application/json' \
  -d '{"bundle_path":"/path/to/bundle","role":"shadow"}'
curl -X POST localhost:8000/v1/models/{id}/evaluate   # may auto-start canary
```

Serving flags use **champion** (or canary cohort). Shadow scores are recorded without changing flags outside canary. Prefer promoting only when gates pass at your FP$ budget.

---

## 5. Feedback sampler details

| Mode | Behavior |
|---|---|
| Inline | After assess: disagreement → **pattern_mass** → uncertainty → score-matched bias_fp/bias_fn, until `quota * inline_soft_cap_fraction` |
| Batch | ~`learning.pattern_mass_fraction` (default 0.7) clear pattern tickets, then boundary/bias, then coverage to daily quota (±1) |

Tickets: one primary head each; unique `(order_display_id, day_key)`. Emitted to SQLite + JSONL stream.

Config under `policy.feedback` + `policy.learning` (feedback numerics also guardrailed).

---

## 6. Maintenance

### 6.0 Topology (single-writer / HA)

This service is a **feature producer**, not multi-region SaaS. Default layout:

| Store | Default | Multi-replica note |
|---|---|---|
| Assessments / feedback | SQLite or `OCR_DATABASE_URL` (Postgres) | Prefer Postgres when >1 API replica shares idempotency |
| Assess job queue | `memory` or `sqlite` (`OCR_QUEUE_BACKEND`) | Use `sqlite` on a **shared volume** (or one worker) |
| Control-plane tick | File flock (`OCR_CONTROL_PLANE_LOCK_PATH`) | One leader per shared lock volume |
| Outcomes, tickets, overlays, models, baselines, … | Per-path SQLite under `data/` | Single writer or shared disk; not cross-region |
| Risk stream | JSONL + optional `OCR_STREAM_URL` HTTP | Inject custom `StreamPublisher` for Kafka/SQS |

**Do:** one control-plane leader, Postgres for assessments when scaling API, HTTP/Kafka stream for Downstream.  
**Don't:** expect Redis/SQS HA inside this repo — keep those in the tenant adapter.

### 6.1 Data stores to back up

| Path (default) | Contents |
|---|---|
| `data/assessments.db` | Assessments, ledger, feedback labels |
| `data/policy_overlays.db` | Market overlays |
| `data/control_plane.db` | Forecast, hardgates, clawback, label metrics, audit |
| `data/label_tickets.db` | Review tickets |
| `data/driver_chains.db` | Driver cancel events |
| `data/entity_baselines.db` | Driver/user/pair baselines |
| `data/calibrators.db` | Per head×market score calibrators |
| `data/device_integrity.db` | Device integrity EWMA |
| `data/device_graph.db` | Device↔account graph edges |
| `data/chat_signals.db` | Force-cancel / chat flags |
| `data/entity_anomaly.db` | Entity anomaly feature samples |
| `data/outcomes.db` | Downstream outcomes + recoverability EWMA |
| `data/models.db` + `data/models/` | Model registry / artifacts |
| `data/*.jsonl` | Streams (risk events, label tickets) |

Treat audit + overlays as **compliance-sensitive**.

### 6.2 Routine jobs

| Cadence | Action |
|---|---|
| PR / CI | `pytest` + `python scripts/eval_holdout.py --check-floors` (floors in `docs/evals/`) |
| Continuous | Assess consumer / API |
| Daily | `POST /v1/feedback/sample` (or enable control-plane tick) |
| Daily / after labels | Confirm metrics + audit; investigate reject spikes |
| Weekly | Refresh supply forecast rows for each market |
| On clawback | `POST /v1/enforcement/clawback` |
| On model train | Sideload shadow → evaluate → canary → promote |

CLIs:

```bash
python scripts/compute_label_metrics.py
python scripts/run_tuner.py --region-code PH --city-code MNL
python scripts/sample_feedback_tickets.py --region-code PH --city-code MNL
python scripts/backtest.py
```

### 6.3 Monitoring (minimum)

- Assess job failure rate / latency  
- `gps_unavailable` / `gps_sparse` reason rates  
- Label ticket open backlog vs `daily_review_quota`  
- Tuner `apply` vs `reject` reasons (`insufficient_labels`, `no_candidate_in_gates`, `holdout_f1_lift_below_min`, `breaches_*_cap`)  
- Canary abort / promote counts  
- Disk growth of SQLite + JSONL  

### 6.4 Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| Scores stuck mid-band | Sparse GPS / window expanded | Check `gps_window`; LBS coverage; `gps.*` policy |
| Tuner always `insufficient_labels` | Quota not labeled | Sample + close feedback; lower min only temporarily for bring-up |
| Tuner `breaches_hour_cap` | Hardgate too tight vs flag volume | Raise hardgate with local ops, or accept fewer flags |
| Overlay `400` | Outside guardrails | Widen guardrails deliberately, or clamp FE |
| Double tickets | — | Unique index prevents; check day boundary UTC |
| Stale bias sampling | — | Hints refresh per assess job from latest metrics |
| Auth `401` on control routes | `OCR_AUTH_REQUIRED` | Send `Authorization: Bearer …` or `X-Api-Key` |

### 6.5 Safe change checklist

1. Change defaults in YAML or overlays only within guardrails.  
2. Prefer city overlay experiments before global default edits.  
3. Run `pytest` after policy/code changes.  
4. Shadow new models before canary; never skip gates for convenience in prod.  
5. Keep Downstream clawback wired—tuner alone cannot protect supply.

### 6.6 Multi-replica note

Control-plane ticks/debounced flushes take a **file flock** on `OCR_CONTROL_PLANE_LOCK_PATH` (default: sibling of the control-plane SQLite). Share that path (and `data/`) across replicas so only one leader tunes/samples.

For assess workers across processes, set `OCR_QUEUE_BACKEND=sqlite` and share `OCR_ASSESS_QUEUE_PATH`. In-memory queue remains the demo default.

Prod bootstrap:

```bash
OCR_PROFILE=prod OCR_API_KEYS=... OCR_QUEUE_BACKEND=sqlite \
  uvicorn offline_cancel_risk.main:app --host 0.0.0.0 --port 8000
```

---

## 7. API quick reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | Liveness |
| GET | `/v1/ready` | Readiness (auth/GPS config); 503 if not ready |
| GET | `/v1/ops/presence-fill` | Downstream place/vehicle fill-rate |
| GET | `/v1/ops/downstream-fill` | Downstream chat/device_risk fill-rate |
| GET | `/v1/ops/dwell-factor-nudges` | Suggested place_factor overlays from labeled FP/FN |
| POST | `/v1/assess` | Enqueue assess |
| POST | `/v1/assess:batch` | Batch enqueue |
| GET | `/v1/assess/{job_id}` | Job status / result |
| GET | `/v1/orders/{id}/latest` | Latest assessment |
| GET | `/v1/orders/{id}/generations` | Generation history |
| POST | `/v1/feedback` | Upsert labels; close tickets; debounce tune |
| GET | `/v1/feedback/tickets` | List tickets |
| POST | `/v1/feedback/sample` | Batch fill quota |
| GET/PUT | `/v1/policy/*` | Guardrails, overlays, resolved |
| PUT/GET | `/v1/supply/forecast` | S&D forecast |
| PUT/GET | `/v1/enforcement/hardgates` | Volume caps |
| POST | `/v1/enforcement/clawback` | Clawback signal |
| POST | `/v1/outcomes` | Ingest Downstream enforcement outcome; EWMA recoverability |
| GET | `/v1/outcomes/recoverability` | Per-market learned recoverability by head |
| GET | `/v1/outcomes` | List recent outcome events (ops debug) |
| POST | `/v1/marketplace/events` | Accept/complete/cancel funnel for marketplace metrics |
| GET | `/v1/devices/{device_id}` | Device integrity EWMA / last flags |
| POST | `/v1/device-graph/edges` | Device↔driver/user identity edges |
| GET | `/v1/device-graph/{device_id}` | Device graph counts / signals |
| POST | `/v1/chat-signals` | Force-cancel / persuasion flags (structured) |
| GET | `/v1/chat-signals/{order_display_id}` | Stored chat signals for an order |
| GET | `/v1/anomalies/{entity_key}` | Recent anomaly feature samples (`driver:1`) |
| GET | `/v1/baselines` | Entity baselines (`updated_since` cursor) |
| GET | `/v1/baselines/{entity_key}` | All heads for `driver:1` / `user:2` / `pair:1:2` |
| GET | `/v1/metrics/labels` | P/R/F1 snapshots |
| POST | `/v1/tuning/run` | Metrics + tuner |
| POST | `/v1/tuning/calibrate` | Fit score calibrators (shadow by default) |
| GET | `/v1/tuning/calibrate/latest` | Latest calibration run report |
| GET | `/v1/tuning/suggestions` | Recent suggest/apply/reject |
| GET | `/v1/audit/policy` | Audit trail |
| GET/POST | `/v1/models…` | Sideload, evaluate, canary, promote |

Assess + control routes honor `_require_auth` when `OCR_AUTH_REQUIRED=1` or `OCR_PROFILE=prod`. Health stays open; ready stays open (no auth) so orchestrators can probe.

---

## 8. Design references

- [specs/2026-07-25-offline-cancel-risk-design.md](superpowers/specs/2026-07-25-offline-cancel-risk-design.md)  
- [specs/2026-07-25-phase2-control-plane-design.md](superpowers/specs/2026-07-25-phase2-control-plane-design.md)  
- [specs/2026-07-25-feedback-tuning-design.md](superpowers/specs/2026-07-25-feedback-tuning-design.md)  
- [specs/2026-07-26-feedback-sampler-design.md](superpowers/specs/2026-07-26-feedback-sampler-design.md)  
- [specs/2026-08-03-learning-objective-design.md](superpowers/specs/2026-08-03-learning-objective-design.md)  
- [specs/2026-08-03-entity-baseline-gate-design.md](superpowers/specs/2026-08-03-entity-baseline-gate-design.md)  
- [specs/2026-08-03-platform-abuse-patterns-design.md](superpowers/specs/2026-08-03-platform-abuse-patterns-design.md)  
- [specs/2026-08-03-outcome-ear-loop-design.md](superpowers/specs/2026-08-03-outcome-ear-loop-design.md)  



