"""Persist assessment + side effects (chains, stream, sampler)."""

from __future__ import annotations

import logging

from offline_cancel_risk.feedback.sampler import safe_inline_sample
from offline_cancel_risk.pipeline.context import AssessContext

_LOG = logging.getLogger(__name__)


def run_publish_stage(ctx: AssessContext) -> None:
    assert ctx.result is not None
    req = ctx.req
    result = ctx.result

    if req.force_reassess:
        mark = getattr(ctx.table, "mark_prior_provisional", None)
        if callable(mark):
            mark(req.order_display_id, before_generation=ctx.generation)
    ctx.table.upsert(result)
    if ctx.driver_chains is not None:
        try:
            ctx.driver_chains.record_from_assess(
                driver_id=req.driver_id,
                order_display_id=req.order_display_id,
                cancel_ts=req.cancel_ts,
                reassign_cancel_events=list(req.reassign_cancel_events),
            )
        except Exception:
            _LOG.exception(
                "Driver chain record failed for order=%s", req.order_display_id
            )
    try:
        ctx.stream.publish(result)
    except Exception:
        _LOG.exception(
            "Stream publish failed after table upsert for order=%s; "
            "idempotent cache still protects recomputation",
            req.order_display_id,
        )
    safe_inline_sample(ctx.tickets, result, ctx.policy, bias_hints=ctx.bias_hints)
