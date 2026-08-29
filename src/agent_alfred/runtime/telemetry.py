"""Run-level telemetry: direct sum of ModelResult.attempts, never the event stream."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agent_alfred.model import AttemptRecord, ModelResult, Usage


def serialize_run_telemetry(
    model_results: Sequence[ModelResult],
    incomplete: bool,
    reason: str | None,
    *,
    redactor: Any | None = None,
) -> str:
    attempts = [
        _attempt_payload(record, redactor)
        for result in model_results
        for record in result.attempts
    ]
    return json.dumps(
        {
            "attempts": attempts,
            "trace_incomplete": incomplete,
            "trace_incomplete_reason": reason,
        },
        ensure_ascii=False,
    )


def _attempt_payload(record: AttemptRecord, redactor: Any | None) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "streamed": record.streamed,
        "outcome": record.outcome,
        "usage": _usage_payload(record.usage, redactor),
    }


def _usage_payload(usage: Usage, redactor: Any | None) -> dict[str, Any]:
    raw: Any = usage.raw
    if redactor is not None:
        try:
            raw = redactor.redact_jsonable(raw)
        except Exception:
            raw = {"redaction_failed": True}
    cost = usage.endpoint_reported_cost_usd
    return {
        "total_input_tokens": usage.total_input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "endpoint_reported_cost_usd": None if cost is None else format(cost, "f"),
        "raw": raw,
    }
