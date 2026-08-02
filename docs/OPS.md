# Ops & engineering manual

How to run, tune, use, and maintain **offline-cancel-risk** in production-shaped deployments.

Audience: ops/platform engineers integrating the service, and product teams wiring a tuning UI. For a one-page overview, see the [README](../README.md).

---

## 1. System map

| Piece | Role |
|---|---|
| Assess pipeline | Cancel + GPS → scores, flags, EAR, routing, reasons |
| Publishers | JSONL risk stream + SQLite assessments / feedback |
| Policy | Default YAML + region/city overlays inside guardrails |
| Feedback sampler | Daily label-ticket quota (inline + batch) |
| Control plane | Label F1, supply operating point, hardgates, tuner, audit |
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

Health: `GET /v1/health` → `{"status":"ok"}`.

### 2.3 Important environment variables (`OCR_*`)

| Variable | Default (conceptually) | Purpose |
|---|---|---|
| `OCR_SYNC_ASSESS` | `false` | `true` = assess in request path (dev) |
| `OCR_AUTH_REQUIRED` | `false` | Require API key / bearer on control routes |
| `OCR_API_KEYS` | empty | Comma-separated keys when auth on |
| `OCR_GPS_BASE_URL` / `OCR_GPS_API_KEY` | empty | HTTP GPS adapter; empty → fake/empty GPS |
| `OCR_POLICY_PATH` | `config/policy.default.yaml` | Base policy |
| `OCR_POLICY_GUARDRAILS_PATH` | `config/policy_guardrails.default.yaml` | Overlay bounds |
| `OCR_POLICY_OVERLAYS_PATH` | `data/policy_overlays.db` | Market overlays |
| `OCR_SQLITE_PATH` | `data/assessments.db` | Assessments + feedback |
| `OCR_STREAM_PATH` | `data/risk_events.jsonl` | Risk event stream |
| `OCR_CONTROL_PLANE_SQLITE_PATH` | `data/control_plane.db` | Forecast, hardgates, metrics, audit |
| `OCR_OPERATING_POINT_PATH` | `config/operating_point.default.yaml` | Peak/surplus P/R bands |
| `OCR_LABEL_TICKETS_PATH` | `data/label_tickets.db` | Review tickets |
| `OCR_LABEL_TICKETS_STREAM_PATH` | `data/label_tickets.jsonl` | Ticket stream |
| `OCR_DRIVER_CHAINS_PATH` | `data/driver_chains.db` | Cross-order cancel chains |
| `OCR_MODELS_*` | under `data/` | Registry, shadow metrics, canary |
| `OCR_TUNER_MIN_LABELED` | `30` | Min labels before auto-apply |
| `OCR_TUNER_COOLDOWN_MINUTES` | `60` | Min gap between applies per head/market |
| `OCR_TUNER_MIN_F1_LIFT` | `0.01` | Holdout F1 lift required to apply |
| `OCR_METRICS_DEBOUNCE_SECONDS` | `30` | Coalesce feedback → tune |
| `OCR_CONTROL_PLANE_TICK_SECONDS` | `0` | Periodic sample+tune (`0` = off) |

Persist `data/` (or your mounted volume) across deploys.

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
| `blend.*.rule_weight` / `ml_weight` | Rules vs ML | After shadow model is healthy |
| `theft.high_value_amount` / `food_categories` | Theft head sensitivity | Category mix changes |
| `abuse.near_dest_radius_m` / `chain_lookback_minutes` | Abuse geography & chains | Reassign-heavy markets |
| `routing.p1_attention_min` / `p2_*` | Review priority cutoffs | Queue capacity |
| `feedback.daily_review_quota` + per-head min/max | Label budget | Reviewer capacity |
| `ear.*` | $ attention ranking | Ops prioritization, not flags |

### 4.3 Supply-aware operating point

`config/operating_point.default.yaml` maps `supply_ratio = forecast_supply / forecast_demand`:

- **Peak / tight supply** (low ratio) → higher min precision, lower min recall  
- **Surplus** (high ratio) → higher min recall, looser precision floor  
- Mid ratios interpolate  

The tuner **must** keep labeled precision/recall inside that band (plus guardrails + hardgates).

### 4.4 Auto-tuner (when to trust it)

Trigger:

- Debounced after feedback (`OCR_METRICS_DEBOUNCE_SECONDS`)  
- `POST /v1/tuning/run` `{ "region_code","city_code" }`  
- CLI: `python scripts/run_tuner.py --region-code PH --city-code MNL`  
- Optional tick: `OCR_CONTROL_PLANE_TICK_SECONDS > 0`  

Behavior:

1. Join assessments ↔ labels → train / holdout split  
2. Grid-search thresholds (and offline blend / `p1_attention_min`)  
3. Candidate must pass operating-point P/R, hardgates, guardrails  
4. Apply overlay only if **holdout F1** lifts by ≥ `OCR_TUNER_MIN_F1_LIFT` and cooldown elapsed  

Inspect:

```bash
curl 'localhost:8000/v1/metrics/labels?region_code=PH&city_code=MNL'
curl 'localhost:8000/v1/tuning/suggestions?limit=20'
curl 'localhost:8000/v1/audit/policy?limit=50'
```

**Do not** rely on auto-tune with &lt; `tuner_min_labeled` labels per head/market—it will reject with `insufficient_labels`.

### 4.5 Recommended tuning loop (human + auto)

1. Set forecast + hardgates for the market week.  
2. Run assess at volume; let sampler fill review quota.  
3. Label tickets promptly (`POST /v1/feedback`).  
4. Review audit applies; spot-check `resolved` policy.  
5. If Downstream claws back, post clawback; expect tighter gates for the TTL.  
6. Revisit guardrail ceilings only when Product + Risk agree (absolute safety bounds).

### 4.6 Models (optional)

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
| Inline | After assess: disagreement → uncertainty → score-matched bias_fp/bias_fn, until `quota * inline_soft_cap_fraction` |
| Batch | Fills remainder to daily quota (±1), per-head min/max, coverage strata |

Tickets: one primary head each; unique `(order_display_id, day_key)`. Emitted to SQLite + JSONL stream.

Config under `policy.feedback` (also guardrailed where numeric).

---

## 6. Maintenance

### 6.1 Data stores to back up

| Path (default) | Contents |
|---|---|
| `data/assessments.db` | Assessments, ledger, feedback labels |
| `data/policy_overlays.db` | Market overlays |
| `data/control_plane.db` | Forecast, hardgates, clawback, label metrics, audit |
| `data/label_tickets.db` | Review tickets |
| `data/driver_chains.db` | Driver cancel events |
| `data/models.db` + `data/models/` | Model registry / artifacts |
| `data/*.jsonl` | Streams (risk events, label tickets) |

Treat audit + overlays as **compliance-sensitive**.

### 6.2 Routine jobs

| Cadence | Action |
|---|---|
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

Debounce / tick loops are **in-process**. Multiple API replicas will each run their own loop. For multi-replica prod, run a single “control” worker for tick/debounce or move scheduling to an external cron calling `/v1/tuning/run` and `/v1/feedback/sample`.

---

## 7. API quick reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | Liveness |
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
| GET | `/v1/metrics/labels` | P/R/F1 snapshots |
| POST | `/v1/tuning/run` | Metrics + tuner |
| GET | `/v1/tuning/suggestions` | Recent suggest/apply/reject |
| GET | `/v1/audit/policy` | Audit trail |
| GET/POST | `/v1/models…` | Sideload, evaluate, canary, promote |

Control routes honor `_require_auth` when `OCR_AUTH_REQUIRED=1`.

---

## 8. Design references

- [specs/2026-07-25-offline-cancel-risk-design.md](superpowers/specs/2026-07-25-offline-cancel-risk-design.md)  
- [specs/2026-07-25-phase2-control-plane-design.md](superpowers/specs/2026-07-25-phase2-control-plane-design.md)  
- [specs/2026-07-25-feedback-tuning-design.md](superpowers/specs/2026-07-25-feedback-tuning-design.md)  
- [specs/2026-07-26-feedback-sampler-design.md](superpowers/specs/2026-07-26-feedback-sampler-design.md)  
