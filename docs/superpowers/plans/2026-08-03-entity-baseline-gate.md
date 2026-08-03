# Entity Baseline Gate Implementation Plan

> **For agentic workers:** Implement task-by-task; verify with pytest after wiring.

**Goal:** Entity (driver/user/pair) rolling+EWMA baselines with band-limited score discount, shadow mode, scores_raw, and warehouse pull API.

**Architecture:** Pure gate logic + SQLite store; hook after blend in `assess_order`; GET `/v1/baselines` with `updated_since`.

**Tech Stack:** Python, SQLite, FastAPI, pytest

## Global Constraints

- Default `baselines.mode: shadow`
- Discount only when `baseline+ε < score < armed_thr`
- Baselines update from raw scores only
- No JSONL baseline stream

---

### Task 1: Core gate + store

**Files:** Create `src/offline_cancel_risk/baselines/{__init__,gate.py,store.py}`; Test `tests/test_entity_baselines.py`

### Task 2: Policy, schema, assess, API

**Files:** Modify policy YAML, schemas, settings, assess.py, main.py, queue.py, routes.py, metrics (prefer scores_raw), OPS.md

### Task 3: Verify

Run full pytest suite.
