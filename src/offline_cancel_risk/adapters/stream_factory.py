"""Build StreamPublisher from settings (JSONL default, optional HTTP fan-out)."""

from __future__ import annotations

from offline_cancel_risk.adapters.http_stream import FanoutStreamPublisher, HttpStreamPublisher
from offline_cancel_risk.adapters.publishers import JsonlStreamPublisher
from offline_cancel_risk.ports import StreamPublisher
from offline_cancel_risk.settings import Settings


def make_stream_publisher(settings: Settings) -> StreamPublisher:
    jsonl = JsonlStreamPublisher(stream_path=settings.stream_path)
    url = (settings.stream_url or "").strip()
    if not url:
        return jsonl
    http = HttpStreamPublisher(
        url,
        api_key=settings.stream_api_key,
        timeout_s=float(settings.stream_timeout_s),
    )
    return FanoutStreamPublisher([jsonl, http])
