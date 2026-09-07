"""Derived usage snapshot schema. Not a runtime file and not a CLI command."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent_alfred.runtime.evidence import cost

SCHEMA_VERSION = "1"


def derived_usage_snapshot(
    attempts: Sequence[Mapping[str, Any]],
    *,
    generated_at: str,
    prices=None,
    schema_version: str = SCHEMA_VERSION,
    computed_at: str | None = None,
) -> dict[str, Any]:
    """Pure projection. Callers that want a file must export explicitly later."""
    moment = computed_at or generated_at
    records = []
    for attempt in attempts:
        usage = attempt.get("usage") or {}
        model = attempt.get("model") or {}
        records.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "outcome": attempt.get("outcome"),
                "endpoint_id": model.get("endpoint_id"),
                "model_id": model.get("model_id"),
                "usage": usage,
                "cost": cost(
                    usage,
                    prices,
                    endpoint_id=model.get("endpoint_id"),
                    model_id=model.get("model_id"),
                    computed_at=moment,
                ),
            }
        )
    return {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "records": records,
    }
