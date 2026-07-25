# CSV demo (no network)

Runs `assess_order` on synthetic sample orders + GPS using `CsvOrdersClient` and `CsvGpsClient`. No API keys or GPS HTTP calls.

## Quickstart

From the repo root (after `pip install -e ".[dev]"`):

```bash
python -m examples.csv_demo
```

Writes `examples/csv_demo/out.json` with three-head scores per order.

## Sample data

Synthetic only (no PII):

- `ORD-FOOD-SYN-1` — FOOD + `next_driver_no_order` (selective-theft style)
- `ORD-HAUL-SYN-1` — haul multistop with clustered GPS near the final stop

Optional `label_*` columns support `scripts/backtest.py`.

## Options

```bash
python -m examples.csv_demo --orders path/to/orders.csv --gps path/to/gps.csv --out /tmp/out.json
```
