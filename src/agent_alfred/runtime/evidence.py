"""Read-only Run evidence. Missing process facts never change a Run outcome."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MAX_TRACE_BYTES = 32 * 1024 * 1024


def read_evidence(
    store, redactor, run_id: str, trace_root: Path, *, overrides=None
) -> dict | None:
    with store.reading() as conn:
        row = conn.execute(
            "SELECT phase, telemetry FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        pruned = conn.execute(
            "SELECT prune_reason FROM trace_prunes WHERE run_id = ?", (run_id,)
        ).fetchone()
    telemetry = json.loads(row[1]) if row[1] else {}
    # Active and pending Runs are presented by the existing SSE ReplayRing.
    # Disk evidence is historical once the durable index says finished,
    # including interrupted recovery without a telemetry record.
    status, events = (
        _read_trace(trace_root, run_id) if row[0] == "finished"
        else ("live", [])
    )
    if pruned:
        status, events = "pruned", []
    try:
        safe_events = [_safe_event(event) for event in events]
    except (ValueError, KeyError, TypeError, AttributeError):
        status, safe_events = "unavailable", []
    try:
        projected = redactor.redact_jsonable(
            {
                "run_id": run_id,
                "trace_status": status,
                "trace_incomplete": telemetry.get("trace_incomplete"),
                "recording_state": "recorded" if row[1] else None,
                "events": safe_events,
                "support_overrides": [
                    asdict(item) for item in overrides.for_run(run_id)
                ]
                if overrides
                else [],
                "attempts": [
                    {
                        "attempt_id": attempt["attempt_id"],
                        "outcome": attempt["outcome"],
                        "usage": _safe_usage(attempt.get("usage")),
                        "cost": cost(attempt.get("usage")),
                    }
                    for attempt in telemetry.get("attempts", [])
                ],
            }
        )
    except Exception:
        return {
            "run_id": run_id,
            "trace_status": "unavailable",
            "trace_incomplete": None,
            "recording_state": None,
            "events": [],
            "attempts": [],
            "support_overrides": [],
        }
    return projected


def _read_trace(root: Path, run_id: str) -> tuple[str, list]:
    digest = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    try:
        candidates = list(root.glob(f"????-??-??/??????Z-{digest}"))
        if len(candidates) != 1:
            return "unavailable", []
        bundle = candidates[0]
        if any(path.is_symlink() for path in (
            root, bundle.parent, bundle, bundle / "meta.json", bundle / "trace.jsonl"
        )):
            return "unavailable", []
        with (bundle / "meta.json").open("rb") as source:
            metadata = json.loads(source.read(8193))
        created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
        if (
            metadata["run_id"] != run_id
            or metadata["run_storage_id"] != digest
            or metadata["run_dir_name"] != bundle.name
            or created.strftime("%Y-%m-%d") != bundle.parent.name
            or created.strftime("%H%M%S") + "Z-" + digest != bundle.name
        ):
            return "unavailable", []
        with (bundle / "trace.jsonl").open("rb") as source:
            data = source.read(MAX_TRACE_BYTES + 1)
        if len(data) > MAX_TRACE_BYTES:
            return "too_large", []
        lines = data.splitlines(keepends=True)
        complete = bool(lines) and lines[-1].endswith(b"\n")
        if not complete and lines:
            lines.pop()
        events = [json.loads(line) for line in lines]
        previous = 0
        for event in events:
            if (event["run_id"] != run_id
                    or event["process_instance_id"] != metadata["process_instance_id"]
                    or type(event["seq"]) is not int or event["seq"] <= previous):
                return "unavailable", []
            previous = event["seq"]
        finished = events and events[-1]["payload_name"] == "run.finished"
        return ("available" if complete and finished else "partial"), events
    except (OSError, ValueError, KeyError, TypeError):
        return "unavailable", []


def _safe_event(event: dict) -> dict:
    payload = event["payload"]
    selected = {
        key: payload[key]
        for key in (
            "name",
            "attempt_id",
            "model",
            "streamed",
            "duration_ms",
            "stop_reason",
            "outcome",
        )
        if key in payload
    }
    if payload.get("code") == "model_support_flipped":
        selected.update(
            {
                key: payload[key]
                for key in ("code", "detail", "evidence")
                if key in payload
            }
        )
    selected["blocks"] = [
        {"type": "text", "text": block["text"]} if block.get("type") == "text"
        else {"type": block.get("type", "unknown")}
        for block in payload.get("blocks", [])
    ]
    error = payload.get("error")
    if isinstance(error, dict):
        selected["error"] = {"code": error.get("code")}
    elif isinstance(error, str):
        selected["error"] = error
    return {"seq": event["seq"], "process_instance_id": event["process_instance_id"],
            "envelope": {key: event.get(key) for key in (
                "run_id", "session_id", "step_index", "attempt_id", "ts", "source"
            )}, "payload": selected}


def _safe_usage(usage: dict | None) -> dict:
    return {key: value for key, value in (usage or {}).items()
            if key in {"total_input_tokens", "uncached_input_tokens",
                       "cache_read_tokens", "cache_write_tokens", "output_tokens",
                       "reasoning_tokens", "endpoint_reported_cost_usd"}}


def cost(usage: dict | None) -> dict[str, Any]:
    """No pricing source is installed yet; never manufacture an estimate."""
    amount = (usage or {}).get("endpoint_reported_cost_usd")
    try:
        value = Decimal(str(amount))
        if value.is_finite() and value >= 0:
            return {"state": "exact", "amount": format(value, "f"), "currency": "USD"}
    except InvalidOperation:
        pass
    return {"state": "unknown"}
