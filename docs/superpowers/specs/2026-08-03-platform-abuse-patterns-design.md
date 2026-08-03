# Platform Abuse Patterns (P0+P1) — Design Spec

**Date:** 2026-08-03  
**Status:** Implemented  

**Scope:** P0+P1 from marketplace research; optional thin `device_risk` ingest  
**Non-goals:** chat NLP, auth holds, selfie, full Risk Entity Watch

## Features

1. **Cancel stage** — `pre_pickup | at_merchant | en_route | near_dropoff | unknown`
2. **Pickup-then-cancel** — stage after merchant proximity
3. **Entity cancel-rate** — rolling cancels per driver/user/pair
4. **Evidence pack** — structured top contributors on `AssessmentResult`
5. **Progress + heading (A→B not B→A)** — displacement toward target **and** GPS heading aligned with bearing to target; wrong-way heading = `wrong_direction` / no credit for progress
6. **Pair graph density** — repeat driver×user cancel counts
7. **Teleport / impossible speed** — dampen stop confidence
8. **Optional `device_risk`** on assess request

## Heading rule

For progress toward target T (pickup = first stop, or dropoff = last):

- Path progress: Δ distance(position, T) decreasing
- Heading alignment: device `heading_deg` (0–360) vs geographic bearing to T; angular error ≤ `max_heading_error_deg` counts as **toward**; error ≥ 180°−ε counts as **away**
- Flag `wrong_direction` / deny progress credit when majority of samples in window are **away** while distance is not shrinking
- If heading missing: fall back to path-only progress (reason notes `heading_unavailable`)

## Wiring

Assess computes stage, progress, integrity, updates cancel/pair stats → abuse/theft/offline features → evidence → result fields.
