"""HTTP webhook StreamPublisher — POST each assessment JSON to Downstream/bus."""

from __future__ import annotations

import logging

import httpx

from offline_cancel_risk.api.schemas import AssessmentResult

_LOG = logging.getLogger(__name__)


class HttpStreamPublisher:
    """Fire-and-forget POST of AssessmentResult JSON. Failures are logged, not raised.

    ponytail: sync httpx; for high QPS inject a custom publisher (Kafka, etc.).
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        self._url = url.strip()
        if not self._url:
            raise ValueError("stream url required")
        self._api_key = api_key
        self._timeout = timeout_s

    def publish(self, result: AssessmentResult) -> None:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        try:
            resp = httpx.post(
                self._url,
                content=result.model_dump_json(),
                headers=headers,
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                _LOG.warning(
                    "HttpStreamPublisher %s → HTTP %s",
                    self._url,
                    resp.status_code,
                )
        except Exception:
            _LOG.exception("HttpStreamPublisher failed url=%s", self._url)


class FanoutStreamPublisher:
    """Publish to every child; later children still run if an earlier one fails."""

    def __init__(self, publishers: list) -> None:
        if not publishers:
            raise ValueError("need at least one publisher")
        self._pubs = list(publishers)

    def publish(self, result: AssessmentResult) -> None:
        for pub in self._pubs:
            try:
                pub.publish(result)
            except Exception:
                _LOG.exception("FanoutStreamPublisher child failed: %s", type(pub).__name__)
