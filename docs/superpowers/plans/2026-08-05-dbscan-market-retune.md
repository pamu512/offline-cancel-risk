# DBSCAN Market Retune — Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD. Spec: `docs/superpowers/specs/2026-08-05-dbscan-market-retune-design.md`

**Goal:** Full hybrid market retune of v5 DBSCAN `clustering_radius_m` + `min_pts` from labeled pattern cohort via GPS replay cache.

**Architecture:** Assess writes tracks+request to `AssessGpsCache`. `run_dbscan_retune` grids candidates, re-assesses with FakeGpsClient, scores Precision_S/Recall_S, shadows or auto-applies overlay.

**Tech Stack:** Python 3.11+, SQLite, existing assess/tuner/overlay patterns.

## Global Constraints

- Keep v5 DBSCAN only; no new clusterer / cross-order geo  
- Default mode `shadow`; `apply` auto-writes when gates pass  
- Guardrails bound all overlay writes  

## File map

| File | Role |
|---|---|
| `features/gps_cache.py` | AssessGpsCache |
| `control_plane/dbscan_retune.py` | Grid search + apply/shadow |
| `pipeline/geometry.py` + `context.py` + `assess.py` | Wire cache write |
| `api/routes.py` + `main.py` + `settings.py` | API + DI |
| `config/policy*.yaml` | Knobs + guardrails if needed |
| `tests/test_dbscan_retune.py` | Full coverage |
| `docs/OPS.md` + `MANUAL.md` | Ops |

## Tasks

### Task 1: GPS cache + tests
- [x] Failing tests: put/get, empty skip, prune retention
- [x] Implement `AssessGpsCache`

### Task 2: Retuner core + tests
- [x] Failing tests: picks better params; shadow no overlay; apply writes; reject insufficient
- [x] Implement `run_dbscan_retune`

### Task 3: Wire assess + API + policy + docs
- [x] Geometry writes cache; routes; settings path; policy section; OPS/MANUAL
- [x] Full suite green
