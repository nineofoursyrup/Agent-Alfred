"""The Agent loop: system prompt, working memory, one Step, no agent_log writes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from agent_alfred.clock import Clock
from agent_alfred.events import (
    EventEnvelope,
    EventPayload,
    SequencedEvent,
    StepFinished,
    StepStarted,
)
from agent_alfred.loop.budget import RunBudget, StepBudgetExceeded
from agent_alfred.messages import (
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    text_message,
)
from agent_alfred.model import (
    ModelClient,
    ModelRef,
    ModelRequest,
    ModelResult,
    StopReason,
)
from agent_alfred.outcomes import RunOutcome
from agent_alfred.settings import (
    CONTROLLED_FAILURE_TEXT,
    LOOP_NODE_ID,
    MAX_STEPS_REACHED_TEXT,
    OVERALL_DEADLINE_TEXT,
    Settings,
)
from agent_alfred.stream_fallback import OverallDeadlineExceeded
from agent_alfred.tools import ToolContext


@dataclass(frozen=True)
class LoopResult:
    outcome: RunOutcome
    reply: Message | None
    error: str | None
    step_count: int
    duration_ms: int
    model_results: tuple[ModelResult, ...] = field(default_factory=tuple)
    memory_telemetry: dict | None = None


class AssistantEvents(Protocol):
    """The event seam the loop actually uses."""

    def emit(
        self, payload: EventPayload, envelope: EventEnvelope | None = None
    ) -> SequencedEvent: ...

    def bind_origin(self, envelope: EventEnvelope | None) -> None: ...


class Assistant:
    def __init__(self, *, clock: Clock, settings: Settings):
        self._clock = clock
        self._settings = settings

    def respond(
        self,
        message: str,
        *,
        client: ModelClient,
        budget: RunBudget,
        working_memory: Sequence[Message],
        model: ModelRef,
        run_id: str,
        session_id: str | None,
        events: AssistantEvents | None = None,
        source: str = "cli",
        overall_deadline_s: float | None = None,
        memory=None,
        tools=None,
        tool_permission=None,
        tool_state=None,
        persona=None,
    ) -> LoopResult:
        started = self._clock.monotonic()
        overall_s = (
            self._settings.overall_deadline_s
            if overall_deadline_s is None
            else overall_deadline_s
        )
        overall_abs = None if overall_s is None else started + overall_s
        system = (
            TextBlock(self._settings.persona if persona is None else persona),
            TextBlock(f"Current local time: {self._clock.local_now().isoformat()}"),
        )
        user = text_message("user", message)
        transcript: list[Message] = [*working_memory, user]
        turns: list[Message] = []
        results: list[ModelResult] = []
        outcome: RunOutcome = "failed"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        pending_tool_step = None

        def note_not_sent(step_index):
            # Retain the stop fact before attempting any fallible ledger write.
            # Only the pending tool batch is affected, never an earlier batch
            # already included in a subsequent model request.
            if tool_state is not None and step_index is not None:
                steps = tool_state.setdefault("tool_model_not_sent_steps", [])
                if step_index not in steps:
                    steps.append(step_index)

        if (
            memory is not None
            and budget.remaining > 0
            and (overall_abs is None or self._clock.monotonic() < overall_abs)
        ):
            working_memory = memory.prepare(
                working_memory, system, model, tools.schemas() if tools else ()
            )
            gate = memory.evaluate(budget, working_memory, overall_abs)
            step_count = budget.used
            if gate is not None:
                if gate.failure_code is not None:
                    return LoopResult(
                        outcome="failed",
                        reply=text_message(
                            "assistant", "记忆检索资料不可用，本次未生成回答。"
                        ),
                        error=gate.failure_code,
                        step_count=step_count,
                        duration_ms=_duration_ms(started, self._clock.monotonic()),
                    )
                if gate.reference_text is not None:
                    transcript = [
                        *working_memory,
                        text_message("user", gate.reference_text),
                        user,
                    ]
        while True:
            if overall_abs is not None and self._clock.monotonic() >= overall_abs:
                outcome = "failed"
                reply = text_message("assistant", OVERALL_DEADLINE_TEXT)
                error = "overall_deadline"
                if tools is not None:
                    note_not_sent(pending_tool_step)
                    tools.stop_run(run_id, "overall_deadline")
                break
            try:
                lease = budget.reserve_step(LOOP_NODE_ID)
            except StepBudgetExceeded:
                outcome = "max_steps"
                if tools is not None:
                    note_not_sent(pending_tool_step)
                    tools.stop_run(run_id, "max_steps")
                reply = text_message("assistant", MAX_STEPS_REACHED_TEXT)
                break
            step_count = lease.step_index + 1
            envelope = EventEnvelope(
                ts=self._clock.monotonic(),
                run_id=run_id,
                session_id=session_id,
                step_index=lease.step_index,
                attempt_id=None,
                node_id=lease.node_id,
                source=source,
            )
            if memory is not None:
                reference_text = memory.reference_text()
                transcript = [*working_memory]
                if reference_text is not None:
                    transcript.append(text_message("user", reference_text))
                transcript.append(user)
                transcript.extend(turns)
            if events is not None:
                events.emit(
                    StepStarted(
                        step_index=lease.step_index,
                        system=system,
                        message_count=len(transcript),
                        tool_names=(
                            tuple(t.name for t in tools.schemas()) if tools else ()
                        ),
                        max_tokens=self._settings.max_tokens,
                    ),
                    envelope,
                )
            request = ModelRequest(
                model=model,
                system=system,
                messages=tuple(transcript),
                tools=tools.schemas() if tools else (),
                max_tokens=self._settings.max_tokens,
            )
            if memory is not None:
                request = memory.answer_request(request, turns, lease.step_index)
            bind = events.bind_origin if events is not None else None
            if bind is not None:
                bind(envelope)
            try:
                model_result = client.respond(
                    request,
                    events=events,
                    deadline=overall_abs,
                )
            except OverallDeadlineExceeded:
                outcome = "failed"
                reply = text_message("assistant", OVERALL_DEADLINE_TEXT)
                error = "overall_deadline"
                break
            finally:
                if bind is not None:
                    bind(None)
                if memory is not None:
                    memory.notify_inputs()
            results.append(model_result)
            pending_tool_step = None
            stop_reason: StopReason = "error"
            if model_result.response is not None:
                stop_reason = model_result.response.stop_reason
                reply = Message(role="assistant", blocks=model_result.response.blocks)
                transcript.append(reply)
                turns.append(reply)
                outcome = "completed"
                error = None
            else:
                err = model_result.final_error
                error = "model_error"
                if err is not None and err.code:
                    error = err.code
                outcome = "failed"
                reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
            if events is not None:
                events.emit(
                    StepFinished(
                        step_index=lease.step_index,
                        stop_reason=stop_reason,
                        duration_ms=_duration_ms(started, self._clock.monotonic()),
                    ),
                    envelope,
                )
            if model_result.response is not None and tools is not None:
                calls = [
                    block
                    for block in model_result.response.blocks
                    if isinstance(block, ToolCallBlock)
                ]
                if calls:
                    from agent_alfred.tools.metering import MeteringError

                    committed = [
                        a for a in model_result.attempts if a.outcome == "committed"
                    ]
                    if (
                        len(committed) != 1
                        or model_result.attempts[-1] is not committed[0]
                    ):
                        raise MeteringError("tool_identity_ambiguous")
                    batch_context = ToolContext(
                        run_id,
                        lease.step_index,
                        calls[0].id,
                        source,
                        overall_abs if overall_abs is not None else float("inf"),
                        session_id,
                        tool_permission,
                    )
                    tools.register_batch(calls, batch_context)
                    pending_tool_step = lease.step_index
                    tool_results = []
                    boundary = None
                    for index, call in enumerate(calls):
                        try:
                            execution = tools.execute(
                                call,
                                ToolContext(
                                    run_id,
                                    lease.step_index,
                                    call.id,
                                    source,
                                    overall_abs
                                    if overall_abs is not None
                                    else float("inf"),
                                    session_id,
                                    tool_permission,
                                ),
                                events=events,
                            )
                        except MeteringError:
                            note_not_sent(lease.step_index)
                            raise
                        tool_results.append(execution.block)
                        if (
                            execution.system_receipt is not None
                            and tool_state is not None
                        ):
                            tool_state.setdefault("system_receipts", []).append(
                                execution.system_receipt
                            )
                        if execution.stop_reason is not None:
                            boundary = execution.stop_reason
                            if (
                                boundary == "memory_delete_boundary"
                                and tool_state is not None
                            ):
                                tool_state.pop("system_receipts", None)
                            note_not_sent(lease.step_index)
                            tools.stop_batch(
                                calls[index + 1 :], batch_context, boundary
                            )
                            remaining = [pending.id for pending in calls[index + 1 :]]
                            for pending in calls[index + 1 :]:
                                tool_results.append(
                                    ToolResultBlock(
                                        pending.id,
                                        (
                                            TextBlock(
                                                json.dumps(
                                                    {
                                                        "ok": False,
                                                        "code": "unavailable",
                                                        "reason": boundary,
                                                    }
                                                )
                                            ),
                                        ),
                                        True,
                                    )
                                )
                            if tool_state is not None:
                                tool_state.update(
                                    finalization_reason=boundary,
                                    not_executed_call_ids=remaining,
                                )
                            reply = text_message(
                                "assistant",
                                "记忆删除已确认。"
                                "操作回执："
                                + execution.block.content[0].text
                                + "。未执行调用："
                                + ", ".join(remaining),
                            )
                            if boundary == "file_result_unverified":
                                operation_id = execution.operation_id
                                if tool_state is not None:
                                    tool_state["file_operation_id"] = operation_id
                                reply = text_message(
                                    "assistant",
                                    "文件结果待核验。操作编号："
                                    + operation_id
                                    + "；恢复操作 "
                                    + operation_id
                                    + "；未执行调用："
                                    + ", ".join(remaining),
                                )
                                outcome = "failed"
                                error = boundary
                            elif boundary in (
                                "memory_result_unverified",
                                "tool_result_unverified",
                            ):
                                reply = text_message(
                                    "assistant",
                                    ("搜索结果无法确认，可能已执行；本次后续调用已停止，未自动重试。操作编号："
                                     if boundary == "tool_result_unverified"
                                     else "操作结果待核验。操作编号：")
                                    + (execution.operation_id or "未记录"),
                                )
                                outcome = "failed"
                                error = boundary
                            else:
                                outcome = "completed"
                            break
                    tool_message = Message("user", tuple(tool_results))
                    turns.append(tool_message)
                    transcript.append(tool_message)
                    if boundary is not None:
                        break
                    continue
            break
        return LoopResult(
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=_duration_ms(started, self._clock.monotonic()),
            model_results=tuple(results),
        )


def _duration_ms(started: float, ended: float) -> int:
    return max(0, int((ended - started) * 1000))
