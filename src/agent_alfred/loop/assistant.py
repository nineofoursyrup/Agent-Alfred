"""The Agent loop: system prompt, working memory, one Step, no agent_log writes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agent_alfred.clock import Clock
from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    StepFinished,
    StepStarted,
)
from agent_alfred.loop.budget import RunBudget, StepBudgetExceeded
from agent_alfred.memory.retrieval_gate import evaluate as evaluate_retrieval_gate
from agent_alfred.messages import Message, TextBlock, text_message
from agent_alfred.model import ModelClient, ModelRef, ModelRequest, ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.settings import (
    CONTROLLED_FAILURE_TEXT,
    LOOP_NODE_ID,
    MAX_STEPS_REACHED_TEXT,
    OVERALL_DEADLINE_TEXT,
    Settings,
)
from agent_alfred.stream_fallback import OverallDeadlineExceeded


@dataclass(frozen=True)
class LoopResult:
    outcome: RunOutcome
    reply: Message | None
    error: str | None
    step_count: int
    duration_ms: int
    model_results: tuple[ModelResult, ...] = field(default_factory=tuple)


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
        events: FanOutSink | None = None,
        source: str = "cli",
        stream: bool = False,
        overall_deadline_s: float | None = None,
        per_attempt_timeout_s: float | None = None,
    ) -> LoopResult:
        del per_attempt_timeout_s
        started = self._clock.monotonic()
        overall_s = (
            self._settings.overall_deadline_s
            if overall_deadline_s is None
            else overall_deadline_s
        )
        overall_abs = None if overall_s is None else started + overall_s
        system = (
            TextBlock(self._settings.persona),
            TextBlock(f"Current local time: {self._clock.local_now().isoformat()}"),
        )
        user = text_message("user", message)
        transcript: list[Message] = [*working_memory, user]
        evaluate_retrieval_gate(transcript)
        results: list[ModelResult] = []
        outcome: RunOutcome = "failed"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        while True:
            if overall_abs is not None and self._clock.monotonic() >= overall_abs:
                outcome = "failed"
                reply = text_message("assistant", OVERALL_DEADLINE_TEXT)
                error = "overall_deadline"
                break
            try:
                lease = budget.reserve_step(LOOP_NODE_ID)
            except StepBudgetExceeded:
                outcome = "max_steps"
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
            if events is not None:
                events.emit(
                    StepStarted(
                        step_index=lease.step_index,
                        system=system,
                        message_count=len(transcript),
                        max_tokens=self._settings.max_tokens,
                    ),
                    envelope,
                )
            request = ModelRequest(
                model=model,
                system=system,
                messages=tuple(transcript),
                max_tokens=self._settings.max_tokens,
            )
            try:
                model_result = client.respond(
                    request,
                    events=events,
                    deadline=overall_abs,
                    stream=stream,
                )
            except OverallDeadlineExceeded:
                outcome = "failed"
                reply = text_message("assistant", OVERALL_DEADLINE_TEXT)
                error = "overall_deadline"
                break
            results.append(model_result)
            stop_reason = "error"
            if model_result.response is not None:
                stop_reason = model_result.response.stop_reason
                reply = Message(
                    role="assistant", blocks=model_result.response.blocks
                )
                transcript.append(reply)
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
                        duration_ms=_duration_ms(
                            started, self._clock.monotonic()
                        ),
                    ),
                    envelope,
                )
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
