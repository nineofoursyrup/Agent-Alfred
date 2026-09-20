"""Version 1 allowlist; unrecognized keys never become output structure."""

import math
from dataclasses import fields
from pathlib import Path

from agent_alfred import events
from agent_alfred.trace_export.errors import ExportError
from agent_alfred.trace_export.json_stream import StringSpan
from agent_alfred.trace_export.text import protect

PLACEHOLDER = "[已按分享净化移除正文]"
EVENT_TYPES = (
    events.RunStarted,
    events.RunFinished,
    events.StepStarted,
    events.StepFinished,
    events.AttemptStarted,
    events.AttemptCommitted,
    events.AttemptAborted,
    events.ToolStarted,
    events.ToolFinished,
    events.Notice,
    events.GateEvaluated,
    events.PathCaptured,
    events.WaveSettled,
    events.PathStage,
    events.GraphStarted,
    events.GraphFinished,
    events.NodeStarted,
    events.NodeFinished,
    events.NodeSkipped,
    events.NodeAborted,
)
SCHEMAS = {
    cls.__dataclass_fields__["name"].default: {f.name for f in fields(cls)}
    for cls in EVENT_TYPES
}
SOURCE_VERSIONS = {"gate.evaluated": 1, "graph.started": 1}
DURATIONS = {"duration_ms", "latency_ms"}
TEXT = {
    "user_message",
    "reply",
    "error",
    "system",
    "blocks",
    "model_content",
    "audit_content",
    "summary",
    "detail",
    "evidence",
    "audit_data",
    "unparsed_tool_arguments",
    "finalization_reason",
    "skill_notice",
}
IDENTITIES = {
    "run_id",
    "session_id",
    "attempt_id",
    "node_id",
    "process_instance_id",
    "persona_id",
    "call_id",
    "memory_id",
    "endpoint_id",
    "model_id",
    "graph_id",
    "tool_name",
    "tool_names",
    "not_executed_call_ids",
}
COUNTS = {
    "working_memory_message_count",
    "step_count",
    "step_index",
    "message_count",
    "max_tokens",
    "timeout_ms",
    "hit_count",
    "selected_count",
    "gate_step_index",
    "schema_version",
}
OUTCOMES = {
    "run.finished": {"completed", "max_steps", "failed", "interrupted"},
    "gate.evaluated": {"skip", "hit", "miss", "error"},
    "tool.finished": {"ok", "error"},
    "node.finished": {"succeeded", "failed"},
    "graph.finished": {
        "Completed",
        "CompletedWithRecovery",
        "NoAction",
        "Failed",
        "BudgetExhausted",
    },
}
ENUMS = {
    "stop_reason": {
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
        "refusal",
        "content_filter",
        "paused",
        "context_exceeded",
        "error",
        "unknown",
    },
    "effect": {"local_read", "local_write", "external"},
    "level": {"error", "warning", "info"},
    "purpose": {"chat", "inference_probe", "aggregation", "consolidation"},
    "tool_choice": {"auto", "any", "none"},
}


def duration(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class Sanitizer:
    def __init__(self, mode, redactor, root, meta):
        self.mode, self.redactor = mode, redactor
        self.aliases = {}
        self.reasons = set()
        self.origin = None
        self.hidden = {
            str(root),
            str(root.resolve()),
            str(Path.home()),
            meta["run_storage_id"],
            meta["run_dir_name"],
        }
        self.learn(meta)

    def alias(self, kind, value):
        if value is None:
            return None
        if isinstance(value, list):
            return [self.alias(kind, item) for item in value]
        if not isinstance(value, str):
            raise ExportError("corrupt_trace")
        key = (kind, value)
        if key not in self.aliases:
            self.aliases[key] = f"{kind}-{sum(k[0] == kind for k in self.aliases) + 1}"
        return self.aliases[key]

    def learn(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key.endswith("_digest") or key.endswith("_hash")) and isinstance(
                    item, str
                ):
                    self.hidden.add(item)
                elif key in IDENTITIES:
                    kind = {
                        "tool_names": "tool_name",
                        "not_executed_call_ids": "call_id",
                    }.get(key, key)
                    self.alias(kind, item)
                elif key in {"id", "name"} and value.get("type") == "tool_call":
                    self.alias("call_id" if key == "id" else "tool_name", item)
                else:
                    self.learn(item)
        elif isinstance(value, list):
            for item in value:
                self.learn(item)

    def text(self, value):
        if self.mode == "share":
            self.reasons.add("free_text_placeholder")
            return PLACEHOLDER
        if isinstance(value, StringSpan):
            return StreamingText(value, self)
        if isinstance(value, dict):
            return {
                self.text(key): "***"
                if key.lower()
                in {
                    "api_key",
                    "authorization",
                    "token",
                    "password",
                    "secret",
                    "access_token",
                }
                else self.text(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.text(item) for item in value]
        if not isinstance(value, str):
            return value
        return b"".join(protect([value.encode("utf-8")], self)).decode("utf-8")

    def event(self, event, artifacts):
        payload = event.get("payload")
        name = event.get("payload_name")
        if (
            name not in SCHEMAS
            or not isinstance(payload, dict)
            or payload.get("name") != name
            or event.get("trace_policy") != "persist"
            or payload.get("trace_policy") != "persist"
        ):
            raise ExportError("unsupported_format")
        if name in SOURCE_VERSIONS and (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != SOURCE_VERSIONS[name]
        ):
            raise ExportError("unsupported_format")
        if name.startswith("path.") and (
            type(payload.get("evidence_version")) is not int
            or payload["evidence_version"] != 1
        ):
            raise ExportError("unsupported_format")
        for key in (
            "run_id",
            "process_instance_id",
            "session_id",
            "attempt_id",
            "node_id",
            "step_index",
        ):
            if key in payload and (
                type(payload[key]) is not type(event.get(key))
                or payload[key] != event.get(key)
            ):
                raise ExportError("unsafe_source")
        ts = event.get("ts")
        if type(ts) not in (float, int) or not math.isfinite(ts):
            raise ExportError("corrupt_trace")
        if self.origin is None:
            self.origin = ts
        output = {
            "seq": event["seq"],
            "payload_name": name,
            "ts" if self.mode == "diagnostic" else "relative_seconds": ts
            if self.mode == "diagnostic"
            else ts - self.origin,
        }
        for key in (
            "run_id",
            "session_id",
            "attempt_id",
            "node_id",
            "process_instance_id",
        ):
            output[key] = self.alias(key, event.get(key))
        if type(event.get("step_index")) is int or event.get("step_index") is None:
            output["step_index"] = event.get("step_index")
        else:
            raise ExportError("corrupt_trace")
        safe = {"name": name}
        for key, value in payload.items():
            if key in {"name", "trace_policy"}:
                continue
            if key not in SCHEMAS[name]:
                self.reasons.add("unknown_field_removed")
            elif key == "audit_content" and isinstance(value, dict):
                reference = artifacts[value["artifact"]]
                safe[key] = reference
            elif key in TEXT:
                safe[key] = None if value is None else self.text(value)
            elif key in {"model", "model_ref"}:
                if value is None:
                    safe[key] = None
                elif (
                    isinstance(value, dict)
                    and {"endpoint_id", "model_id"} <= value.keys()
                ):
                    safe[key] = {
                        k: self.alias(k, value[k]) for k in ("endpoint_id", "model_id")
                    }
                else:
                    raise ExportError("unsupported_format")
            elif key in IDENTITIES:
                kind = {
                    "tool_names": "tool_name",
                    "not_executed_call_ids": "call_id",
                }.get(key, key)
                safe[key] = self.alias(kind, value)
            elif key in COUNTS and (value is None or type(value) is int and value >= 0):
                safe[key] = value
            elif key in DURATIONS and duration(value):
                safe[key] = value
            elif (
                name == "gate.evaluated" and key == "timing" and isinstance(value, dict)
            ):
                safe[key] = {
                    k: v
                    for k, v in value.items()
                    if k in {"model_ms", "search_ms", "selection_ms"}
                    and (v is None or duration(v))
                }
                if safe[key] != value:
                    self.reasons.add("environment_or_unlisted_field_removed")
            elif key == "outcome":
                if isinstance(value, str) and value in OUTCOMES.get(name, set()):
                    safe[key] = value
                else:
                    self.reasons.add("environment_or_unlisted_field_removed")
            elif key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
                safe[key] = value
            elif (
                key in {"partial", "streamed", "truncated", "decision"}
                and type(value) is bool
            ):
                safe[key] = value
            else:
                self.reasons.add("environment_or_unlisted_field_removed")
        output["payload"] = safe
        return output


class StreamingText:
    def __init__(self, source, sanitizer):
        self.source, self.sanitizer = source, sanitizer

    def chunks(self):
        return protect(self.source.chunks(), self.sanitizer)
