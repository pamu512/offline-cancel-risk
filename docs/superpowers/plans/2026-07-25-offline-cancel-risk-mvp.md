# Offline Cancel Risk MVP (Phase 0–1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an async assess microservice that scores cancelled orders for `cancelled_offline`, `cancel_abuse`, and `selective_theft` using deterministic rules (v5 DBSCAN + dwell/sequence + lineage/replacement + abuse/theft features), writes stream+table outputs with ledger audit fields, and supports batch backfill — without ML, twin, or enforcement.

**Architecture:** FastAPI accepts assess/batch/feedback/health; an in-process worker runs the pipeline (GPS adapter → features → rule scores → policy/EAR → publishers). GPS talks to an existing LBS API behind a swappable client (fake in tests). SQLite holds assessment table + ledger; an in-memory/file event log stands in for Kafka until Phase 3 binding.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, httpx, numpy, scikit-learn, pandas, PyYAML, pytest, pytest-asyncio, uvicorn, sqlite3

**Spec:** `docs/superpowers/specs/2026-07-25-offline-cancel-risk-design.md`

**Out of this plan (follow-on plans):** Phase 2 ML/calibration/shadow/feedback sampler intelligence; Phase 3 spoof/market/simulator/feature-store/multi-tenant/Kafka binding; Phase 4 twin/entity-graph/multi-signal/causal/foundation/case-packs.

## Global Constraints

- Async assessment only — never block a cancel path for realtime scoring.
- No enforcement actions (no payout/suspend calls).
- Three risk heads are independent (theft must not imply offline).
- Soft scores are source of truth; flags come from versioned policy thresholds.
- GPS via existing LBS adapter interface; adaptive window 3h→24h on sparse points or gaps.
- Every successful assess writes identical schema to stream + table; idempotent on `(order_display_id, policy_hash, model_version, assessment_generation)`.
- MVP `model_version` is always `"none"`; `ml_scores` are null/omitted; blend = rule scores.
- Seed v5 constants in config: `MIN_PTS=7`, radii 150/400/800, discounts 0.6/0.2, confidence 0.75, clustering radius 50m.
- TDD: failing test → implement → pass → commit per task.
- Frequent small commits; do not skip hooks.

## File structure (MVP)

```text
offline-cancel-risk/
  pyproject.toml
  README.md
  config/policy.default.yaml
  app/
    __init__.py
    main.py                 # FastAPI app factory
    settings.py             # env + paths
    api/
      routes.py             # HTTP endpoints
      schemas.py            # request/response Pydantic models
    domain/
      models.py             # internal dataclasses / Typed structures
    gps/
      client.py             # LBS protocol + HttpGpsClient + FakeGpsClient
      window.py             # 3h→24h expand logic
    features/
      geo.py                # haversine, parse_latlong
      dbscan_v5.py          # stop confidence port
      dwell.py              # dwell/speed stop proof
      sequence.py           # visit order match
      replacement.py        # OR validity paths
      abuse.py
      theft.py
      lineage.py            # cancel→reassign→replace chain summary
    scoring/
      rules.py              # rule_scores + reasons
      blend.py              # rule-only blend for MVP
      policy.py             # flags + policy_hash
      ear.py                # expected revenue at risk + attention
    pipeline/
      assess.py             # orchestrates one assessment
      idempotency.py
    publishers/
      stream.py             # file/memory event log
      table.py              # sqlite assessments + ledger
    worker/
      queue.py              # in-process asyncio queue consumer
  tests/
    conftest.py
    test_geo.py
    test_dbscan_v5.py
    test_dwell_sequence.py
    test_replacement.py
    test_abuse_theft.py
    test_rules_policy_ear.py
    test_gps_window.py
    test_pipeline.py
    test_api.py
  scripts/
    backtest.py
```

---

### Task 1: Project scaffold + policy config

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `config/policy.default.yaml`
- Create: `app/__init__.py`
- Create: `app/settings.py`
- Create: `tests/conftest.py`
- Test: `tests/test_settings_policy.py`

**Interfaces:**
- Consumes: none
- Produces: `app.settings.get_settings() -> Settings`; `load_policy(path) -> dict`; package installable via `pip install -e ".[dev]"`

- [ ] **Step 1: Write failing test for policy load**

```python
# tests/test_settings_policy.py
from pathlib import Path
from app.settings import load_policy, get_settings

def test_load_policy_has_v5_seeds():
    policy = load_policy(Path("config/policy.default.yaml"))
    assert policy["dbscan"]["min_pts"] == 7
    assert policy["dbscan"]["immediate_dp_radius"] == 150
    assert policy["dbscan"]["confidence_threshold"] == 0.75
    assert policy["gps"]["min_window_h"] == 3
    assert policy["gps"]["max_window_h"] == 24

def test_settings_policy_path_exists():
    s = get_settings()
    assert Path(s.policy_path).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/pamu/Projects/offline-cancel-risk && python -m pytest tests/test_settings_policy.py -v`  
Expected: FAIL (package/module not found)

- [ ] **Step 3: Create scaffold files**

`pyproject.toml`:

```toml
[project]
name = "offline-cancel-risk"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.32.0",
  "pydantic>=2.9.0",
  "pydantic-settings>=2.6.0",
  "httpx>=0.27.0",
  "numpy>=1.26.0",
  "scikit-learn>=1.5.0",
  "pandas>=2.2.0",
  "pyyaml>=6.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "pytest-asyncio>=0.24.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
```

`config/policy.default.yaml`:

```yaml
dbscan:
  min_pts: 7
  confidence_threshold: 0.75
  clustering_radius_m: 50
  immediate_dp_radius: 150
  standard_dp_radius: 400
  extended_dp_radius: 800
  standard_discount: 0.6
  extended_discount: 0.2
  drop_off_min_pts: 30
  round_trip_gap_seconds: 3600
dwell:
  min_dwell_seconds: 120
  max_speed_mps: 1.5
sequence:
  stop_match_radius_m: 150
gps:
  min_window_h: 3
  max_window_h: 24
  min_points: 20
  max_gap_minutes: 45
replacement:
  max_place_delay_minutes: 180
  route_similarity_min: 0.7
theft:
  high_value_amount: 500
  food_categories: ["FOOD", "FOOD_DELIVERY"]
abuse:
  multi_cancel_window_minutes: 120
  near_dest_radius_m: 400
blend:
  cancelled_offline: {rule_weight: 1.0, ml_weight: 0.0}
  cancel_abuse: {rule_weight: 1.0, ml_weight: 0.0}
  selective_theft: {rule_weight: 1.0, ml_weight: 0.0}
thresholds:
  cancelled_offline: 0.75
  cancel_abuse: 0.75
  selective_theft: 0.75
ear:
  recoverability:
    cancelled_offline: 1.0
    cancel_abuse: 0.4
    selective_theft: 0.8
  attention_weights:
    cancelled_offline: 1.0
    cancel_abuse: 0.7
    selective_theft: 1.2
feedback:
  daily_review_quota: 50
```

`app/settings.py`:

```python
from functools import lru_cache
from pathlib import Path
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OCR_")
    policy_path: str = str(ROOT / "config" / "policy.default.yaml")
    sqlite_path: str = str(ROOT / "data" / "assessments.db")
    stream_path: str = str(ROOT / "data" / "risk_events.jsonl")
    gps_base_url: str = "http://localhost:9"  # overridden in real deploy
    gps_api_key: str = ""

@lru_cache
def get_settings() -> Settings:
    return Settings()

def load_policy(path: Path | str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("policy must be a mapping")
    return data
```

Also create empty `app/__init__.py`, short `README.md` stating purpose + `pip install -e ".[dev]"` + `pytest`.

- [ ] **Step 4: Install and pass tests**

Run:
```bash
cd /Users/pamu/Projects/offline-cancel-risk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/test_settings_policy.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md config/policy.default.yaml app/__init__.py app/settings.py tests/conftest.py tests/test_settings_policy.py
git commit -m "chore: scaffold project and default policy config"
```

---

### Task 2: API/domain schemas

**Files:**
- Create: `app/api/schemas.py`
- Create: `app/domain/models.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Consumes: none
- Produces: Pydantic models `AssessRequest`, `GpsPoint`, `AssessmentResult` matching spec §5.3 field names

- [ ] **Step 1: Write failing schema round-trip test**

```python
# tests/test_schemas.py
from app.api.schemas import AssessRequest, AssessmentResult

def test_assess_request_minimal():
    req = AssessRequest(
        order_display_id="ORD1",
        driver_id=1,
        cancel_ts="2024-01-01T12:00:00Z",
        assign_ts="2024-01-01T09:00:00Z",
        latlong="1.0|2.0,1.1|2.1",
        path_point_num=1,
        order_status="CANCELLED",
        category="FOOD",
        order_value=100.0,
        currency="SGD",
    )
    assert req.order_display_id == "ORD1"

def test_assessment_result_has_three_heads():
    result = AssessmentResult(
        order_display_id="ORD1",
        driver_id=1,
        scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2, "selective_theft": 0.3},
        flags={"cancelled_offline": 0, "cancel_abuse": 0, "selective_theft": 0},
        expected_revenue_at_risk={
            "cancelled_offline": 10.0,
            "cancel_abuse": 8.0,
            "selective_theft": 24.0,
            "total": 42.0,
        },
        attention_score=42.0,
        reasons=["gps_sparse"],
        rule_scores={"cancelled_offline": 0.1, "cancel_abuse": 0.2, "selective_theft": 0.3},
        ml_scores={"cancelled_offline": None, "cancel_abuse": None, "selective_theft": None},
        gps_window={"start": "x", "end": "y", "expanded": False, "point_count": 0, "max_gap_minutes": 0},
        lineage_id="LIN1",
        assessment_generation=1,
        provisional=True,
        policy_hash="abc",
        model_version="none",
        twin_version="none",
        graph_version="none",
        feature_vector_ref="mem:1",
        assessed_at="2024-01-01T13:00:00Z",
    )
    assert set(result.scores) == {"cancelled_offline", "cancel_abuse", "selective_theft"}
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_schemas.py -v`  
Expected: FAIL import error

- [ ] **Step 3: Implement schemas**

Implement `AssessRequest` with optional fields: `replacement_order_id`, `replacement_placed_at`, `replacement_latlong`, `replacement_status`, `reassign_cancel_events: list[dict]`, `next_driver_no_order: bool | None`, `user_id`, `merchant_id`, `device_id`.  
Implement `AssessmentResult` exactly as in the test. Put shared GPS point model in `app/domain/models.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class GpsPoint:
    lat: float
    lon: float
    ts: str  # ISO-8601 or "%Y-%m-%d %H:%M:%S"
    speed_mps: float | None = None
```

- [ ] **Step 4: Pass tests + commit**

```bash
pytest tests/test_schemas.py -v
git add app/api/schemas.py app/domain/models.py tests/test_schemas.py
git commit -m "feat: add assess request and result schemas"
```

---

### Task 3: Geo helpers

**Files:**
- Create: `app/features/geo.py`
- Test: `tests/test_geo.py`

**Interfaces:**
- Produces: `haversine(lat1, lon1, lat2, lon2) -> float` meters; `parse_latlong(s: str) -> list[tuple[float,float]]`

- [ ] **Step 1: Failing tests**

```python
# tests/test_geo.py
from app.features.geo import haversine, parse_latlong

def test_haversine_zero():
    assert haversine(1.0, 2.0, 1.0, 2.0) == 0.0

def test_haversine_known_distance_approx():
    # ~111.2km per degree latitude
    d = haversine(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_500

def test_parse_latlong():
    assert parse_latlong("1.0|2.0,3.0|4.0") == [(1.0, 2.0), (3.0, 4.0)]
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement (port from notebook)**

```python
# app/features/geo.py
import numpy as np

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    )
    return float(r * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a)) * 1000)

def parse_latlong(latlong_str: str) -> list[tuple[float, float]]:
    if not latlong_str or not latlong_str.strip():
        return []
    out: list[tuple[float, float]] = []
    for point in latlong_str.split(","):
        lat_s, lon_s = point.split("|")
        out.append((float(lat_s), float(lon_s)))
    return out
```

- [ ] **Step 4: Pass + commit**

```bash
pytest tests/test_geo.py -v
git add app/features/geo.py tests/test_geo.py
git commit -m "feat: add haversine and latlong parsing"
```

---

### Task 4: DBSCAN v5 stop confidence

**Files:**
- Create: `app/features/dbscan_v5.py`
- Test: `tests/test_dbscan_v5.py`

**Interfaces:**
- Consumes: `haversine`, policy `dbscan` dict, `list[GpsPoint]`, stops `list[tuple[float,float]]`
- Produces: `compute_stop_confidences(points, stops, policy) -> dict` with keys `confidence_list: list[float]`, `unique_clusters: int`, `is_round_trip: bool`, `pk_visited_times: int`, `final_confidence: float`

- [ ] **Step 1: Failing test — empty GPS**

```python
from app.features.dbscan_v5 import compute_stop_confidences
from app.settings import load_policy
from pathlib import Path

POLICY = load_policy(Path("config/policy.default.yaml"))["dbscan"]

def test_empty_points_zero_confidence():
    r = compute_stop_confidences([], [(1.0, 2.0), (1.1, 2.1)], POLICY)
    assert r["confidence_list"] == []
    assert r["final_confidence"] == 0.0
    assert r["unique_clusters"] == 0
```

- [ ] **Step 2: Failing test — dwell cluster near stop beats pass-through**

Build ~40 points tightly clustered within 30m of stop A (same lat/lon jitter 0.0001) with timestamps 10s apart, plus a line of moving points that pass within 200m of stop B once. Assert confidence for stop A > 0.75 and stop B < 0.3.

- [ ] **Step 3: Run — FAIL**

- [ ] **Step 4: Implement port of notebook `process_order` clustering/confidence logic** into `compute_stop_confidences`:

- DBSCAN `eps=clustering_radius_m/6371000`, `min_samples=min_pts`, `metric='haversine'` on radians coords  
- Per-stop immediate/standard/extended counts and stop-in-cluster ratios with discounts  
- Round-trip: if first==last stop, count visits with `round_trip_gap_seconds`; if `visited_times==1`, halve last confidence  
- `final_confidence = mean(confidence_list)` when non-empty else 0  

Handle empty points without crashing (return empty list / zeros).

- [ ] **Step 5: Pass + commit**

```bash
pytest tests/test_dbscan_v5.py -v
git add app/features/dbscan_v5.py tests/test_dbscan_v5.py
git commit -m "feat: port v5 DBSCAN stop confidence scoring"
```

---

### Task 5: Dwell/speed + sequence features

**Files:**
- Create: `app/features/dwell.py`
- Create: `app/features/sequence.py`
- Test: `tests/test_dwell_sequence.py`

**Interfaces:**
- Produces: `dwell_stop_mask(points, stop, policy) -> bool`; `sequence_match_score(points, stops, policy) -> float` in `[0,1]`

- [ ] **Step 1: Failing tests**

```python
from app.domain.models import GpsPoint
from app.features.dwell import dwell_stop_mask
from app.features.sequence import sequence_match_score

def test_dwell_requires_low_speed_duration():
    stop = (1.0, 2.0)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:01:00", 0.2),
        GpsPoint(1.0, 2.0, "2024-01-01 10:02:30", 0.2),
    ]
    policy = {"min_dwell_seconds": 120, "max_speed_mps": 1.5, "radius_m": 150}
    assert dwell_stop_mask(points, stop, policy) is True

def test_pass_through_not_dwell():
    stop = (1.0, 2.0)
    points = [
        GpsPoint(1.0, 2.0, "2024-01-01 10:00:00", 12.0),
        GpsPoint(1.001, 2.0, "2024-01-01 10:00:20", 12.0),
    ]
    policy = {"min_dwell_seconds": 120, "max_speed_mps": 1.5, "radius_m": 150}
    assert dwell_stop_mask(points, stop, policy) is False

def test_sequence_score_full_order():
    # points visit stop0 then stop1 in time order inside radius
    ...
    assert sequence_match_score(points, stops, {"stop_match_radius_m": 150}) == 1.0
```

Fill the sequence test with two stops and timestamps proving ordered visits.

- [ ] **Step 2: FAIL → implement**

`dwell_stop_mask`: among points within `radius_m`, if any contiguous low-speed run lasts `>= min_dwell_seconds`, True.  
`sequence_match_score`: greedy time-ordered match of stops; score = matched_stops / len(stops).

- [ ] **Step 3: Pass + commit**

```bash
pytest tests/test_dwell_sequence.py -v
git add app/features/dwell.py app/features/sequence.py tests/test_dwell_sequence.py
git commit -m "feat: add dwell/speed and sequence match features"
```

---

### Task 6: Replacement + lineage

**Files:**
- Create: `app/features/replacement.py`
- Create: `app/features/lineage.py`
- Test: `tests/test_replacement.py`

**Interfaces:**
- Produces: `evaluate_replacement(...) -> ReplacementVerdict` with `valid: bool`, `paths_passed: list[str]`, `reason_codes: list[str]`; `build_lineage_id(order_display_id, events) -> str`; `summarize_lineage(events) -> dict`

- [ ] **Step 1: Failing tests for OR validity**

```python
from app.features.replacement import evaluate_replacement

def test_valid_via_gps_path_only():
    v = evaluate_replacement(
        original_reached_destination=False,
        replacement_placed_delay_minutes=9999,
        route_similarity=0.0,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is True
    assert "gps" in v.paths_passed

def test_valid_via_timing_only():
    v = evaluate_replacement(
        original_reached_destination=True,
        replacement_placed_delay_minutes=30,
        route_similarity=0.0,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is True
    assert "timing" in v.paths_passed

def test_invalid_replacement_all_paths_fail():
    v = evaluate_replacement(
        original_reached_destination=True,
        replacement_placed_delay_minutes=9999,
        route_similarity=0.1,
        has_replacement=True,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is False
    assert "invalid_replacement" in v.reason_codes

def test_no_replacement_reason():
    v = evaluate_replacement(
        original_reached_destination=False,
        replacement_placed_delay_minutes=None,
        route_similarity=None,
        has_replacement=False,
        policy={"max_place_delay_minutes": 180, "route_similarity_min": 0.7},
    )
    assert v.valid is False
    assert "no_replacement" in v.reason_codes
```

- [ ] **Step 2: Implement dataclass + logic; lineage helper hashes order id + sorted event timestamps**

- [ ] **Step 3: Pass + commit**

```bash
pytest tests/test_replacement.py -v
git add app/features/replacement.py app/features/lineage.py tests/test_replacement.py
git commit -m "feat: add replacement OR validity and lineage helpers"
```

---

### Task 7: Abuse + theft rule features

**Files:**
- Create: `app/features/abuse.py`
- Create: `app/features/theft.py`
- Test: `tests/test_abuse_theft.py`

**Interfaces:**
- Produces: `abuse_feature_score(ctx, policy) -> tuple[float, list[str]]`; `theft_feature_score(ctx, policy) -> tuple[float, list[str]]`

- [ ] **Step 1: Failing tests**

```python
def test_abuse_order_still_active():
    score, reasons = abuse_feature_score(
        {"order_still_active": True, "cancel_event_count": 1, "driver_chain_count": 1, "cancel_near_destination": False},
        {"multi_cancel_window_minutes": 120},
    )
    assert score >= 0.5
    assert "order_still_active_after_driver_cancel" in reasons

def test_abuse_ignores_next_driver_no_order():
    score_a, _ = abuse_feature_score(
        {"order_still_active": False, "cancel_event_count": 1, "driver_chain_count": 1, "cancel_near_destination": False, "next_driver_no_order": True},
        {"multi_cancel_window_minutes": 120},
    )
    score_b, _ = abuse_feature_score(
        {"order_still_active": False, "cancel_event_count": 1, "driver_chain_count": 1, "cancel_near_destination": False, "next_driver_no_order": False},
        {"multi_cancel_window_minutes": 120},
    )
    assert score_a == score_b

def test_theft_food_high_value_no_order():
    score, reasons = theft_feature_score(
        {"category": "FOOD", "order_value": 800, "next_driver_no_order": True},
        {"high_value_amount": 500, "food_categories": ["FOOD", "FOOD_DELIVERY"]},
    )
    assert score >= 0.75
    assert "food_category" in reasons
    assert "high_value" in reasons
    assert "next_driver_no_order" in reasons

def test_theft_independent_of_offline_inputs():
    # no GPS/offline fields required
    score, _ = theft_feature_score(
        {"category": "HAUL", "order_value": 10, "next_driver_no_order": False},
        {"high_value_amount": 500, "food_categories": ["FOOD"]},
    )
    assert score == 0.0
```

Scoring guidance (keep deterministic & simple):

- Abuse: start 0; +0.5 still active; +0.25 if `cancel_event_count >= 3`; +0.25 if `driver_chain_count >= 3`; +0.25 if cancel near dest; clip to 1.
- Theft: +0.34 food; +0.33 high value; +0.33 next_driver_no_order; clip to 1.

- [ ] **Step 2: Implement + pass + commit**

```bash
pytest tests/test_abuse_theft.py -v
git add app/features/abuse.py app/features/theft.py tests/test_abuse_theft.py
git commit -m "feat: add cancel-abuse and selective-theft rule features"
```

---

### Task 8: Rules + blend + policy + EAR

**Files:**
- Create: `app/scoring/rules.py`
- Create: `app/scoring/blend.py`
- Create: `app/scoring/policy.py`
- Create: `app/scoring/ear.py`
- Test: `tests/test_rules_policy_ear.py`

**Interfaces:**
- Produces:
  - `compute_rule_scores(features: dict, policy: dict) -> tuple[dict[str,float], list[str]]`
  - `blend_scores(rule_scores, ml_scores, policy) -> dict[str,float]`
  - `policy_hash(policy: dict) -> str`
  - `apply_thresholds(scores, policy) -> dict[str,int]`
  - `compute_ear(scores, order_value, policy) -> tuple[dict, float]`  # ear dict + attention

- [ ] **Step 1: Failing tests**

```python
def test_offline_boost_on_invalid_replacement():
    features = {
        "final_stop_confidence": 0.8,
        "sequence_score": 0.8,
        "dwell_fraction": 0.8,
        "replacement_valid": False,
        "has_replacement": True,
        "abuse_score": 0.2,
        "theft_score": 0.0,
        "abuse_reasons": [],
        "theft_reasons": [],
        "replacement_reasons": ["invalid_replacement"],
    }
    scores, reasons = compute_rule_scores(features, full_policy)
    assert "invalid_replacement" in reasons
    assert scores["cancelled_offline"] >= 0.75

def test_theft_high_offline_low_independence():
    features = {
        "final_stop_confidence": 0.0,
        "sequence_score": 0.0,
        "dwell_fraction": 0.0,
        "replacement_valid": False,
        "has_replacement": False,
        "abuse_score": 0.0,
        "theft_score": 0.9,
        "abuse_reasons": [],
        "theft_reasons": ["food_category", "next_driver_no_order"],
        "replacement_reasons": ["no_replacement"],
    }
    scores, _ = compute_rule_scores(features, full_policy)
    assert scores["selective_theft"] >= 0.75
    assert scores["cancelled_offline"] < 0.5

def test_attention_uses_ear_weights():
    scores = {"cancelled_offline": 1.0, "cancel_abuse": 0.0, "selective_theft": 0.0}
    ear, attention = compute_ear(scores, order_value=100.0, policy=full_policy)
    assert ear["cancelled_offline"] == 100.0
    assert attention > 0
```

Offline rule sketch (clip 0–1):

```
base = mean(final_stop_confidence, sequence_score, dwell_fraction)
if not has_replacement: base = max(base, base*0.5 + 0.35)  # no_replacement lift
if has_replacement and not replacement_valid: base = min(1.0, base + 0.15)
cancelled_offline = base
cancel_abuse = abuse_score (+0.1 if invalid replacement and abuse_score>=0.5)
selective_theft = theft_score
```

`policy_hash`: sha256 of canonical JSON dump of policy.  
`blend_scores`: MVP ignores ml (all None) → return rule scores.  
`apply_thresholds`: per-head 0/1 from `policy["thresholds"]`.

- [ ] **Step 2: Implement + pass + commit**

```bash
pytest tests/test_rules_policy_ear.py -v
git add app/scoring/*.py tests/test_rules_policy_ear.py
git commit -m "feat: add rule scoring, policy flags, and EAR attention"
```

---

### Task 9: GPS client + adaptive window

**Files:**
- Create: `app/gps/client.py`
- Create: `app/gps/window.py`
- Test: `tests/test_gps_window.py`

**Interfaces:**
- Produces: `GpsClient` protocol with `async def fetch_track(driver_id: int, start: datetime, end: datetime) -> list[GpsPoint]`; `FakeGpsClient`; `HttpGpsClient`; `resolve_gps_window(anchor_start, anchor_end, points_loader, policy) -> WindowResult`

- [ ] **Step 1: Failing tests**

```python
import pytest
from datetime import datetime, timezone, timedelta
from app.gps.window import resolve_gps_window
from app.domain.models import GpsPoint

@pytest.mark.asyncio
async def test_expands_when_too_few_points():
    async def loader(start, end):
        # return 5 points for 3h window, 40 points when window > 12h
        hours = (end - start).total_seconds() / 3600
        n = 5 if hours <= 3.1 else 40
        return [GpsPoint(1.0, 2.0, start.isoformat(), 0.0) for _ in range(n)]

    anchor = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    result = await resolve_gps_window(
        anchor_start=anchor - timedelta(hours=1),
        anchor_end=anchor,
        fetch=loader,
        policy={"min_window_h": 3, "max_window_h": 24, "min_points": 20, "max_gap_minutes": 45},
    )
    assert result.expanded is True
    assert result.point_count >= 20

@pytest.mark.asyncio
async def test_expands_on_large_gap():
    ...
```

For gap test: return plenty of points but with a 3-hour hole in the 3h window; denser coverage when expanded.

- [ ] **Step 2: Implement window algorithm**

1. Query `[anchor_end - 3h, anchor_end + small_buffer]` (also include `anchor_start` if earlier).  
2. If `len(points) < min_points` OR `max_gap_minutes` exceeded → expand end/start symmetrically toward 24h total span; re-fetch.  
3. Stop at max window even if still sparse; set reasons later in pipeline.

`HttpGpsClient.fetch_track`: `GET {base}/v1/drivers/{id}/gps?start=&end=` with API key header; map JSON list to `GpsPoint`. Exact path may change — keep mapping in one function.

- [ ] **Step 3: Pass + commit**

```bash
pytest tests/test_gps_window.py -v
git add app/gps/client.py app/gps/window.py tests/test_gps_window.py
git commit -m "feat: add GPS client interface and adaptive windowing"
```

---

### Task 10: Assess pipeline + idempotency

**Files:**
- Create: `app/pipeline/assess.py`
- Create: `app/pipeline/idempotency.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: all feature/scoring/gps modules
- Produces: `async def assess_order(req: AssessRequest, gps_client: GpsClient, policy: dict, stores...) -> AssessmentResult`

- [ ] **Step 1: Failing integration-style unit test with FakeGpsClient**

Fixture: food order, high value, `next_driver_no_order=True`, GPS cluster at pickup only (not destination), no replacement.  
Assert: `selective_theft` flag 1 possible; `cancelled_offline` may be mid/low; `model_version=="none"`; `policy_hash` non-empty; `assessment_generation==1`.

Second call with same identity key returns same generation without duplicate side effects when store says exists.

- [ ] **Step 2: Implement orchestration**

Order of operations:

1. Compute idempotency key; return cached if present.  
2. Resolve GPS window via adapter.  
3. Parse stops; DBSCAN confidences; dwell fraction; sequence score; `original_reached_destination` from last-stop confidence/dwell.  
4. Replacement verdict; lineage id.  
5. Abuse/theft feature scores from request fields + near-dest check.  
6. `compute_rule_scores` → `blend_scores` → thresholds → EAR.  
7. Build `AssessmentResult` with `twin_version="none"`, `graph_version="lineage-v0"`, `provisional=True` if `gps_sparse` else `False` when window maxed still sparse.  
8. Persist via publishers (injected).

- [ ] **Step 3: Pass + commit**

```bash
pytest tests/test_pipeline.py -v
git add app/pipeline/assess.py app/pipeline/idempotency.py tests/test_pipeline.py
git commit -m "feat: wire end-to-end assess pipeline with idempotency"
```

---

### Task 11: Publishers (stream + sqlite table/ledger)

**Files:**
- Create: `app/publishers/stream.py`
- Create: `app/publishers/table.py`
- Test: `tests/test_publishers.py`

**Interfaces:**
- Produces: `StreamPublisher.publish(result)`; `TablePublisher.upsert(result)` + `get(order_display_id, policy_hash, model_version, generation)`; ledger row written every upsert

- [ ] **Step 1: Failing tests** — publish one result; read JSONL line; read sqlite row; upsert same key does not create second ledger identity (ledger may append audit row but assessments unique on idempotency key).

- [ ] **Step 2: Implement**

- Stream: append JSON line to `settings.stream_path` (create `data/`).  
- Table: SQLite schema:

```sql
CREATE TABLE IF NOT EXISTS assessments (
  order_display_id TEXT,
  policy_hash TEXT,
  model_version TEXT,
  assessment_generation INTEGER,
  payload_json TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  PRIMARY KEY (order_display_id, policy_hash, model_version, assessment_generation)
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_display_id TEXT,
  policy_hash TEXT,
  model_version TEXT,
  assessment_generation INTEGER,
  payload_json TEXT NOT NULL,
  written_at TEXT NOT NULL
);
```

- [ ] **Step 3: Pass + commit**

```bash
pytest tests/test_publishers.py -v
git add app/publishers/stream.py app/publishers/table.py tests/test_publishers.py
git commit -m "feat: add stream and sqlite assessment/ledger publishers"
```

---

### Task 12: Worker queue + FastAPI routes

**Files:**
- Create: `app/worker/queue.py`
- Create: `app/api/routes.py`
- Create: `app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: app with routes from spec §9 (MVP subset): assess, assess batch, get job, get latest, get generations, feedback upsert (store only), health

- [ ] **Step 1: Failing API tests with httpx ASGITransport + FakeGpsClient dependency override**

```python
from httpx import ASGITransport, AsyncClient
from app.main import create_app

@pytest.mark.asyncio
async def test_health():
    app = create_app(gps_client=FakeGpsClient([]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

@pytest.mark.asyncio
async def test_assess_enqueue_and_latest():
    ...
```

Flow: `POST /v1/assess` returns `job_id`; worker processes (run worker tick in test or sync mode flag `OCR_SYNC_ASSESS=1` for tests that completes inline); `GET /v1/orders/{id}/latest` returns result.

- [ ] **Step 2: Implement**

- `AssessJobQueue` asyncio queue; worker task started on app lifespan.  
- For tests, `Settings.sync_assess: bool = True` runs pipeline inside POST.  
- `POST /v1/feedback` stores labels in sqlite `feedback` table (`order_display_id`, labels JSON, `created_at`) — no sampler intelligence yet (Phase 2).  
- `GET /v1/orders/{id}/generations` lists generations ascending.

- [ ] **Step 3: Pass full unit/API suite + commit**

```bash
pytest tests/test_api.py tests/ -v
git add app/worker/queue.py app/api/routes.py app/main.py tests/test_api.py
git commit -m "feat: add FastAPI assess endpoints and async worker"
```

---

### Task 13: Backtest script + README usage

**Files:**
- Create: `scripts/backtest.py`
- Modify: `README.md`
- Test: `tests/test_backtest_smoke.py`

**Interfaces:**
- Produces: CLI `python scripts/backtest.py --orders path.csv --gps path.csv --out path.json` computing flag rates and optional metrics if `label` column present

- [ ] **Step 1: Smoke test with tiny CSV fixtures under `tests/fixtures/`**

- [ ] **Step 2: Implement script** that loads orders+gps, runs `assess_order` with FakeGpsClient per driver/order, writes summary JSON: counts, mean scores, confusion vs `is_offline_reviewed` if present

- [ ] **Step 3: Document runbook in README (install, pytest, uvicorn, backtest)

- [ ] **Step 4: Pass + commit**

```bash
pytest tests/test_backtest_smoke.py -v
git add scripts/backtest.py tests/fixtures tests/test_backtest_smoke.py README.md
git commit -m "feat: add backtest CLI and usage docs"
```

---

### Task 14: MVP acceptance gate

**Files:** none new required

- [ ] **Step 1: Run full suite**

```bash
pytest -v
```
Expected: all PASS

- [ ] **Step 2: Manual smoke**

```bash
uvicorn app.main:app --reload
# POST /v1/health → ok
# POST /v1/assess with sample body → job/result
```

- [ ] **Step 3: Verify constraints checklist**

- [ ] Three independent heads in output  
- [ ] `model_version` is `none`  
- [ ] Stream JSONL + sqlite row written  
- [ ] Ledger row written  
- [ ] Idempotent re-POST same payload does not bump generation  
- [ ] GPS expand behavior covered by tests  
- [ ] No enforcement client code exists (`rg -i "suspend|payout|ban" app/` → no matches)

- [ ] **Step 4: Commit any fixes; tag MVP**

```bash
git commit --allow-empty -m "chore: phase 0-1 MVP acceptance gate passed"
git tag v0.1.0-mvp
```

---

## Follow-on plans (do not implement in this plan)

1. `2026-*-offline-cancel-risk-phase2-ml.md` — models, calibration, shadow, smart sampler  
2. `2026-*-offline-cancel-risk-phase3-platform.md` — Kafka/WH binding, simulator, spoof, multi-tenant, feature store  
3. `2026-*-offline-cancel-risk-phase4-excellence.md` — twin, entity graph, multi-signal, causal replacement, foundation model, case packs  

## Spec coverage map (Phase 0–1)

| Spec area | Task |
|---|---|
| Async assess API + batch | 12 |
| GPS adaptive 3→24h | 9 |
| v5 DBSCAN + dwell/sequence | 4, 5 |
| Replacement OR + invalid lift | 6, 8 |
| Abuse A/C/D/E; theft A/B/C | 7 |
| Independent scores + flags + EAR attention | 8 |
| Stream + table + ledger | 11 |
| Idempotency + generations | 10, 12 |
| Feedback store stub | 12 |
| Backtest | 13 |
| No enforcement | 14 |
| ML/twin/graph/foundation/adversarial/simulator | Follow-on plans |

## Plan self-review notes

- Placeholders avoided; MVP explicitly stubs Phase 2+ with `model_version=none`, `twin_version=none`.  
- Types aligned: `AssessmentResult`, `GpsPoint`, policy dict keys used consistently.  
- Absolute-ceiling items deferred to named follow-on plans rather than silently dropped.
