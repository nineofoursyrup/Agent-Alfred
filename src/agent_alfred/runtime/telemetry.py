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
            "accounting_version": 1,
            "attempts": attempts,
            "memory": memory
            if memory is not None
            else {"gate_state": "legacy_unknown", "gate": None, "input_attempts": []},
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


def finalization_metadata(encoded, *, phase, purpose=None):
    """Body-free proof shared by summaries and evidence; truthy is not proof.

    Both current and historical finalizers wrote an attempts array and the
    explicit trace completeness bit in their atomic terminal transaction.
    Unknown or contradictory shapes cannot confirm that transaction.
    """
    try:
        value = json.loads(encoded)
        valid = (
            phase == "finished"
            and isinstance(value, dict)
            and isinstance(value.get("attempts"), list)
            and type(value.get("trace_incomplete")) is bool
            and value.get("recording_state", "recorded") == "recorded"
            and value.get("phase", "finished") == "finished"
            and (
                "accounting_version" not in value
                or type(value["accounting_version"]) is int
                and value["accounting_version"] == 1
            )
            and ("memory" not in value or isinstance(value["memory"], dict))
            and all(
                isinstance(a, dict)
                and isinstance(a.get("attempt_id"), str)
                and a.get("outcome") in ("committed", "aborted")
                and isinstance(a.get("usage"), dict)
                for a in value["attempts"]
            )
            and len({a["attempt_id"] for a in value["attempts"]})
            == len(value["attempts"])
        )
    except ValueError, TypeError:
        value, valid = {}, False
    aggregation = None
    if purpose == "aggregation":
        raw = value.get("memory", {}).get("aggregation") if valid else None
        aggregation = aggregation_metadata(raw)
    return {
        "recording_state": "recorded" if valid else None,
        "recording_source": "durable_finalization" if valid else "unknown",
        "aggregation": aggregation,
    }


def aggregation_metadata(raw):
    result = {
        "graph_result": None,
        "reply_disposition": None,
        "reason_code": None,
        "evidence_state": "unknown",
    }
    if not isinstance(raw, dict):
        return result
    graph = raw.get("graph_result")
    disposition = raw.get("reply_disposition")
    if graph not in (
        "Completed",
        "CompletedWithRecovery",
        "NoAction",
        "Failed",
        "BudgetExhausted",
    ):
        return result
    if disposition not in ("reply", "no_reply", "reply_withheld"):
        return result
    if graph == "NoAction" and disposition != "no_reply":
        return result
    if graph in ("Failed", "BudgetExhausted") and disposition == "reply":
        return result
    return {
        "graph_result": graph,
        "reply_disposition": disposition,
        "reason_code": raw.get("reason_code")
        if isinstance(raw.get("reason_code"), str)
        else None,
        "evidence_state": "known",
    }
