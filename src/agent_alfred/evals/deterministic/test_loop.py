"""The Assistant loop, driven by ScriptedModel. No network, no keys."""

from __future__ import annotations

from pathlib import Path

from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, EventEnvelope, FanOutSink
from agent_alfred.loop.assistant import Assistant
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ModelRef, ScriptedModel
from agent_alfred.settings import MAX_STEPS_REACHED_TEXT, Settings


def _assistant(
    max_steps: int = 8,
) -> tuple[Assistant, FakeClock, FanOutSink, CapturingSink]:
    clock = FakeClock()
    sink = CapturingSink(flush_at_run_end=True)
    fanout = FanOutSink([sink], process_instance_id="proc-test")
    assistant = Assistant(clock=clock, settings=Settings(max_steps=max_steps))
    return assistant, clock, fanout, sink


def test_scripted_model_drives_the_same_loop_as_production() -> None:
    assistant, _clock, fanout, sink = _assistant()
    model = ScriptedModel(["hello from script"])
    result = assistant.respond(
        "hi",
        client=model,
        budget=RunBudget(8),
        working_memory=(),
        model=ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash"),
        run_id="run-1",
        session_id="s1",
        events=fanout,
    )
    assert result.outcome == "completed"
    assert result.reply is not None
    assert message_plain_text(result.reply) == "hello from script"
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.system is not None
    system_text = "\n".join(block.text for block in request.system)
    assert "Alfred" in system_text
    assert "Current local time" in system_text
    names = [event.payload.name for event in sink.events]
    assert names == ["step.started", "step.finished"]


def test_max_steps_does_not_call_the_model() -> None:
    assistant, _clock, fanout, _sink = _assistant(max_steps=0)
    model = ScriptedModel(["should not run"])
    result = assistant.respond(
        "hi",
        client=model,
        budget=RunBudget(0),
        working_memory=(),
        model=ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash"),
        run_id="run-1",
        session_id="s1",
        events=fanout,
    )
    assert result.outcome == "max_steps"
    assert result.reply is not None
    assert message_plain_text(result.reply) == MAX_STEPS_REACHED_TEXT
    assert model.requests == []


def test_loop_package_contains_no_vendor_names() -> None:
    root = Path(__file__).resolve().parents[2] / "loop"
    banned = ("openai", "anthropic", "opencode", "deepseek", "tavily")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, f"{path} contains {word!r}"


def test_fanout_assigns_seq_in_commit_order() -> None:
    sink = CapturingSink()
    fanout = FanOutSink([sink], process_instance_id="p")
    from agent_alfred.events import RunStarted

    first = fanout.emit(
        RunStarted(purpose="chat"),
        EventEnvelope(0.0, "r", None, None, None, None),
    )
    second = fanout.emit(
        RunStarted(purpose="chat"),
        EventEnvelope(0.0, "r", None, None, None, None),
    )
    assert (first.seq, second.seq) == (1, 2)
    assert [event.seq for event in sink.events] == [1, 2]
    # No flush_at_run_end sink exists, so the barrier must not claim the trace
    # is complete -- durability is nobody's promise here.
    incomplete, reason = fanout.flush_barrier()
    assert incomplete is True
    assert reason is not None and "no flush_at_run_end sink" in reason
