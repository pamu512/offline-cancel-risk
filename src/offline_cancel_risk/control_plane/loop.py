"""Debounced feedback → tune + periodic control-plane tick."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from offline_cancel_risk.control_plane.cycle import (
    assessments_as_dicts,
    markets_from_assessments,
    run_metrics_and_tune,
)
from offline_cancel_risk.control_plane.calibrate import (
    CalibrationFitContext,
    run_calibration_fit,
)
from offline_cancel_risk.control_plane.dbscan_retune import (
    DbscanRetuneContext,
    dbscan_retune_cfg,
    run_dbscan_retune,
)
from offline_cancel_risk.scoring.calibration import calibration_cfg
from offline_cancel_risk.control_plane.leader import FileLeaderLock
from offline_cancel_risk.feedback.sampler import (
    bias_hints_from_metrics,
    run_batch_sample,
)
from offline_cancel_risk.feedback.tickets import LabelTicketStore
from offline_cancel_risk.ports import AssessmentStore

_LOG = logging.getLogger(__name__)


class ControlPlaneLoop:
    """Background debounce + optional periodic tick.

    Multi-replica: optional FileLeaderLock skips ticks/debounced flushes when
    another process holds the lockfile.
    """

    def __init__(
        self,
        *,
        debounce_seconds: float,
        tick_seconds: float,
        run_kwargs: dict[str, Any],
        table: AssessmentStore,
        tickets: LabelTicketStore,
        sample_on_tick: bool = True,
        leader: FileLeaderLock | None = None,
    ) -> None:
        self._debounce_s = max(0.0, float(debounce_seconds))
        self._tick_s = max(0.0, float(tick_seconds))
        self._run_kwargs = run_kwargs
        self._table = table
        self._tickets = tickets
        self._sample_on_tick = sample_on_tick
        self._leader = leader
        self._pending: set[tuple[str, str]] = set()
        self._debounce_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def _is_leader(self) -> bool:
        if self._leader is None:
            return True
        return self._leader.try_acquire()

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

    def _tune_kwargs(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in self._run_kwargs.items()
            if k
            not in {
                "gps_cache",
                "dbscan_retune_store",
                "calibrators",
                "calibration_run_store",
            }
        }

    async def flush_pending(self, *, reason: str) -> list[dict[str, Any]]:
        if not self._is_leader():
            return []
        async with self._lock:
            markets = list(self._pending)
            self._pending.clear()
        results: list[dict[str, Any]] = []
        for region, city in markets:
            try:
                results.append(
                    run_metrics_and_tune(
                        **self._tune_kwargs(),
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

    async def _maybe_dbscan_retune(self, markets: list[tuple[str, str]]) -> None:
        policy = self._run_kwargs.get("policy") or {}
        cfg = dbscan_retune_cfg(policy)
        if not cfg.get("on_tick"):
            return
        gps_cache = self._run_kwargs.get("gps_cache")
        if gps_cache is None:
            return
        for region, city in markets:
            if not region:
                continue
            try:
                ctx = DbscanRetuneContext(
                    base_policy=policy,
                    guardrails=self._run_kwargs["guardrails"],
                    overlays=self._run_kwargs["overlays"],
                    audit=self._run_kwargs["audit"],
                    hardgates=self._run_kwargs["hardgates"],
                    gps_cache=gps_cache,
                    feedback=self._table.list_feedback(),
                    region_code=region,
                    city_code=city,
                    run_store=self._run_kwargs.get("dbscan_retune_store"),
                )
                await asyncio.to_thread(run_dbscan_retune, ctx)
            except Exception:
                _LOG.exception(
                    "DBSCAN retune tick failed for market=%s/%s", region, city
                )

    async def _maybe_calibrate(self, markets: list[tuple[str, str]]) -> None:
        policy = self._run_kwargs.get("policy") or {}
        cfg = calibration_cfg(policy)
        if not cfg.get("on_tick"):
            return
        calibrators = self._run_kwargs.get("calibrators")
        if calibrators is None:
            return
        assessments = assessments_as_dicts(self._table)
        feedback = self._table.list_feedback()
        for region, city in markets:
            if not region:
                continue
            try:
                ctx = CalibrationFitContext(
                    base_policy=policy,
                    audit=self._run_kwargs["audit"],
                    calibrators=calibrators,
                    assessments=assessments,
                    feedback=feedback,
                    region_code=region,
                    city_code=city,
                    run_store=self._run_kwargs.get("calibration_run_store"),
                )
                await asyncio.to_thread(run_calibration_fit, ctx)
            except Exception:
                _LOG.exception(
                    "Calibration tick failed for market=%s/%s", region, city
                )

    async def _run_tick(self) -> None:
        if not self._is_leader():
            return
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
                        **self._tune_kwargs(),
                        table=self._table,
                        region_code=region,
                        city_code=city,
                        reason="scheduled_tick",
                    )
                except Exception:
                    _LOG.exception(
                        "Scheduled tune failed for market=%s/%s", region, city
                    )
            await self._maybe_dbscan_retune(markets)
            await self._maybe_calibrate(markets)

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
        if self._leader is not None:
            self._leader.release()
