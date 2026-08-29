"""Stream fallback, shared StepLease, and the run-level absolute deadline."""

from __future__ import annotations

from agent_alfred.clock import FakeClock
from agent_alfred.loop.assistant import Assistant
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import TextBlock, message_plain_text
from agent_alfred.model import (
    AttemptRecord,
    ModelError,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    Usage,
)
from agent_alfred.settings import Settings
from agent_alfred.stream_fallback import StreamFallback

_MODEL = ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash")


class RecordingInner:
    """Inner ModelClient that records deadline/stream and consumes a script."""

    def __init__(self, script: list[ModelResult], *, clock: FakeClock | None = None):
        self._script = list(script)
        self.calls: list[dict] = []
        self._clock = clock

    def respond(
        self,
        request: ModelRequest,
        *,
        events=None,
        deadline: float | None = None,
    ) -> ModelResult:
        del events
        self.calls.append({"deadline": deadline, "model": request.model})
        if self._clock is not None:
            self._clock.monotonic_value += 5
        if not self._script:
            raise AssertionError("RecordingInner has no remaining responses")
        return self._script.pop(0)


def _aborted_stream() -> ModelResult:
    error = ModelError(
        retryable=True,
        status_code=None,
        body_excerpt="incomplete_stream",
        attempt_id="a-stream",
        code="incomplete_stream",
    )
    return ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id="a-stream",
                streamed=True,
                outcome="aborted",
                usage=Usage(output_tokens=7),
                error=error,
            ),
        ),
        response=None,
        final_error=error,
    )


def _committed(text: str, *, attempt_id: str = "a-fallback") -> ModelResult:
    return ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id=attempt_id,
                streamed=False,
                outcome="committed",
                usage=Usage(total_input_tokens=3, output_tokens=2),
            ),
        ),
        response=ModelResponse(
            blocks=(TextBlock(text),),
            stop_reason="end_turn",
            model=_MODEL,
        ),
        final_error=None,
    )


def test_stream_interrupt_falls_back_on_the_same_step_lease() -> None:
    clock = FakeClock()
    inner = RecordingInner([_aborted_stream(), _committed("recovered")], clock=clock)
    client = StreamFallback(
        inner,
        clock=clock,
        stream=True,
        stream_fallback=True,
        per_attempt_timeout_s=8,
    )
    assistant = Assistant(clock=clock, settings=Settings(max_steps=8))
    budget = RunBudget(8)
    result = assistant.respond(
        "hi",
        client=client,
        budget=budget,
        working_memory=(),
        model=_MODEL,
        run_id="run-1",
        session_id="s1",
        overall_deadline_s=10,
    )
    assert result.outcome == "completed"
    assert result.reply is not None
    assert message_plain_text(result.reply) == "recovered"
    assert budget.used == 1
    assert len(inner.calls) == 2
    attempts = result.model_results[0].attempts
    assert [record.outcome for record in attempts] == ["aborted", "committed"]
    assert attempts[0].attempt_id != attempts[1].attempt_id
    assert attempts[0].usage.output_tokens == 7


def test_disabled_fallback_does_not_issue_a_non_stream_attempt() -> None:
    clock = FakeClock()
    inner = RecordingInner([_aborted_stream(), _committed("should-not")])
    client = StreamFallback(
        inner,
        clock=clock,
        stream=True,
        stream_fallback=False,
        per_attempt_timeout_s=8,
    )
    assistant = Assistant(
        clock=clock, settings=Settings(max_steps=8, stream_fallback=False)
    )
    budget = RunBudget(8)
    result = assistant.respond(
        "hi",
        client=client,
        budget=budget,
        working_memory=(),
        model=_MODEL,
        run_id="run-1",
        session_id="s1",
        overall_deadline_s=10,
    )
    assert result.outcome == "failed"
    assert len(inner.calls) == 1
    assert budget.used == 1


def test_attempt_deadline_is_min_of_remaining_overall_and_per_attempt() -> None:
    clock = FakeClock(monotonic_value=0)
    inner = RecordingInner([_aborted_stream(), _committed("ok")], clock=clock)
    client = StreamFallback(
        inner,
        clock=clock,
        stream=True,
        stream_fallback=True,
        per_attempt_timeout_s=8,
    )
    client.respond(
        ModelRequest(model=_MODEL, system=None, messages=()),
        deadline=10,
    )
    assert inner.calls[0]["deadline"] == 8
    assert inner.calls[1]["deadline"] == 10


def test_overall_deadline_does_not_reset_between_attempts() -> None:
    clock = FakeClock(monotonic_value=0)
    inner = RecordingInner([_aborted_stream(), _committed("ok")], clock=clock)
    client = StreamFallback(
        inner,
        clock=clock,
        stream=True,
        stream_fallback=True,
        per_attempt_timeout_s=8,
    )
    assistant = Assistant(
        clock=clock,
        settings=Settings(overall_deadline_s=10, per_attempt_timeout_s=8),
    )
    assistant.respond(
        "hi",
        client=client,
        budget=RunBudget(8),
        working_memory=(),
        model=_MODEL,
        run_id="run-1",
        session_id="s1",
        overall_deadline_s=10,
    )
    # start=0, overall abs=10. Attempt 1: min(10, 0+8)=8, then clock += 5.
    # Attempt 2: min(10, 5+8)=10. A reset would yield 5+10=15 or 5+8=13.
    assert inner.calls[0]["deadline"] == 8
    assert inner.calls[1]["deadline"] == 10


def test_expired_overall_deadline_sends_zero_network_requests() -> None:
    clock = FakeClock(monotonic_value=100)
    inner = RecordingInner([_committed("nope")])
    client = StreamFallback(
        inner,
        clock=clock,
        stream=True,
        stream_fallback=True,
        per_attempt_timeout_s=8,
    )
    assistant = Assistant(clock=clock, settings=Settings(overall_deadline_s=10))
    budget = RunBudget(8)
    result = assistant.respond(
        "hi",
        client=client,
        budget=budget,
        working_memory=(),
        model=_MODEL,
        run_id="run-1",
        session_id="s1",
        overall_deadline_s=0,
    )
    assert result.outcome == "failed"
    assert inner.calls == []
    assert budget.used == 0


def test_scripted_model_still_drives_the_loop_without_the_fallback_wrapper() -> None:
    clock = FakeClock()
    assistant = Assistant(clock=clock, settings=Settings())
    model = ScriptedModel(["hello from script"])
    result = assistant.respond(
        "hi",
        client=model,
        budget=RunBudget(8),
        working_memory=(),
        model=_MODEL,
        run_id="run-1",
        session_id="s1",
    )
    assert result.outcome == "completed"
    assert message_plain_text(result.reply) == "hello from script"
