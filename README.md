# offline-cancel-risk

## Elevator pitch

Canceled orders don't always mean stopped activity. Off-platform completions, marketplace gaming, and inventory loss drain margins quietly. Offline Cancel Risk transforms raw order and GPS telemetry into three configurable risk scores. This gives downstream systems the intelligence to block post-cancel fraud at scale without overloading operations teams.

It is an installable, Apache-2.0 toolkit (not a locked-in vendor product): plug in your GPS and order feeds, or try the zero-network CSV demo in minutes.

## Who it’s for

| Audience | Why you’d use it |
|---|---|
| **Ops / logistics managers** | Tune thresholds and weights; prioritize by expected revenue at risk, not raw flag counts |
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

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
