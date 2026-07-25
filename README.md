# offline-cancel-risk

Reusable async toolkit for scoring **cancelled-order offline**, **cancel abuse**, and **selective theft** risk in logistics. Policy-driven rules, GPS windows, DBSCAN clustering, and optional ML blending ship as an installable Python package (`offline_cancel_risk`).

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

After later tasks add the examples module:

```bash
python -m examples.csv_demo
```

## Optional HTTP API

A FastAPI service can be run when the API module is present (see project docs). Configure tenant GPS and paths via `OCR_*` environment variables; default policy lives at `config/policy.default.yaml`.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
