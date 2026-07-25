import hashlib


def _event_timestamp(event: dict) -> str:
    ts = event.get("ts") or event.get("timestamp")
    if ts is None:
        return ""
    return str(ts)


def build_lineage_id(order_display_id: str, events: list[dict]) -> str:
    timestamps = sorted(_event_timestamp(e) for e in events)
    payload = f"{order_display_id}|{'|'.join(timestamps)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarize_lineage(events: list[dict]) -> dict:
    timestamps = sorted(t for t in (_event_timestamp(e) for e in events) if t)
    event_types = [str(e.get("type", e.get("event_type", ""))) for e in events]
    return {
        "event_count": len(events),
        "timestamps": timestamps,
        "event_types": event_types,
    }
