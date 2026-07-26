# Feedback Sampler — Design Spec

**Date:** 2026-07-26  
**Status:** Approved  
**Project:** `~/Projects/offline-cancel-risk`  
**Slice:** A — bias / uncertainty / disagreement quota sampler  
**Depends on:** assessments table, `POST /v1/feedback`, optional `label_metrics`

## 1. Problem

Labels only arrive when someone posts feedback. There is no predetermined, bias-aware review quota. ML/tuner quality depends on stratified labels (uncertainty, rule↔ML disagreement, FP/FN hotspots), not random or “flag everything” review.

## 2. Goals

- Emit a **daily label ticket quota** (`daily_review_quota` ±1) with per-head min/max.
- **Hybrid:** inline high-priority candidates on assess + batch fill for remaining mix.
- Persist tickets in SQLite **and** append to a JSONL label-sample stream.
- Product pulls tickets via API; `POST /v1/feedback` closes matching open tickets.
- Downstream owns the review UI; this service only samples.

### Non-goals

- Investigator product UI.
- Changing assess scoring logic beyond reading scores for strata.
- Auto-labeling.

## 3. Data model — `label_tickets`

| Column | Type | Notes |
|---|---|---|
| ticket_id | TEXT PK | uuid |
| order_display_id | TEXT | |
| region_code | TEXT | |
| city_code | TEXT | |
| heads_json | TEXT | JSON list of heads to label |
| sampling_reason | TEXT | `uncertainty` \| `disagreement` \| `bias_fp` \| `bias_fn` \| `coverage` |
| strata_json | TEXT | band / head metadata |
| priority | INTEGER | higher = sooner (inline reasons > coverage) |
| status | TEXT | `open` \| `labeled` \| `expired` |
| day_key | TEXT | UTC `YYYY-MM-DD` |
| created_at | TEXT | |
| labeled_at | TEXT | nullable |

Unique soft constraint: at most one **open** ticket per `order_display_id` per `day_key`.

## 4. Stream

Path: `OCR_LABEL_TICKETS_STREAM_PATH` default `data/label_tickets.jsonl`.  
On create, append one JSON object (same fields as ticket row).

## 5. Quota policy

Extend `config/policy.default.yaml` → `feedback`:

```yaml
feedback:
  daily_review_quota: 50
  inline_soft_cap_fraction: 0.6
  per_head_min: 5
  per_head_max: 25
  uncertainty_delta: 0.1
```

Day counter = tickets with `day_key=today` and status in (`open`,`labeled`).

## 6. Sampling reasons

| Reason | Priority | Rule |
|---|---|---|
| `disagreement` | 100 | ML score present and (rule≥thr) XOR (ml≥thr) for a head |
| `uncertainty` | 80 | \|blend−thr\| ≤ `uncertainty_delta` for any head |
| `bias_fp` | 70 | Latest label_metrics for market/head has precision below operating floor proxy (or fp/(tp+fp) high) |
| `bias_fn` | 70 | High FN rate on latest metrics |
| `coverage` | 10 | Batch-only fill for under-min head or score band |

Score bands vs threshold: `low` (< thr−δ), `mid` (within δ), `high` (≥ thr+δ).

## 7. Hybrid flow

### Inline (after successful assess, before return)

If `day_count < quota * inline_soft_cap_fraction` and order not already ticketed today:

1. Evaluate disagreement → uncertainty → bias reasons (first match wins, or highest priority).
2. If match: create ticket, stream append, audit optional `ticket_create`.

### Batch — `POST /v1/feedback/sample`

Body: `{ "region_code"?, "city_code"?, "lookback_hours": 24 }`.

1. Load recent assessments without open/labeled ticket today and without feedback labels.
2. Fill remaining = `quota - day_count` (allow quota+1 max).
3. Ensure each head reaches `per_head_min` via `coverage` / priority reasons; cap `per_head_max`.
4. Return created ticket ids.

CLI: `scripts/sample_feedback_tickets.py`.

## 8. API

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/feedback/tickets` | List (`status`, `day_key`, limit) |
| POST | `/v1/feedback/sample` | Batch fill |
| POST | `/v1/feedback` | Existing; also mark open tickets for order as `labeled` |

## 9. Package layout

```
src/offline_cancel_risk/feedback/
  __init__.py
  tickets.py      # store + stream
  sampler.py      # reason eval + inline/batch
api/routes.py     # ticket routes; feedback close
pipeline/assess.py  # call inline sampler
```

## 10. Success metrics

- Inline never exceeds soft cap; batch brings total to quota ±1.
- Per-head min/max respected on batch when enough candidates exist.
- Dedupe: second assess same order same day does not create second open ticket.
- Feedback upsert flips ticket → `labeled`.
- Stream line count matches creates.
- Unit tests for reason selection + quota math; API smoke test.
