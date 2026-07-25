from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Protocol

from offline_cancel_risk.api.schemas import AssessRequest

_LABEL_PREFIX = "label_"
_BOOL_TRUE = {"1", "true", "yes", "y", "t"}
_OPTIONAL_STR = {
    "replacement_order_id",
    "replacement_placed_at",
    "replacement_latlong",
    "replacement_status",
    "device_id",
}
_OPTIONAL_INT = {"user_id", "merchant_id"}
_HEADS = ("cancelled_offline", "cancel_abuse", "selective_theft")


def _empty(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _parse_bool(value: str | None) -> bool | None:
    if _empty(value):
        return None
    return value.strip().lower() in _BOOL_TRUE


def _parse_optional_int(value: str | None) -> int | None:
    if _empty(value):
        return None
    return int(value)


def _parse_events(value: str | None) -> list[dict]:
    if _empty(value):
        return []
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("reassign_cancel_events must be a JSON list")
    return data


def _row_to_request(row: dict[str, str]) -> AssessRequest:
    payload: dict[str, Any] = {
        "order_display_id": row["order_display_id"],
        "driver_id": int(row["driver_id"]),
        "cancel_ts": row["cancel_ts"],
        "assign_ts": row["assign_ts"],
        "latlong": row["latlong"],
        "path_point_num": int(row["path_point_num"]),
        "order_status": row["order_status"],
        "category": row["category"],
        "order_value": float(row["order_value"]),
        "currency": row["currency"],
        "reassign_cancel_events": _parse_events(row.get("reassign_cancel_events")),
        "next_driver_no_order": _parse_bool(row.get("next_driver_no_order")),
    }
    for key in _OPTIONAL_STR:
        raw = row.get(key)
        payload[key] = None if _empty(raw) else raw
    for key in _OPTIONAL_INT:
        payload[key] = _parse_optional_int(row.get(key))
    return AssessRequest.model_validate(payload)


def _row_labels(row: dict[str, str]) -> dict[str, int | None]:
    labels: dict[str, int | None] = {}
    for head in _HEADS:
        raw = row.get(f"{_LABEL_PREFIX}{head}")
        if _empty(raw):
            labels[head] = None
        else:
            labels[head] = int(raw)
    return labels


class OrdersClient(Protocol):
    def load(self) -> list[AssessRequest]: ...


class CsvOrdersClient:
    """Local-file cancel/order source. No network."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> list[AssessRequest]:
        return [req for req, _labels in self.load_labeled()]

    def load_labeled(self) -> list[tuple[AssessRequest, dict[str, int | None]]]:
        if not self._path.is_file():
            raise FileNotFoundError(f"orders CSV not found: {self._path}")
        out: list[tuple[AssessRequest, dict[str, int | None]]] = []
        with self._path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("orders CSV has no header")
            for row in reader:
                out.append((_row_to_request(row), _row_labels(row)))
        return out
