# Offline Cancel Risk — User Manual

How to run this repo, what you need at minimum, and how to tune scores / models without breaking precision.

For deep ops (env vars, HA topology, full API tables), see [OPS.md](OPS.md).

---

## 1. What this service does

On a cancelled order it produces **three independent heads**:

| Head | Question |
|---|---|
| `cancelled_offline` | Did the trip likely finish off-platform? |
| `cancel_abuse` | Cancel / reassign games? |
| `selective_theft` | Food / high-value loss pattern? |

Each head has a soft score, a 0/1 flag (threshold), reasons, and evidence. Downstream owns enforcement (payout block, suspension, clawback). This repo is a **feature producer**.

Optional Downstream fields improve stop geometry:

- `place_class` / `vehicle_class` — scale target dwell (skip → `unknown` / factor 1.0)
- `device_risk` / `chat_signals` — abuse bonuses when present
- GPS track via `OCR_GPS_BASE_URL` (or injected client)

---

## 2. Minimum requirements

### Runtime

| Requirement | Minimum |
|---|---|
| Python | **3.11+** |
| OS | macOS / Linux (Windows via WSL recommended) |
| Disk | ~500 MB for venv + `data/` (more if you train synth) |
| Network | Not required for CSV demo / unit tests |

### Install

```bash
git clone <this-repo>
cd offline-cancel-risk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### Production-shaped minimum

| Piece | Minimum |
|---|---|
| GPS | `OCR_GPS_BASE_URL` (or inject `GpsClient`). `OCR_PROFILE=prod` refuses empty Fake GPS. |
| Auth | `OCR_PROFILE=prod` + `OCR_API_KEYS` |
| Persistence | Writable `data/` volume (or `OCR_DATABASE_URL` Postgres for assessments) |
| Queue | Single process `memory`, or `OCR_QUEUE_BACKEND=sqlite` on shared disk for multi-worker |
| Downstream contract | `POST /v1/assess` with cancel + stops (`latlong`); GPS fetchable for `driver_id` |

Optional but recommended: label feedback loop, outcome ingest for EAR, HTTP stream (`OCR_STREAM_URL`).

### Sanity checks

```bash
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --reload
curl -s localhost:8000/v1/health    # liveness
curl -s localhost:8000/v1/ready     # config readiness (GPS/auth for profile)
```

---

## 3. How to use the repo

### A. Local CSV demo (no network)

```bash
python -m examples.csv_demo
```

See [examples/csv_demo/README.md](../examples/csv_demo/README.md).

### B. HTTP API (sync, local)

```bash
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --reload

curl -s -X POST localhost:8000/v1/assess \
  -H 'content-type: application/json' \
  -d '{
    "order_display_id":"ORD-1",
    "driver_id":1,
    "cancel_ts":"2024-01-01 11:20:00",
    "assign_ts":"2024-01-01 10:00:00",
    "latlong":"14.55|121.02,14.65|121.08",
    "path_point_num":2,
    "order_status":"CANCELLED",
    "category":"FOOD",
    "order_value":120.0,
    "currency":"PHP",
    "place_class":"apartment",
    "vehicle_class":"two_wheel"
  }'
# → {"job_id":"...","status":"done"}  (sync) then GET /v1/assess/{job_id}
```

### C. Library call

```python
from offline_cancel_risk.adapters.gps import FakeGpsClient
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher, SqliteTablePublisher
from offline_cancel_risk.pipeline.assess import assess_order
from offline_cancel_risk.settings import load_policy

result = await assess_order(
    req, gps_client, load_policy("config/policy.default.yaml"),
    stream=JsonlStreamPublisher("data/risk_events.jsonl"),
    table=SqliteTablePublisher("data/assessments.db"),
)
```

### D. Holdout / CI floors

```bash
python scripts/eval_holdout.py --check-floors
```

### E. Ops dashboards (after some traffic)

| Endpoint | Purpose |
|---|---|
| `GET /v1/ops/presence-fill` | Downstream place/vehicle fill-rate |
| `GET /v1/ops/downstream-fill` | chat / device_risk fill-rate |
| `GET /v1/ops/dwell-factor-nudges` | Suggested place_factor overlays from labels |
| `GET /v1/outcomes/recoverability?region_code=&city_code=` | EAR shadow vs learned |

Auth required when `OCR_AUTH_REQUIRED=1` or prod profile (except `/health` and `/ready`).

---

## 4. How to tune (rules first, then ML)

Tuning is **policy-first**. ML is optional (champion/shadow/canary). Prefer market overlays over editing global YAML in prod.

### 4.1 Layers

```
config/policy.default.yaml
        ↑
region / city overlay   ← PUT /v1/policy/overlays  (must pass guardrails)
```

```bash
curl -X PUT localhost:8000/v1/policy/overlays \
  -H 'content-type: application/json' \
  -d '{
    "region_code":"PH","city_code":"MNL",
    "overlay":{"thresholds":{"cancelled_offline":0.82}}
  }'
```

### 4.2 Knobs that move precision most

| Goal | Touch first | Notes |
|---|---|---|
| Fewer false flags | Raise `thresholds.*` | Primary lever |
| Offline path FPs near lights | `dwell.radius_m`, `max_run_displacement_m` | Traffic crawl filter |
| Mixed ping rates (1s–30s) | Keep `dbscan.autoscale_min_pts: true` | Clamp with `dwell.gap_seconds_*` |
| Apartment / truck vs bike | Send `place_class` / `vehicle_class`; tune `dwell.*_factors` | Or use nudge API |
| Abuse volume | `abuse.*`, anomaly/baselines mode | Defaults: abuse apply, theft baseline shadow |
| Review queue size | `routing.p1_attention_min`, EAR weights | Does not change flags |
| Label budget | `feedback.daily_review_quota`, `per_head_min` | SLO: ≥ per_head_min labels/head/day |

Guardrails: `config/policy_guardrails.default.yaml`. Invalid overlays → HTTP 400.

### 4.3 Closed loops (in order)

1. **Assess** → flags + tickets (sampler).
2. **Label** → `POST /v1/feedback` → metrics + optional auto-tuner (within guardrails / cooldown).
3. **Outcomes** → `POST /v1/outcomes` → recoverability EWMA; keep `ear.mode: shadow` until `ear_shadow.recommendation` says `consider_apply`.
4. **ML** → sideload shadow → promote gates (`config/promote_gates.default.yaml`) → canary → champion. Track `label_agreement` when labels exist on shadow rows.

Do **not** flip `ear.mode` / global baselines to apply on day one without market shadow review.

### 4.3a Learning objective (pattern precision)

Primary goal: **~0.98 precision on the pattern cohort** \(S\) (`policy.learning`), then maximize recall **on \(S\)**. Not global F1 and not the exotic 2% tail.

| Piece | Behavior |
|---|---|
| Sampler | ~70% `pattern_mass` tickets (`pattern_mass_fraction`) |
| Tuner | Precision-constrained threshold search on \(S\); surplus `min_recall` is soft |
| DBSCAN | Keep v5 per-trip clustering; market retune via `POST /v1/tuning/dbscan-retune` (default shadow) |
| Calibration | Follow-on after Precision_S; `POST /v1/tuning/calibrate` (shadow → apply → retune thresholds) |

See [OPS §4.4](OPS.md#44-learning-objective-pattern-precision), [OPS §4.6a](OPS.md#46a-dbscan-market-retune-eps--min_pts), and [learning-objective design](superpowers/specs/2026-08-03-learning-objective-design.md).

### 4.4 Adaptive dwell (short)

Target dwell \(D = D_{base} \cdot f_{place} \cdot f_{vehicle}\).  
DBSCAN counts scale as:

\[
min\_pts \propto min\_pts_{ref}\cdot\frac{D}{D_{base}}\cdot\frac{\tau_{ref}}{\tau}
\]

with median gap \(\tau\) clamped to `[gap_seconds_min, gap_seconds_max]` (default 1–30s).  
At \(\tau = \tau_{ref}\) and \(D = D_{base}\), counts match policy refs (7 / 30).

Disable autoscale per market with overlay `dbscan.autoscale_min_pts: false` if needed.

### 4.5 Suggested bring-up sequence

1. Demo / holdout green locally.  
2. Wire GPS + assess; check `/v1/ready`.  
3. Shadow one city: watch provisional rate, GPS sparse, fill-rates.  
4. Tune thresholds for ~target precision on labeled sample.  
5. Turn on feedback sampler; hit label SLO.  
6. Review dwell nudges / overlays; then consider EAR apply.  
7. Only then sideload / promote ML.

---

## 5. What not to put in this service

- Geofence enforcement, POD, live reassign  
- Chat NLP / GPS SDK root detection (Downstream sends flags)  
- POI inference for building type (send `place_class`)  
- Multi-region Redis/SQS HA (inject publishers; see OPS §6.0)

---

## 6. Where next

| Doc | Use when |
|---|---|
| [OPS.md](OPS.md) | Env vars, API list, HA, maintenance |
| [README.md](../README.md) | One-page overview |
| `docs/superpowers/specs/` | Design rationale |
| `config/policy.default.yaml` | Default knobs |
| `config/policy_guardrails.default.yaml` | Overlay bounds |
