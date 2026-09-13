"""Immutable tool declarations and their public execution registry."""

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
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
    units: Decimal
    unit: str
    source: str = "reported"

    def __post_init__(self):
        if isinstance(self.units, bool) or not isinstance(
            self.units, (Decimal, str, int)
        ):
            raise ValueError("tool cost requires Decimal, integer or decimal text")
        value = Decimal(self.units)
        if not value.is_finite() or value < 0:
            raise ValueError("invalid tool cost")
        object.__setattr__(self, "units", value)


@dataclass(frozen=True)
class NotSentCost:
    """Trusted tool transport proved no business request was sent."""

    reason: Literal["http_not_sent", "transport_not_sent"] = "http_not_sent"


@dataclass(frozen=True)
class UnknownCost:
    reason: str = "not_reported"


@dataclass(frozen=True)
class ToolSuccess:
    content: Sequence[TextBlock]
    summary: str | None = None
    cost: ToolCost | UnknownCost | NotSentCost | None = None
    stop_reason: str | None = None
    operation_id: str | None = None
    audit_data: Mapping[str, Any] | None = None


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
    cost: ToolCost | UnknownCost | NotSentCost | None = None
    stop_reason: str | None = None
    operation_id: str | None = None
    memory_code: str | None = None
    audit_data: Mapping[str, Any] | None = None


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
    metering: object | None = None
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
    source_id: str = "builtin"
    capability_id: str | None = None
    cost_units: tuple[str, ...] = ()
    cost_sources: tuple[str, ...] = ()
    schema_mode: Literal["local", "mcp"] = "local"


@dataclass(frozen=True)
class ToolPolicy:
    configured: bool = False
    authorization: Literal["unset", "allowed", "denied"] = "unset"
    connection: Literal["unverified", "connected", "error"] = "unverified"
    unavailable_reason: str | None = None


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
        metering=None,
        external_block=None,
    ):
        for tool in tools:
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name) is None:
                raise ValueError("invalid tool name")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("duplicate tool name")
        if len({capability_identity(tool) for tool in tools}) != len(tools):
            raise ValueError("duplicate capability identity")
        for tool in tools:
            if not isinstance(tool.source_id, str) or not tool.source_id:
                raise ValueError("invalid source identity")
            if tool.capability_id is not None and (
                not isinstance(tool.capability_id, str) or not tool.capability_id
            ):
                raise ValueError("invalid capability identity")
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
            if tool.schema_mode == "local":
                validate_schema(tool.input_schema)
            elif tool.schema_mode != "mcp" or not tool.source_id.startswith("mcp:"):
                raise ValueError("invalid schema mode")
            if tool.schema_mode == "local" and tool.input_schema["type"] != "object":
                raise ValueError("tool parameters must be object")
            if any(
                key not in tool.input_schema.get("properties", {})
                for key in tool.summary_keys
            ):
                raise ValueError("summary_keys must name declared arguments")
        self._tools = tuple(
            replace(
                tool,
                input_schema=freeze(tool.input_schema),
                capability_id=tool.capability_id or tool.name,
            )
            for tool in tools
        )
        self._external_block = external_block
        self._metering = metering
        self._external_ledger = external_ledger
        self._redactor = redactor or Redactor(())
        if type(model_content_limit) is not int or model_content_limit < 1:
            raise ValueError("invalid projection limit")
        self._limit = model_content_limit
        self._clock = clock
        self._policies = freeze(policies or {})
        self._schemas = self._build_schemas()

    @property
    def external_block(self):
        return self._external_block

    def declarations(self):
        return self._tools

    def policy(self, name):
        return self._policies.get(name, ToolPolicy())

    def suspend_external(self, reason):
        self._external_block = reason
        self._schemas = self._build_schemas()

    def suspend_tools(self, names, reason):
        self._policies = freeze({
            **self._policies,
            **{name: replace(self.policy(name), unavailable_reason=reason)
               for name in names},
        })
        self._schemas = self._build_schemas()

    def with_policies(self, policies, *, external_block=None):
        return ToolRegistry(
            self._tools,
            clock=self._clock,
            policies=policies,
            model_content_limit=self._limit,
            redactor=self._redactor,
            external_ledger=self._external_ledger,
            metering=self._metering,
            external_block=external_block,
        )

    def catalog(self):
        rows = []
        exposed = {s.name for s in self._schemas}
        for tool in self._tools:
            policy = self.policy(tool.name)
            external = tool.effect == "external"
            rows.append(
                {
                    "identity": capability_identity(tool),
                    "name": tool.name,
                    "description": tool.description,
                    "source_id": tool.source_id,
                    "capability_id": tool.capability_id,
                    "effect": tool.effect,
                    "availability": policy.unavailable_reason
                    if external and policy.unavailable_reason
                    else "configured"
                    if not external or policy.configured
                    else "unconfigured",
                    "connection": policy.connection if external else "not_applicable",
                    "authorization": policy.authorization
                    if external
                    else "not_required",
                    "exposure": "hidden"
                    if tool.name not in exposed
                    else "guidance"
                    if external and not policy.configured
                    else "real",
                    "reason": self._external_block
                    if external and self._external_block
                    else policy.unavailable_reason
                    if external and policy.unavailable_reason
                    else "not_authorized"
                    if tool.name not in exposed
                    else "configuration_required"
                    if external and not policy.configured
                    else None,
                }
            )
        return rows

    def register_batch(self, calls, context):
        if self._metering is not None:
            self._metering.register(calls, context, {t.name: t for t in self._tools})

    def stop_run(self, run_id, reason):
        if self._metering is not None:
            self._metering.stop_run(run_id, reason)

    def stop_batch(self, calls, context, reason):
        if self._metering is not None:
            self._metering.stopped(calls, context, reason)

    def schemas(self):
        return self._schemas

    def _build_schemas(self):
        exposed = []
        for tool in self._tools:
            policy = self._policies.get(tool.name, ToolPolicy())
            if tool.effect == "external":
                if self._external_block or policy.unavailable_reason:
                    continue
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
        from agent_alfred.tools.metering import MeteringError

        try:
            return self._execute(call, context, events=events)
        except MeteringError:
            if self._metering is not None:
                try:
                    self._metering.not_sent(context)
                except MeteringError:
                    pass  # The terminal Run also retains this fact for recovery.
            raise

    def _execute(self, call, context, *, events=None):
        if self._metering is not None:
            self.register_batch([call], context)
            context = replace(context, metering=self._metering)
            prior = self._metering.prior(context)
            if prior is not None:
                return ToolExecution(
                    ToolResultBlock(
                        call.id,
                        (
                            TextBlock(
                                "原请求已有持久记录，未再次执行；原结果："
                                + prior["result"]
                                + "。原正文可在人工历史中核对。"
                            ),
                        ),
                        prior["result"] != "succeeded",
                    ),
                    stop_reason="tool_result_unverified"
                    if prior["result"] == "unknown"
                    else None,
                    operation_id=prior["operation_id"],
                )
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
        ledger_id = None
        unknown = False
        entered = False
        tool = next((tool for tool in self._tools if tool.name == call.name), None)
        if tool is None:
            outcome = ToolFailure("unknown_tool", (TextBlock("Unknown tool."),))
        elif tool.effect == "external" and self._external_block:
            outcome = ToolFailure("unavailable", (TextBlock(self._external_block),))
        elif tool.effect == "external" and self.policy(tool.name).unavailable_reason:
            outcome = ToolFailure(
                "unavailable", (TextBlock(self.policy(tool.name).unavailable_reason),)
            )
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
                if tool.schema_mode == "local":
                    validate_input(tool.input_schema, call.input)
                else:
                    from agent_alfred.mcp.transport import decode

                    if not isinstance(call.input, dict):
                        raise ValueError("MCP arguments must be a JSON object")
                    decode(
                        json.dumps(
                            call.input, ensure_ascii=False, allow_nan=False
                        ).encode()
                    )
            except ValueError as exc:
                outcome = ToolFailure("invalid_input", (TextBlock(str(exc)),))
            else:
                claim = None
                if tool.effect == "external" and self._external_ledger is not None:
                    claim = self._external_ledger.start(tool, call.input, context)
                    if claim.execution is not None:
                        return claim.execution
                    ledger_id = claim.row_id
                if claim is not None and claim.error is not None:
                    outcome = ToolFailure(
                        claim.error,
                        (
                            TextBlock(
                                "Existing operation differs or has no confirmed "
                                "receipt; inspect its result. Not started."
                            ),
                        ),
                    )
                else:
                    execution_context = replace(
                        context,
                        monotonic=self._clock.monotonic,
                        events=(
                            ProgressEvents(events, envelope, call.id, tool.name)
                            if tool.emits_progress
                            else None
                        ),
                        deadline=min(context.deadline, started + tool.budget_s),
                    )
                    try:
                        execution_context.checkpoint()
                        if events is not None:
                            # FanOut preparation may consume the remaining budget.
                            # Its publication boundary checks before allocating a
                            # sequence or committing any tool.started projection.
                            linearized = getattr(events, "emit_linearized", None)
                            if linearized is not None:
                                linearized(
                                    ToolStarted(call.id, tool.name, tool.effect),
                                    envelope,
                                    boundary=_start_boundary(execution_context),
                                    after_commit=lambda event: None,
                                )
                            else:
                                events.emit(
                                    ToolStarted(call.id, tool.name, tool.effect),
                                    envelope,
                                )
                            # Post-commit listeners (or a simple emitter) can also
                            # consume time. Never enter fn on an expired budget.
                            executed = True
                            execution_context.checkpoint()
                        if self._metering is not None:
                            self._metering.intent(context)
                        execution_context.checkpoint()
                        entered = True
                        outcome = tool.fn(call.input, execution_context)
                    except Exception as exc:
                        from agent_alfred.tools.metering import MeteringError

                        if isinstance(exc, MeteringError):
                            raise
                        raise_if_rollback_pending(exc)
                        unknown = entered
                        outcome = ToolFailure(
                            "timeout"
                            if isinstance(exc, TimeoutError)
                            else "execution_error",
                            (
                                TextBlock(
                                    "Tool execution failed; "
                                    "inspect the operation result."
                                    if unknown
                                    else "Budget or event preparation failed; "
                                    "not started."
                                ),
                            ),
                            stop_reason="tool_result_unverified"
                            if unknown and tool.effect == "external"
                            else None,
                            operation_id=(
                                f"{context.run_id}:{context.step_index}:{context.call_id}"
                                if unknown and tool.effect == "external"
                                else None
                            ),
                        )
                    except BaseException:
                        if ledger_id is not None:
                            try:
                                self._external_ledger.finish(
                                    ledger_id,
                                    "unknown" if entered else "failed",
                                    ToolExecution(
                                        ToolResultBlock(
                                            call.id,
                                            (
                                                TextBlock(
                                                    "Control interrupted; "
                                                    "result unverified."
                                                ),
                                            ),
                                            True,
                                        ),
                                        stop_reason="tool_result_unverified"
                                        if entered
                                        else None,
                                    ),
                                )
                            except BaseException:
                                pass  # The retained intent is recovered as unknown.
                        if self._metering is not None:
                            try:
                                self._metering.finish(
                                    context,
                                    entered=entered,
                                    result="unknown",
                                    reason="control_interrupted",
                                    cost={"kind": "unknown"}
                                    if tool.effect == "external"
                                    else {"kind": "not_billable"},
                                )
                            except BaseException:
                                self._metering.failed.set()
                        raise
                    else:
                        executed = True
        if self._metering is not None:
            from agent_alfred.tools.cost import project_tool_cost
            from agent_alfred.tools.metering import MeteringError

            # Business services can normalize storage exceptions into outcomes.
            # The accounting owner retains the stronger stop-execution signal.
            if self._metering.failed.is_set():
                raise MeteringError("metering_unconfirmed")

            self._metering.finish(
                context,
                entered=entered,
                result="unknown"
                if unknown
                or outcome.stop_reason
                in (
                    "file_result_unverified",
                    "memory_result_unverified",
                    "tool_result_unverified",
                )
                else "not_executed"
                if not entered
                and isinstance(outcome, ToolFailure)
                and (
                    outcome.code == "timeout"
                    or (tool is not None and tool.schema_mode == "mcp")
                )
                else "failed"
                if isinstance(outcome, ToolFailure)
                else "succeeded",
                reason=outcome.code
                if isinstance(outcome, ToolFailure)
                else outcome.stop_reason,
                cost=project_tool_cost(tool, outcome, entered),
                operation_id=outcome.operation_id,
            )
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
        execution = ToolExecution(
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

        if ledger_id is not None:
            state = (
                "unknown"
                if unknown
                or outcome.stop_reason == "tool_result_unverified"
                or (
                    executed
                    and isinstance(outcome, ToolFailure)
                    and outcome.code == "timeout"
                    and entered
                )
                else "failed"
                if isinstance(outcome, ToolFailure)
                else "succeeded"
            )
            self._external_ledger.finish(ledger_id, state, execution)
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
                    audit_data=self._redactor.redact_jsonable(outcome.audit_data),
                ),
                envelope,
            )
        return execution


@contextmanager
def _start_boundary(context):
    context.checkpoint()
    yield


def capability_identity(tool):
    return json.dumps(
        [tool.source_id, tool.capability_id or tool.name], separators=(",", ":")
    )
