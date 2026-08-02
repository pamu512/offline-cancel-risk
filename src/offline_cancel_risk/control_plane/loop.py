"""Debounced feedback → tune + periodic control-plane tick."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from offline_cancel_risk.adapters.publishers import SqliteTablePublisher
from offline_cancel_risk.control_plane.cycle import (
    assessments_as_dicts,
    markets_from_assessments,
    run_metrics_and_tune,
)
from offline_cancel_risk.feedback.sampler import (
    bias_hints_from_metrics,
    run_batch_sample,
)
from offline_cancel_risk.feedback.tickets import LabelTicketStore

_LOG = logging.getLogger(__name__)


class ControlPlaneLoop:
    """Background debounce + optional periodic tick.

    ponytail: in-process asyncio only — fine for single-replica demo; multi-replica
    needs an external lock / queue.
    """

    def __init__(
        self,
        *,
        debounce_seconds: float,
        tick_seconds: float,
        run_kwargs: dict[str, Any],
        table: SqliteTablePublisher,
        tickets: LabelTicketStore,
        sample_on_tick: bool = True,
    ) -> None:
        self._debounce_s = max(0.0, float(debounce_seconds))
        self._tick_s = max(0.0, float(tick_seconds))
        self._run_kwargs = run_kwargs
        self._table = table
        self._tickets = tickets
        self._sample_on_tick = sample_on_tick
        self._pending: set[tuple[str, str]] = set()
        self._debounce_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def notify_feedback(self, order_display_id: str) -> None:
        latest = self._table.latest(order_display_id)
        if latest is None:
            region, city = "", ""
        else:
            region = (latest.region_code or "").strip().upper()
            city = (latest.city_code or "").strip().upper()
        if not region:
            # Still recompute global metrics when market unknown
            self._pending.add(("", ""))
        else:
            self._pending.add((region, city))
        if self._debounce_s <= 0:
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(
            self._debounce_flush(), name="ocr-control-debounce"
        )

    async def _debounce_flush(self) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
            await self.flush_pending(reason="feedback_debounce")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Debounced control-plane flush failed")

    async def flush_pending(self, *, reason: str) -> list[dict[str, Any]]:
        async with self._lock:
            markets = list(self._pending)
            self._pending.clear()
        results: list[dict[str, Any]] = []
        for region, city in markets:
            try:
                results.append(
                    run_metrics_and_tune(
                        **self._run_kwargs,
                        table=self._table,
                        region_code=region,
                        city_code=city,
                        reason=reason,
                    )
                )
            except Exception:
                _LOG.exception(
                    "Control cycle failed for market=%s/%s", region, city
                )
        return results

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                await self._run_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("Control-plane tick failed")

    async def _run_tick(self) -> None:
        assessments = self._table.list_latest_assessments()
        if self._sample_on_tick:
            labeled = {f["order_display_id"] for f in self._table.list_feedback()}
            hints = bias_hints_from_metrics(
                self._run_kwargs["label_metrics"].latest(limit=50)
            )
            run_batch_sample(
                self._tickets,
                assessments,
                self._run_kwargs["policy"],
                labeled_order_ids=labeled,
                bias_hints=hints,
            )
        markets = markets_from_assessments(assessments_as_dicts(self._table))
        if not markets:
            markets = [("", "")]
        async with self._lock:
            for region, city in markets:
                try:
                    run_metrics_and_tune(
                        **self._run_kwargs,
                        table=self._table,
                        region_code=region,
                        city_code=city,
                        reason="scheduled_tick",
                    )
                except Exception:
                    _LOG.exception(
                        "Scheduled tune failed for market=%s/%s", region, city
                    )

    def start(self) -> None:
        if self._tick_s > 0 and (
            self._tick_task is None or self._tick_task.done()
        ):
            self._tick_task = asyncio.create_task(
                self._tick_loop(), name="ocr-control-tick"
            )

    async def stop(self) -> None:
        for task in (self._debounce_task, self._tick_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._debounce_task = None
        self._tick_task = None
