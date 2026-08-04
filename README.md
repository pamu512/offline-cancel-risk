# offline-cancel-risk

When a logistics order is cancelled, money can still walk out the door: the trip may have been completed off-platform, cancel/reassign games may be gaming the marketplace, or food/high-value goods may have gone missing. **offline-cancel-risk** turns cancelled-order + GPS evidence into three independent, ops-tunable risk scores—so downstream systems can stop revenue leakage without a human reviewing every cancel.

Apache-2.0 toolkit: plug in your GPS and order feeds, or try the zero-network CSV demo in minutes.

## How it works

```
cancel / batch assess
        │
        ▼
 GPS window (3h→24h) → features (DBSCAN, adaptive dwell, sequence, replacement, abuse, theft)
        │
        ▼
 rule_scores + ml_scores → blend → soft scores → policy thresholds → flags
        │
        ▼
 EAR / attention_score + routing (P1/P2/P3) → stream + table
        │
        ├── feedback sampler → label tickets (quota)
        ├── labels → F1 metrics → supply-aware tuner → market overlays
        ├── outcomes → recoverability EWMA → EAR (shadow / apply)
        └── models: champion / shadow / canary
```

**Three independent heads** (soft score + 0/1 flag each):

| Head | Meaning |
|---|---|
| `cancelled_offline` | Trip likely completed off-platform (revenue leakage) |
| `cancel_abuse` | Cancel / reassign games |
| `selective_theft` | Food / high-value + next-driver “no order” |

Stop geometry normalizes across ping rates (1s–30s) and optional Downstream `place_class` / `vehicle_class`, with a traffic-crawl filter on dwell. This service is a **feature producer**. Downstream owns payout blocks, suspensions, and clawback.

## Docs

| Doc | Contents |
|---|---|
| **[docs/MANUAL.md](docs/MANUAL.md)** | **Start here** — how to use, minimum requirements, how to tune |
| [docs/OPS.md](docs/OPS.md) | Full ops: env vars, HA topology, API reference, maintenance |
| [examples/csv_demo/README.md](examples/csv_demo/README.md) | Offline CSV demo |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup / PRs |
| `docs/superpowers/specs/` | Design specs |

## Who it’s for

| Audience | Why |
|---|---|
| Ops / logistics | Per-market thresholds within guardrails; prioritize by $ at risk |
| Investigators | Reason codes + evidence for rare disputes |
| Platform / data eng | Async API or library job; stream + table consumers |
| Any delivery team | Clone / pip install; CSV demo needs no network |

**Not for:** realtime cancel-path blocking, owning LBS GPS, or running enforcement inside this service.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m examples.csv_demo
```

**Minimum:** Python **3.11+**, writable working directory. See [MANUAL §2](docs/MANUAL.md#2-minimum-requirements).

Optional API (sync mode for local demos):

```bash
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --reload
curl localhost:8000/v1/health
curl localhost:8000/v1/ready
```

Labeled backtest smoke:

```bash
python scripts/backtest.py
```

Holdout eval (CI floors — pattern precision vs naive baselines):

```bash
python scripts/eval_holdout.py --check-floors
```

Shared assessments store (multi-replica): set `OCR_DATABASE_URL=postgresql://…` and `pip install -e ".[pg]"`; default remains SQLite.

Prod profile: `OCR_PROFILE=prod` requires `OCR_API_KEYS` and `OCR_GPS_BASE_URL` (or an injected GPS client).

## Tuning (short)

1. Prefer **market overlays** (`PUT /v1/policy/overlays`) over editing global YAML.  
2. Primary lever: `thresholds.*`.  
3. GPS mix: keep `dbscan.autoscale_min_pts`; clamp gaps with `dwell.gap_seconds_min/max`.  
4. Labels → feedback → tuner; outcomes → EAR shadow before `apply`.  
5. ML: shadow → promote gates → canary → champion.

Details: [MANUAL §4](docs/MANUAL.md#4-how-to-tune-rules-first-then-ml) and [OPS §4](docs/OPS.md#4-tuning-manual).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
