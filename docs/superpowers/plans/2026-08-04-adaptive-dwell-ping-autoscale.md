# Adaptive dwell + ping autoscale + transit filter — Implementation Plan

> **For agentic workers:** Use TDD. Spec: `docs/superpowers/specs/2026-08-04-adaptive-dwell-ping-autoscale-design.md`

**Goal:** Resolve target dwell \(D\) from optional place/vehicle + policy; derive effective DBSCAN counts from median ping gap; reject transit crawls via tighter dwell radius + max run displacement.

## File map

| File | Role |
|---|---|
| `src/offline_cancel_risk/features/dwell_scale.py` | Pure resolve: \(D\), \(\tau\), effective counts |
| `src/offline_cancel_risk/features/dwell.py` | Displacement break in dwell run |
| `src/offline_cancel_risk/features/dbscan_v5.py` | Accept effective min_pts / drop_off_min_pts via policy dict |
| `src/offline_cancel_risk/pipeline/geometry.py` | Wire resolve → dwell + dbscan; evidence |
| `src/offline_cancel_risk/api/schemas.py` | Optional `place_class`, `vehicle_class` |
| `config/policy.default.yaml` + guardrails | Knobs |
| `tests/test_dwell_scale.py` | New |
| `tests/test_dwell_sequence.py` | Transit crawl case |
| `docs/OPS.md` | Short knob blurb |

## Tasks

### Task 1: `resolve_stop_presence` + tests

- [ ] Failing tests: 1s vs 30s gaps; place×vehicle; missing → 1.0; autoscale off
- [ ] Implement `dwell_scale.py`

### Task 2: Dwell transit filter

- [ ] Failing test: crawl with displacement > max → False; stationary → True
- [ ] Update `dwell_stop_mask` for `max_run_displacement_m`
- [ ] Policy `dwell.radius_m`; geometry stops overriding with sequence radius

### Task 3: Wire geometry + schema + policy

- [ ] Optional fields on AssessRequest
- [ ] geometry uses resolve; passes effective dbscan policy; evidence fields
- [ ] Defaults + guardrails + OPS line
- [ ] Full suite green
