"""Immutable tool declarations and their public execution registry."""

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from agent_alfred.events import EventEnvelope, ToolFinished, ToolProgress, ToolStarted
from agent_alfred.messages import TextBlock, ToolResultBlock
from agent_alfred.model import ToolSpec
from agent_alfred.redact import Redactor
from agent_alfred.resource_rollback import raise_if_rollback_pending
from agent_alfred.tools.schema import freeze, validate_input, validate_schema

Effect = Literal["local_read", "local_write", "external"]


@dataclass(frozen=True)
class ToolCost:
    units: float
    unit: str


@dataclass(frozen=True)
class UnknownCost:
    reason: str = "not_reported"


@dataclass(frozen=True)
class ToolSuccess:
    content: Sequence[TextBlock]
    summary: str | None = None
    cost: ToolCost | UnknownCost | None = None
    stop_reason: str | None = None
    operation_id: str | None = None


ToolErrorCode = Literal[
    "unknown_tool",
    "invalid_input",
    "configuration_required",
    "not_authorized",
    "timeout",
    "execution_error",
    "unavailable",
]


@dataclass(frozen=True)
class ToolFailure:
    code: ToolErrorCode
    content: Sequence[TextBlock]
    summary: str | None = None
    cost: ToolCost | UnknownCost | None = None
    stop_reason: str | None = None
    operation_id: str | None = None
    memory_code: str | None = None


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    step_index: int
    call_id: str
    source: str
    deadline: float
    session_id: str | None = None
    permission: object | None = None
    events: object | None = None
    monotonic: Callable[[], float] = time.monotonic

    def checkpoint(self):
        if self.monotonic() >= self.deadline:
            raise TimeoutError("Tool IO deadline exhausted")


@dataclass(frozen=True)
class ToolExecution:
    block: ToolResultBlock
    stop_reason: str | None = None
    operation_id: str | None = None
    system_receipt: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    fn: Callable
    effect: Effect
    budget_s: float = 30.0
    summary_keys: Sequence[str] = ()
    emits_progress: bool = False
    parallel_safe: bool = False


@dataclass(frozen=True)
class ToolPolicy:
    configured: bool = False
    authorization: Literal["unset", "allowed", "denied"] = "unset"


class ProgressEvents:
    def __init__(self, events, envelope, call_id, tool_name):
        self._events, self._envelope = events, envelope
        self._call_id, self._name = call_id, tool_name

    def progress(self, text):
        if self._events is not None:
            self._events.emit(
                ToolProgress(self._call_id, self._name, text), self._envelope
            )


class ToolRegistry:
    def __init__(
        self,
        tools: Sequence[Tool],
        *,
        clock,
        policies=None,
        model_content_limit=8000,
        redactor=None,
        external_ledger=None,
    ):
        for tool in tools:
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name) is None:
                raise ValueError("invalid tool name")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("duplicate tool name")
        for tool in tools:
            if tool.effect not in ("local_read", "local_write", "external"):
                raise ValueError("invalid tool effect")
            if not callable(tool.fn) or tool.parallel_safe is not False:
                raise ValueError("invalid synchronous tool declaration")
            if type(tool.emits_progress) is not bool:
                raise ValueError("invalid progress declaration")
            if (
                type(tool.budget_s) not in (int, float)
                or not math.isfinite(tool.budget_s)
                or tool.budget_s <= 0
            ):
                raise ValueError("invalid tool budget")
            validate_schema(tool.input_schema)
            if tool.input_schema["type"] != "object":
                raise ValueError("tool parameters must be object")
            if any(
                key not in tool.input_schema.get("properties", {})
                for key in tool.summary_keys
            ):
                raise ValueError("summary_keys must name declared arguments")
        self._tools = tuple(
            replace(tool, input_schema=freeze(tool.input_schema)) for tool in tools
        )
        self._external_ledger = external_ledger
        self._redactor = redactor or Redactor(())
        if type(model_content_limit) is not int or model_content_limit < 1:
            raise ValueError("invalid projection limit")
        self._limit = model_content_limit
        self._clock = clock
        self._policies = freeze(policies or {})
        self._schemas = self._build_schemas()

    def schemas(self):
        return self._schemas

    def _build_schemas(self):
        exposed = []
        for tool in self._tools:
            policy = self._policies.get(tool.name, ToolPolicy())
            if tool.effect == "external":
                if policy.authorization == "denied" or (
                    policy.configured and policy.authorization != "allowed"
                ):
                    continue
                if not policy.configured:
                    exposed.append(
                        ToolSpec(
                            tool.name,
                            "当前未配置，调用只返回配置指引，不执行动作。",
                            freeze(
                                {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                }
                            ),
                        )
                    )
                    continue
            exposed.append(ToolSpec(tool.name, tool.description, tool.input_schema))
        return tuple(exposed)

    def execute(self, call, context, *, events=None):
        started = self._clock.monotonic()
        envelope = EventEnvelope(
            started,
            context.run_id,
            context.session_id,
            context.step_index,
            None,
            None,
            context.source,
        )
        executed = False
        tool = next((tool for tool in self._tools if tool.name == call.name), None)
        if tool is None:
            outcome = ToolFailure("unknown_tool", (TextBlock("Unknown tool."),))
        elif self._clock.monotonic() >= context.deadline:
            outcome = ToolFailure(
                "timeout", (TextBlock("Budget exhausted; not started."),)
            )
        elif tool.effect == "external" and (
            self._policies.get(tool.name, ToolPolicy()).authorization == "denied"
            or (
                self._policies.get(tool.name, ToolPolicy()).configured
                and self._policies.get(tool.name, ToolPolicy()).authorization
                != "allowed"
            )
        ):
            outcome = ToolFailure("not_authorized", (TextBlock("Not authorized."),))
        elif (
            tool.effect == "external"
            and not self._policies.get(tool.name, ToolPolicy()).configured
        ):
            outcome = ToolFailure(
                "configuration_required",
                (TextBlock("Configure this capability before use; not executed."),),
            )
        elif tool.effect == "external" and self._external_ledger is None:
            outcome = ToolFailure(
                "unavailable", (TextBlock("External ledger unavailable; not started."),)
            )
        else:
            try:
                validate_input(tool.input_schema, call.input)
            except ValueError as exc:
                outcome = ToolFailure("invalid_input", (TextBlock(str(exc)),))
            else:
                ledger_id = None
                if tool.effect == "external" and self._external_ledger is not None:
                    ledger_id = self._external_ledger.start(tool, call.input, context)
                if events is not None:
                    events.emit(ToolStarted(call.id, tool.name, tool.effect), envelope)
                executed = True
                unknown = False
                try:
                    outcome = tool.fn(
                        call.input,
                        replace(
                            context,
                            monotonic=self._clock.monotonic,
                            events=(
                                ProgressEvents(events, envelope, call.id, tool.name)
                                if tool.emits_progress
                                else None
                            ),
                            deadline=min(
                                context.deadline,
                                self._clock.monotonic() + tool.budget_s,
                            ),
                        ),
                    )
                except Exception as exc:
                    raise_if_rollback_pending(exc)
                    unknown = True
                    outcome = ToolFailure(
                        "timeout"
                        if isinstance(exc, TimeoutError)
                        else "execution_error",
                        (
                            TextBlock(
                                "Tool execution failed; inspect the operation result."
                            ),
                        ),
                    )
                if ledger_id is not None:
                    state = (
                        "unknown"
                        if unknown
                        or (
                            isinstance(outcome, ToolFailure)
                            and outcome.code == "timeout"
                        )
                        else "failed"
                        if isinstance(outcome, ToolFailure)
                        else "succeeded"
                    )
                    self._external_ledger.finish(ledger_id, state)
        content = tuple(outcome.content)
        if isinstance(outcome, ToolFailure):
            header = json.dumps(
                {
                    "ok": False,
                    "code": outcome.code,
                    "tool": call.name,
                    **(
                        {"memory_code": outcome.memory_code}
                        if outcome.memory_code
                        else {}
                    ),
                },
                separators=(",", ":"),
            )
            content = (TextBlock(header + "\n" + "\n".join(x.text for x in content)),)
        audit = self._redactor.redact_text("\n".join(block.text for block in content))
        digest = hashlib.sha256(audit.encode("utf-8")).hexdigest()
        original_bytes = len(audit.encode("utf-8"))
        truncated = len(audit) > self._limit
        model = audit
        if truncated:
            model = audit[: self._limit] + (
                f"\n[truncated original_bytes={original_bytes} sha256={digest}]"
            )
        if events is not None and executed:
            events.emit(
                ToolFinished(
                    call.id,
                    call.name,
                    "error" if isinstance(outcome, ToolFailure) else "ok",
                    outcome.code if isinstance(outcome, ToolFailure) else None,
                    model,
                    audit,
                    truncated,
                    original_bytes,
                    digest,
                    max(0, int((self._clock.monotonic() - started) * 1000)),
                    summary=outcome.summary or tool.name,
                    cost=outcome.cost,
                ),
                envelope,
            )
        return ToolExecution(
            ToolResultBlock(
                call.id, (TextBlock(model),), isinstance(outcome, ToolFailure)
            ),
            outcome.stop_reason,
            outcome.operation_id,
            audit
            if (
                tool is not None
                and tool.effect == "local_write"
                and isinstance(outcome, ToolSuccess)
                and not outcome.stop_reason
            )
            else None,
        )
