# offline-cancel-risk

Reusable async toolkit for scoring **cancelled-order offline**, **cancel abuse**, and **selective theft** risk in logistics. Policy-driven rules, GPS windows, DBSCAN clustering, and optional ML blending ship as an installable Python package (`offline_cancel_risk`).

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
