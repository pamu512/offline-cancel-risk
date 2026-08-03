# offline-cancel-risk

When a logistics order is cancelled, money can still walk out the door: the trip may have been completed off-platform, cancel/reassign games may be gaming the marketplace, or food/high-value goods may have gone missing. **offline-cancel-risk** turns cancelled-order + GPS evidence into three independent, ops-tunable risk scores—so downstream systems can stop revenue leakage without a human reviewing every cancel.

Apache-2.0 toolkit: plug in your GPS and order feeds, or try the zero-network CSV demo in minutes.

## How it works

```
cancel / batch assess
        │
        ▼
 GPS window (3h→24h) → features (DBSCAN, dwell, sequence, replacement, abuse, theft)
        │
        ▼
 rule_scores + ml_scores → blend → soft scores → policy thresholds → flags
        │
        ▼
 EAR / attention_score + routing (P1/P2/P3) → stream + table
        │
        ├── feedback sampler → label tickets (quota)
        ├── labels → F1 metrics → supply-aware tuner → market overlays
        └── models: champion / shadow / canary
```

**Three independent heads** (soft score + 0/1 flag each):

| Head | Meaning |
|---|---|
| `cancelled_offline` | Trip likely completed off-platform (revenue leakage) |
| `cancel_abuse` | Cancel / reassign games |
| `selective_theft` | Food / high-value + next-driver “no order” |

This service is a **feature producer**. Downstream owns payout blocks, suspensions, and clawback execution. Product owns the ops tuning UI; this repo exposes ingest APIs and auto-tune within guardrails.

**Full ops manual:** [docs/OPS.md](docs/OPS.md) — tuning, day-to-day use, maintenance, API reference, env vars.

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

Optional API (sync mode for local demos):

```bash
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --reload
curl localhost:8000/v1/health
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

## Docs map

| Doc | Contents |
|---|---|
| [docs/OPS.md](docs/OPS.md) | **Tuning, use, maintenance manual** |
| [examples/csv_demo/README.md](examples/csv_demo/README.md) | Offline CSV demo |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup / PRs |
| `docs/superpowers/specs/` | Design specs |

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
