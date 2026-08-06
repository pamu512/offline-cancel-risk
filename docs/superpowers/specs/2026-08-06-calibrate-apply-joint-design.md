# Joint Calibration Apply + Threshold Retune — Design Spec

**Date:** 2026-08-06  
**Status:** Approved  
**Project:** offline-cancel-risk  
**Depends on:** score calibration (`policy.calibration`, `CalibratorStore`, `POST /v1/tuning/calibrate`), threshold tuner (`run_tuner` / `POST /v1/tuning/run`)

## 1. Problem

Flipping `calibration.mode` to `apply` shifts the live score channel (calibrated \(p\) × baseline discount) while thresholds \(\tau\) were fit on the previous channel. OPS documents “retune after apply,” but nothing enforces the pairing. Operators can `PUT /v1/policy/overlays` with `{calibration:{mode:apply}}` and leave Precision_S operating point wrong.

Additionally, the tuner prefers `scores_raw` via `resolve_scores`, so a naive “flip then `/tuning/run`” still searches the wrong channel unless assessments are re-scored under apply first.

## 2. Goals

- One **atomic** control-plane action that: projects calibrated scores → runs threshold search → on search-ok writes `calibration.mode: apply` (and any accepted \(\tau\) overlays).
- **Soft success:** search completed under projected scores is enough; zero heads changing \(\tau\) (no-lift / already optimal) still allows mode apply.
- **All-or-nothing on mode:** if search cannot run, do **not** write `mode: apply`.
- Return `force_reassess_required: true` so live cached assessments catch up (projection does not rewrite historical rows).
- **Harden** `PUT /v1/policy/overlays` so a shadow→apply flip is rejected unless an explicit escape hatch is set.

### Non-goals

- Changing calibration fit math (Platt/isotonic, ECE, Brier)
- Auto `force_reassess` of historical orders
- Blocking base YAML edits (`policy.default.yaml`)
- Changing EAR / DBSCAN apply paths
- Claiming sampler labels equal population probabilities

## 3. Architecture

```
POST /v1/tuning/calibrate/apply
  calibrators + assessments
       → project applied scores onto assess copies
       → threshold search on copies with overlay writes deferred
       → search-ok? single deep_merge: {calibration:{mode:apply}} + accepted τ
       → audit + report {force_reassess_required: true}

PUT /v1/policy/overlays
  if overlay flips calibration.mode → apply and prior ≠ apply
    and allow_calib_apply_without_retune is false → 400
```

| Piece | Role |
|---|---|
| `run_calibrate_apply` (control plane) | Orchestrator: project → tune (defer writes) → one overlay commit |
| Score projection helper | `predict_calibrated` + `apply_calibrated_score` per head with calibrator |
| Tuner defer flag | `run_tuner(..., defer_overlay_writes=True)` (or equivalent) returns accepted overlays without `save_overlay` |
| `POST /v1/tuning/calibrate/apply` | Ops entrypoint |
| Overlay ingest gate | Reject bare apply flips |

**Atomicity:** Do not call today’s `run_tuner` as-is for the happy path — it `save_overlay`s per accepted head. Joint apply must search first, then one `deep_merge` of mode + suggested thresholds so a failed mode write cannot leave τ applied under shadow from a projected search.

## 4. Score projection

For each assessment copy and each head with a calibrator row:

1. \(p = \mathrm{predict}(\mathrm{calib}, \texttt{scores\_raw}[h])\).
2. Recover **pre-calibration post-baseline** score:
   - If resolved `calibration.mode` ≠ `apply`: use published `scores[h]` (shadow/off path).
   - If already `apply`: prefer `scores[h] / p_{\mathrm{meta}}` when `calibration_meta` is available on the row; else fall back to `scores_raw[h]` (discount = 1).
3. `applied = apply_calibrated_score(p, scores_raw[h], pre_calib)`.
4. Write `applied` into **both** `scores` and `scores_raw` on the copy so `resolve_scores` (which prefers `scores_raw`) searches the live apply flag channel.

Projection is in-memory only; assessment store is not updated. Response always advises reassess when mode or \(\tau\) may have changed for live traffic.

## 5. Success / failure

**Search-ok (single commit of `mode: apply` ± \(\tau\)):**

- At least one calibrator row exists for the market.
- Threshold search runs to completion under projected scores (same label/holdout gates as normal tuner “can search” path).
- No-lift / cooldown / per-head reject that does not prevent search completion does **not** block mode apply (those heads simply omit \(\tau\) from the merged overlay).
- Final `deep_merge` (mode ± accepted \(\tau\)) passes guardrails; on guardrail failure write nothing.

**Reject (leave prior overlay untouched):**

- `no_calibrators`
- Insufficient labels / empty holdout / other “search cannot run” failures
- Guardrail error on the combined overlay write

Idempotency: market already on `apply` → still run projected retune; mode merge is a no-op; return `force_reassess_required: true` if any \(\tau\) accepted.

## 6. API

### `POST /v1/tuning/calibrate/apply`

```json
{ "region_code": "PH", "city_code": "MNL" }
```

Response (shape illustrative):

```json
{
  "decision": "applied",
  "reason": "search_ok",
  "force_reassess_required": true,
  "decisions": [ /* per-head tuner decisions */ ],
  "overlay": { "calibration": { "mode": "apply" }, "thresholds": { } }
}
```

Rejected: `"decision": "rejected"`, `"reason": "<code>"`, overlay unchanged for mode.

### `PUT /v1/policy/overlays`

Extend ingest request:

```json
{
  "region_code": "PH",
  "city_code": "MNL",
  "overlay": { "calibration": { "mode": "apply" } },
  "allow_calib_apply_without_retune": false
}
```

- Transition to `apply` from non-apply without escape → **400**.
- Escape `allow_calib_apply_without_retune: true` → allow (break-glass).
- `apply`→`apply`, or setting `shadow`/`off`, allowed without escape.

## 7. Audit & docs

- Audit actor: `calibrate_apply` (action `apply` / `reject`).
- OPS §4.6b: joint endpoint is the supported apply path; document escape hatch.
- MANUAL: one-line pointer to joint apply.

## 8. Tests

1. Shadow + calibrators → apply endpoint writes mode; `force_reassess_required` true.
2. No calibrators → rejected; overlay mode unchanged.
3. PUT apply flip without escape → 400.
4. PUT with escape → 200.
5. Projection: tuner search channel differs from raw-only when \(p \neq\) raw (assert projected scores used).

## 9. Rollout

Land behind existing shadow default. No policy default change required (`calibration.mode` stays `shadow` until joint apply succeeds for a market).
