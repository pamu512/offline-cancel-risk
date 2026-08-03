# Outcome → EAR recoverability loop (A+++ thin) — design

**Date:** 2026-08-03  
**Status:** draft for review  
**Goal:** Ingest Downstream enforcement outcomes, EWMA-update per-market recoverability, feed EAR in shadow (default) then optional apply — unlock A+++ closed loop without case packs / sampler auto-quotas.

## Locked decisions

| Decision | Choice |
|---|---|
| Scope | Thin closed loop (not full A+++) |
| Approach | Outcome store + EWMA recoverability per `(region, city, head)` |
| Default mode | `ear.mode: shadow` — static EAR unchanged; learned weights exposed in meta |
| Apply | `ear.mode: apply` uses learned recoverability (static fallback if cold) |
| Auth | `POST /v1/outcomes` requires `_require_auth` |
| Champion ML | Unchanged; this slice is recoverability/EAR only |

## Non-goals

- Case-pack investigator export
- Auto feedback-sampler quota from FP outcomes
- Auto-promote ML models from outcomes
- Replacing market clawback TTL hardgate (`POST /v1/enforcement/clawback` stays)
- NLP / chat transcript storage

## Data model

### Outcome event

| Field | Required | Notes |
|---|---|---|
| `order_display_id` | yes | Join key to latest assessment |
| `outcome` | yes | `payout_blocked` \| `clawback_won` \| `clawback_lost` \| `account_actioned` |
| `amount` | no | Recovered or blocked $; informational + optional weighting later |
| `head` | no | One of three heads; if omitted, infer from latest assessment’s highest score among flagged heads, else highest score |
| `region_code` / `city_code` | no | Default from latest assessment market |
| `occurred_at` | no | ISO ts; default now UTC |

Idempotency: `(order_display_id, outcome, occurred_at)` unique — duplicate POST is no-op success.

### Recoverability EWMA

Key: `(region_code, city_code, head)`  
State: `value` in `[0, 1]`, `n_updates`, `updated_at`.

Update:

```
signal = 1.0  if outcome in {clawback_won, payout_blocked, account_actioned}
signal = 0.0  if outcome == clawback_lost
value ← (1 - α) * value + α * signal
```

- Cold start: `value = policy.ear.recoverability[head]` (static default).
- `α` from `policy.ear.outcome_ewma_alpha` (default `0.05`).
- Clamp to guardrails `ear.recoverability.<head>` min/max.

## API

```
POST /v1/outcomes
Authorization: required when auth on / prod profile

→ { ok, order_display_id, head, region_code, city_code,
    recoverability: { head: value, ... },  # post-update for that market
    n_updates }
```

```
GET /v1/outcomes/recoverability?region_code=&city_code=
→ { region_code, city_code, heads: { cancelled_offline: {value, n_updates, updated_at}, ... } }
```

List recent outcomes (ops debug):

```
GET /v1/outcomes?order_display_id=&limit=100
```

## Assess / EAR wiring

In `compute_ear` (or thin wrapper in score_build):

1. Resolve market from request.
2. Load learned recoverability map for market (missing heads → static).
3. If `ear.mode == apply` and `n_updates >= ear.min_updates_apply` (default `5` per head used): use learned for those heads.
4. Else: use static policy recoverability for the live `expected_revenue_at_risk` / `attention_score`.
5. Always attach on result (schema extension):

```json
"ear_meta": {
  "mode": "shadow|apply",
  "recoverability_static": {...},
  "recoverability_learned": {...},
  "ear_learned": {...},
  "attention_learned": 0.0
}
```

Shadow must not change existing golden scores / flags.

## Persistence

- Path setting: `OCR_OUTCOMES_PATH` → `data/outcomes.db` (SQLite).
- Tables: `outcomes`, `recoverability_ewma`.
- Wire store on `app.state.outcomes` in `main.py`.

## Eval / CI smoke

- Unit: EWMA math, cold start, clamp, idempotent ingest, shadow does not change EAR totals.
- Tiny fixture: seed assessments + outcomes → learned differs from static; `ear_meta.ear_learned` present.
- Optional: `scripts/eval_holdout.py` or sibling reports EAR@top-k static vs learned when outcomes exist (non-blocking floor for v1).

## Policy knobs

```yaml
ear:
  mode: shadow          # shadow | apply
  outcome_ewma_alpha: 0.05
  min_updates_apply: 5
  recoverability: { ... }   # existing static defaults
  attention_weights: { ... }
```

Guardrails already bound recoverability; add bounds for `outcome_ewma_alpha` and `min_updates_apply` if missing.

## Success criteria

1. Outcomes ingest + EWMA persist across process restart.
2. Shadow mode: golden / existing assess scores unchanged; `ear_meta` populated when store present.
3. Apply mode (test-only setting): EAR uses learned weights after enough updates.
4. Auth honored on write routes.
5. OPS blurb: how Downstream posts outcomes.

## Implementation order

1. Outcome store + EWMA  
2. API routes  
3. EAR shadow/apply wiring + schema  
4. Policy defaults / guardrails  
5. Tests + OPS  

## Out of scope (later A+++ polish)

- Amount-weighted EWMA  
- Case packs  
- Sampler quota coupling  
- Postgres outcomes adapter  
