# offline-cancel-risk

## Elevator pitch

When a logistics order is cancelled, money can still walk out the door: the trip may have been completed off-platform, cancel/reassign games may be gaming the marketplace, or food/high-value goods may have gone missing. **offline-cancel-risk** turns cancelled-order + GPS evidence into three independent, ops-tunable risk scores—so downstream systems can stop revenue leakage without a human reviewing every cancel.

It is an installable, Apache-2.0 toolkit (not a locked-in vendor product): plug in your GPS and order feeds, or try the zero-network CSV demo in minutes.

## Who it’s for

| Audience | Why you’d use it |
|---|---|
| **Ops / logistics managers** | Product FE tunes thresholds/weights per `region_code` / `city_code` within risk guardrails (this service ingests overlays); prioritize review by expected revenue at risk |
| **Fraud / risk investigators** | Get explainable reason codes and evidence packs for the rare disputes that still need a human |
| **Data / platform engineers** | Drop in as an async microservice or library job; consume scores from a stream + table |
| **Any logistics / delivery team** | Clone or `pip install`, bring CSV or adapters—no proprietary stack required |

**Not for:** realtime cancel-path blocking, payout/suspension enforcement (downstream owns actions), or replacing your LBS GPS platform.

## What it scores

Independent soft scores + policy flags:

1. **Cancelled offline** — trip likely completed off-platform (revenue leakage)
2. **Cancel abuse** — cancel / reassign games (order stays active, chains, cancel near destination, …)
3. **Selective theft** — food / high-value + next-driver “no order” signals

## Quickstart (no API keys)

```bash
pip install -e ".[dev]"
pytest -q
python -m examples.csv_demo
```

## Install (development)

From a clone of this repository:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
pytest
```

Focused example:

```bash
pytest tests/test_settings_policy.py -v
```

## CSV demo

Offline end-to-end on synthetic sample CSVs (`CsvOrdersClient` + `CsvGpsClient`):

```bash
python -m examples.csv_demo
```

See [examples/csv_demo/README.md](examples/csv_demo/README.md).

Labeled metrics smoke:

```bash
python scripts/backtest.py
```

## Optional HTTP API

```bash
OCR_SYNC_ASSESS=1 uvicorn offline_cancel_risk.main:app --reload
```

Configure tenant GPS and paths via `OCR_*` environment variables; default policy lives at `config/policy.default.yaml`.

### Model sideload / shadow / canary

Bundle layout: `model.json` (`format`: `joblib`|`onnx`) + artifact + `feature_schema.json` + `metrics_baseline.json`.

```bash
# Sideload a challenger (shadow by default)
curl -X POST localhost:8000/v1/models \
  -H 'content-type: application/json' \
  -d '{"bundle_path":"/path/to/bundle","role":"shadow"}'

# Evaluate promote gates (auto-starts canary when ready; defaults 5% / 24h)
curl -X POST localhost:8000/v1/models/{id}/evaluate
```

Serving flags use the champion (or canary cohort when active). Shadow scores are recorded on each assess without changing flags outside the canary.

### Market policy overlays (ops ingest)

Product owns the tuning UI. This service exposes ingest + guardrails so ops can set almost all model/policy numerics per **region** and **city**, clamped by `config/policy_guardrails.default.yaml`.

Merge order at assess time: **default ← region (`city_code=""`) ← city**. Pass `region_code` / `city_code` on `/v1/assess`. Each result includes a `routing` object (`priority` / `queue`) for prioritized investigation queues.

```bash
# Guardrails Product FE should enforce client-side (server re-validates)
curl localhost:8000/v1/policy/guardrails

# Ingest city overlay (rejects values outside guardrails with 400)
curl -X PUT localhost:8000/v1/policy/overlays \
  -H 'content-type: application/json' \
  -d '{
    "region_code":"PH",
    "city_code":"MNL",
    "overlay":{
      "thresholds":{"cancelled_offline":0.82},
      "dbscan":{"confidence_threshold":0.8},
      "routing":{"p1_attention_min":150}
    }
  }'

# Resolved policy for a market
curl 'localhost:8000/v1/policy/resolved?region_code=PH&city_code=MNL'
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
