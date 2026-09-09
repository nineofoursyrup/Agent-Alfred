"""Run-level telemetry: direct sum of ModelResult.attempts, never the event stream."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agent_alfred.model import AttemptRecord, ModelResult, Usage


class AttemptObservationFailed(Exception):
    """A completed model call failed in host accounting, not in model IO."""

    def __init__(self, cause: Exception):
        super().__init__("attempt_observation_failed")
        self.cause = cause


def serialize_run_telemetry(
    model_results: Sequence[ModelResult],
    incomplete: bool,
    reason: str | None,
    *,
    redactor: Any | None = None,
    memory: dict | None = None,
) -> str:
    attempts = [
        _attempt_payload(record, redactor)
        for result in model_results
        for record in result.attempts
    ]
    return json.dumps(
        {
            "attempts": attempts,
            "memory": memory if memory is not None else {
                "gate_state": "legacy_unknown", "gate": None, "input_attempts": []
            },
            "trace_incomplete": incomplete,
            "trace_incomplete_reason": reason,
        },
        ensure_ascii=False,
    )


def _attempt_payload(record: AttemptRecord, redactor: Any | None) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "streamed": record.streamed,
        "model": None if record.model is None else {
            "endpoint_id": record.model.endpoint_id,
            "model_id": record.model.model_id,
        },
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
