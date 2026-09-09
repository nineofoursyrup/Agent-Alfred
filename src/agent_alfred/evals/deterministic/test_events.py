"""EventSink failures must not steer the Run (issue #3 failure matrix)."""

from __future__ import annotations

import dis
import inspect
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from types import CodeType

import pytest

import agent_alfred.events as events_module
from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
    interrupt_instruction_once,
    interrupt_py_return_once,
)
from agent_alfred.events import (
    BestEffortFlushResult,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    Notice,
    PostCommit,
    ProcessFatalSinkError,
    RunStarted,
    SequencedEvent,
    UnsequencedEvent,
    event_json_default,
)
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink

SECRET = "supersecret-key-value"


_GATE_DECISION = (
    '{"retrieve":true,"query":"runtime-fixture",'
    '"reason_code":"conservative_retrieve"}'
)


class _RunStartedFailingSink(CapturingSink):
    def commit(self, prepared: object, event: SequencedEvent) -> None:
        super().commit(prepared, event)
        if isinstance(event.payload, RunStarted):
            raise RuntimeError(f"{self.name} failed")


class _SyntheticCommitControl(BaseException):
    """Non-stdlib process control used to prove the BaseException boundary."""




@contextmanager
def _interrupt_instruction_sequence(
    code: CodeType,
    interruptions: tuple[tuple[int, BaseException], ...],
) -> Iterator[list[tuple[int, BaseException]]]:
    """Raise a prescribed sequence at exact instructions in one code object."""
    pending = list(interruptions)

    with claimed_monitoring_tool(
        "fanout-recovery-sequence-test", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code: CodeType, actual_offset: int) -> None:
            if not pending or actual_code is not code:
                return
            offset, failure = pending[0]
            if actual_offset != offset:
                return
            pending.pop(0)
            raise failure

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        yield pending


def test_instruction_monitoring_registration_failure_releases_tool_id() -> None:
    """A rejected callback registration cannot consume all monitoring slots."""
    script = inspect.cleandoc(
        """
        import sys

        from agent_alfred.evals.deterministic.test_events import (
            interrupt_instruction_once,
        )

        class RegistrationRejected(BaseException):
            pass

        def reject_registration(event, args):
            del args
            if event == "sys.monitoring.register_callback":
                raise RegistrationRejected("callback registration rejected")

        sys.addaudithook(reject_registration)
        try:
            with interrupt_instruction_once(
                (lambda: None).__code__, 0, KeyboardInterrupt("unused")
            ):
                raise AssertionError("registration should fail before the body")
        except RegistrationRejected:
            pass
        else:
            raise AssertionError("registration rejection did not propagate")

        claimed = [sys.monitoring.get_tool(tool_id) for tool_id in range(6)]
        assert claimed == [None] * 6, claimed
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_claimed_monitoring_tool_clears_state_after_body_control_exit() -> None:
    """A test-body control exit leaves no callback or event configuration."""
    failure = _SyntheticCommitControl("test body interrupted")
    claimed: list[int] = []

    def target() -> None:
        return None

    def callback(code: CodeType, offset: int) -> None:
        del code, offset

    with pytest.raises(_SyntheticCommitControl) as raised:
        with claimed_monitoring_tool(
            "body-control-test", local_codes=(target.__code__,)
        ) as tool_id:
            claimed.append(tool_id)
            sys.monitoring.register_callback(
                tool_id, sys.monitoring.events.INSTRUCTION, callback
            )
            sys.monitoring.set_events(
                tool_id, sys.monitoring.events.INSTRUCTION
            )
            sys.monitoring.set_local_events(
                tool_id, target.__code__, sys.monitoring.events.INSTRUCTION
            )
            raise failure

    assert raised.value is failure
    tool_id = claimed[0]
    assert sys.monitoring.get_tool(tool_id) is None
    assert sys.monitoring.get_events(tool_id) == sys.monitoring.events.NO_EVENTS
    assert (
        sys.monitoring.get_local_events(tool_id, target.__code__)
        == sys.monitoring.events.NO_EVENTS
    )

    with claimed_monitoring_tool(
        "body-control-reuse-test", local_codes=(target.__code__,)
    ) as reused_tool_id:
        assert reused_tool_id == tool_id
        assert (
            sys.monitoring.get_local_events(reused_tool_id, target.__code__)
            == sys.monitoring.events.NO_EVENTS
        )
        previous = sys.monitoring.register_callback(
            reused_tool_id, sys.monitoring.events.INSTRUCTION, callback
        )
        assert previous is None


def test_claimed_monitoring_tool_keeps_first_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later free failure cannot replace an earlier cleanup failure."""
    clear_failure = _SyntheticCommitControl("clear monitoring tool failed")
    free_failure = SystemExit("free monitoring tool failed after effect")
    claimed: list[int] = []
    real_free = sys.monitoring.free_tool_id

    def target() -> None:
        return None

    def reject_clear(tool_id: int) -> None:
        del tool_id
        raise clear_failure

    def interrupt_after_free(tool_id: int) -> None:
        real_free(tool_id)
        raise free_failure

    monkeypatch.setattr(sys.monitoring, "clear_tool_id", reject_clear)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", interrupt_after_free)
    with pytest.raises(_SyntheticCommitControl) as raised:
        with claimed_monitoring_tool(
            "cleanup-failure-test", local_codes=(target.__code__,)
        ) as tool_id:
            claimed.append(tool_id)
            sys.monitoring.set_local_events(
                tool_id, target.__code__, sys.monitoring.events.INSTRUCTION
            )

    assert raised.value is clear_failure
    tool_id = claimed[0]
    assert sys.monitoring.get_tool(tool_id) is None
    assert (
        sys.monitoring.get_local_events(tool_id, target.__code__)
        == sys.monitoring.events.NO_EVENTS
    )


def test_claimed_monitoring_tool_preserves_body_failure_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup diagnostics cannot replace a control exit from the test body."""
    body_failure = _SyntheticCommitControl("test body interrupted")
    cleanup_failure = SystemExit("clear monitoring tool interrupted")
    claimed: list[int] = []

    def reject_clear(tool_id: int) -> None:
        del tool_id
        raise cleanup_failure

    monkeypatch.setattr(sys.monitoring, "clear_tool_id", reject_clear)
    with pytest.raises(_SyntheticCommitControl) as raised:
        with claimed_monitoring_tool("body-and-cleanup-failure-test") as tool_id:
            claimed.append(tool_id)
            raise body_failure

    assert raised.value is body_failure
    assert repr(cleanup_failure) in (body_failure.__notes__ or [])[-1]
    assert sys.monitoring.get_tool(claimed[0]) is None


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_claimed_monitoring_tool_recovers_use_control_exit(
    monkeypatch: pytest.MonkeyPatch, after_effect: bool
) -> None:
    """Only a tool ID actually claimed by this attempt may be reclaimed."""
    failure = _SyntheticCommitControl("tool claim interrupted")
    tool_id = next(
        candidate
        for candidate in range(6)
        if sys.monitoring.get_tool(candidate) is None
    )
    name = "claim-control-test"
    freed: list[int] = []
    real_use = sys.monitoring.use_tool_id
    real_clear = sys.monitoring.clear_tool_id
    real_free = sys.monitoring.free_tool_id

    def interrupt_claim(candidate: int, claimed_name: str) -> None:
        if after_effect:
            real_use(candidate, claimed_name)
        raise failure

    def record_free(candidate: int) -> None:
        freed.append(candidate)
        real_free(candidate)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", interrupt_claim)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", record_free)
    try:
        with pytest.raises(_SyntheticCommitControl) as raised:
            with claimed_monitoring_tool(name):
                raise AssertionError("the interrupted claim cannot enter the body")

        assert raised.value is failure
        assert sys.monitoring.get_tool(tool_id) is None
        assert freed == ([tool_id] if after_effect else [])
    finally:
        if sys.monitoring.get_tool(tool_id) == name:
            real_clear(tool_id)
            real_free(tool_id)


def test_returned_monitoring_claim_does_not_probe_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed claim is durable even if ownership reads are unavailable."""
    probe_failure = _SyntheticCommitControl("ownership probe interrupted")
    claimed: list[int] = []
    probe_calls: list[int] = []
    name = "returned-claim-test"
    real_get = sys.monitoring.get_tool
    real_clear = sys.monitoring.clear_tool_id
    real_free = sys.monitoring.free_tool_id

    def target() -> None:
        return None

    def callback(code: CodeType, offset: int) -> None:
        del code, offset

    def interrupt_probe(tool_id: int) -> str | None:
        probe_calls.append(tool_id)
        raise probe_failure

    try:
        with monkeypatch.context() as patch:
            with claimed_monitoring_tool(
                name, local_codes=(target.__code__,)
            ) as tool_id:
                claimed.append(tool_id)
                sys.monitoring.register_callback(
                    tool_id, sys.monitoring.events.INSTRUCTION, callback
                )
                sys.monitoring.set_events(
                    tool_id, sys.monitoring.events.INSTRUCTION
                )
                sys.monitoring.set_local_events(
                    tool_id, target.__code__, sys.monitoring.events.INSTRUCTION
                )
                patch.setattr(sys.monitoring, "get_tool", interrupt_probe)

        assert probe_calls == []
        tool_id = claimed[0]
        assert real_get(tool_id) is None
        assert sys.monitoring.get_events(tool_id) == sys.monitoring.events.NO_EVENTS
        assert (
            sys.monitoring.get_local_events(tool_id, target.__code__)
            == sys.monitoring.events.NO_EVENTS
        )
    finally:
        if claimed and real_get(claimed[0]) == name:
            sys.monitoring.set_local_events(
                claimed[0], target.__code__, sys.monitoring.events.NO_EVENTS
            )
            real_clear(claimed[0])
            real_free(claimed[0])


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_unknown_monitoring_claim_retries_one_interrupted_probe(
    monkeypatch: pytest.MonkeyPatch, after_effect: bool
) -> None:
    """One failed ownership observation cannot strand an uncertain claim."""
    claim_failure = _SyntheticCommitControl("tool claim interrupted")
    probe_failure = SystemExit("ownership probe interrupted")
    tool_id = next(
        candidate
        for candidate in range(6)
        if sys.monitoring.get_tool(candidate) is None
    )
    name = "unknown-claim-test"
    probe_calls: list[int] = []
    freed: list[int] = []
    real_get = sys.monitoring.get_tool
    real_use = sys.monitoring.use_tool_id
    real_clear = sys.monitoring.clear_tool_id
    real_free = sys.monitoring.free_tool_id

    def probe(candidate: int) -> str | None:
        probe_calls.append(candidate)
        if len(probe_calls) == 1:
            raise probe_failure
        return real_get(candidate)

    def interrupt_claim(candidate: int, claimed_name: str) -> None:
        if after_effect:
            real_use(candidate, claimed_name)
        monkeypatch.setattr(sys.monitoring, "get_tool", probe)
        raise claim_failure

    def record_free(candidate: int) -> None:
        freed.append(candidate)
        real_free(candidate)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", interrupt_claim)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", record_free)
    try:
        with pytest.raises(_SyntheticCommitControl) as raised:
            with claimed_monitoring_tool(name):
                raise AssertionError("the interrupted claim cannot enter the body")

        assert raised.value is claim_failure
        assert probe_calls == [tool_id, tool_id]
        assert real_get(tool_id) is None
        assert freed == ([tool_id] if after_effect else [])
        assert repr(probe_failure) in (claim_failure.__notes__ or [])[-1]
    finally:
        if real_get(tool_id) == name:
            real_clear(tool_id)
            real_free(tool_id)


class BoomPrepareSink:
    """prepare() always raises. A disabled sink must not be retried."""

    def __init__(self, *, name: str = "boom"):
        self.name = name
        self.flush_at_run_end = False
        self.prepare_calls = 0
        self.commit_calls = 0
        self.exceptions: list[BaseException] = []

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        self.prepare_calls += 1
        exc = RuntimeError(f"cannot prepare {SECRET}")
        self.exceptions.append(exc)
        raise exc

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event
        self.commit_calls += 1

    def flush(self, run_id: str):
        del run_id
        from agent_alfred.events import BestEffortFlushResult

        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


class _CountingCloseSink:
    """A sink that counts close() calls and refuses the first N of them."""

    def __init__(self, *, name: str, close_failures: int = 0):
        self.name = name
        self.flush_at_run_end = False
        self.close_calls = 0
        self._close_failures = close_failures

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event

    def flush(self, run_id: str) -> BestEffortFlushResult:
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self._close_failures:
            raise RuntimeError(f"{self.name} close refused")


class _RetryableCloseSink(_CountingCloseSink):
    """A sink that reports incomplete until its close is allowed to finish."""

    def __init__(self, *, name: str):
        super().__init__(name=name)
        self.close_complete = False

    def close(self) -> bool:
        self.close_calls += 1
        return self.close_complete


class _CountingTimedCloseSink(_CountingCloseSink):
    """A timed-close capability whose successful effect is observable."""

    def close_with_timeout(self, timeout: float | None = None) -> None:
        del timeout
        self.close()


class _PublishedCloseSink(_CountingCloseSink):
    """A non-idempotent close that publishes completion before returning."""

    def __init__(self, *, name: str):
        super().__init__(name=name)
        self.close_complete = False

    def close(self) -> None:
        self.close_calls += 1
        self.close_complete = True

    def close_completed(self) -> bool:
        return self.close_complete


def _fanout_close_return_instruction(method_name: str) -> int:
    """The store immediately after one sink close call returns."""
    instructions = tuple(dis.get_instructions(FanOutSink.close))
    method_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == method_name
    )
    return next(
        instruction.offset
        for instruction in instructions[method_load:]
        if instruction.opname == "STORE_FAST" and instruction.argval == "closed"
    )


@pytest.mark.parametrize(
    ("sink_type", "method_name"),
    (
        (_CountingCloseSink, "close"),
        (_CountingTimedCloseSink, "close_with_timeout"),
    ),
    ids=("legacy", "timed"),
)
def test_fanout_close_return_edge_does_not_reclose_a_successful_sink(
    sink_type, method_name: str
) -> None:
    """A returned close effect owns progress before Python stores the result."""
    sink = sink_type(name="counted")
    fanout = FanOutSink([sink], process_instance_id="proc-close-return")
    failure = SystemExit("close returned before progress was stored")
    target = _fanout_close_return_instruction(method_name)

    with interrupt_instruction_once(FanOutSink.close.__code__, target, failure):
        with pytest.raises(SystemExit) as caught:
            fanout.close(timeout=1.0)

    assert caught.value is failure
    assert sink.close_calls == 1
    assert fanout.close(timeout=1.0) is True
    assert sink.close_calls == 1, "the successful sink was closed twice"


def test_fanout_observes_sink_completion_after_its_py_return_is_interrupted() -> None:
    sink = _PublishedCloseSink(name="published-close")
    fanout = FanOutSink([sink], process_instance_id="proc-close-published")
    failure = SystemExit("sink returned after publishing close completion")

    with interrupt_py_return_once(
        "fanout-sink-close-return", sink.close.__code__, failure
    ) as armed:
        with pytest.raises(SystemExit) as caught:
            fanout.close()

    assert armed == [False]
    assert caught.value is failure
    assert sink.close_calls == 1
    assert fanout.close() is True
    assert sink.close_calls == 1, "published completion was ignored"


def test_fanout_reobserves_completion_when_the_probe_return_is_interrupted() -> None:
    class AmbiguousCompletionSink(_PublishedCloseSink):
        def __init__(self) -> None:
            super().__init__(name="ambiguous-completion")
            self.failure = RuntimeError("close returned ambiguously")

        def close(self) -> None:
            super().close()
            raise self.failure

    sink = AmbiguousCompletionSink()
    fanout = FanOutSink([sink], process_instance_id="proc-close-observation")
    probe_failure = SystemExit("completion probe return interrupted")

    with interrupt_py_return_once(
        "fanout-completion-probe-return",
        sink.close_completed.__code__,
        probe_failure,
    ) as armed:
        with pytest.raises(RuntimeError) as caught:
            fanout.close()

    assert armed == [False]
    assert caught.value is sink.failure
    assert sink.close_calls == 1
    assert fanout.close() is True
    assert sink.close_calls == 1


@pytest.mark.parametrize("interrupt_retirement", [False, True])
def test_fanout_close_retirement_keeps_the_original_failure(
    interrupt_retirement: bool,
) -> None:
    """Completion bookkeeping cannot replace the failed close's exception."""
    failure = RuntimeError("close failed after publishing completion")

    class FailedCompletedSink(_PublishedCloseSink):
        def close(self) -> None:
            super().close()
            raise failure

    sink = FailedCompletedSink(name="failed-completed")
    fanout = FanOutSink([sink], process_instance_id="proc-close-retirement")
    interruption = SystemExit("retirement returned before failure propagated")
    boundary = (
        interrupt_py_return_once(
            "fanout-retirement-return",
            events_module._SinkCloseProgress.retire.__code__,
            interruption,
        )
        if interrupt_retirement
        else nullcontext(None)
    )
    with boundary as armed:
        with pytest.raises(BaseException) as caught:
            fanout.close()

    if interrupt_retirement:
        assert armed == [False], "retirement return was not reached"
    assert sink.close_calls == 1
    assert caught.value is failure
    assert fanout.close() is True
    assert sink.close_calls == 1, "the completed sink was closed again"


def test_fanout_close_resumes_per_sink_without_reclosing_succeeded_sinks() -> None:
    """A sink that failed to close is the only one the retry may ask again.

    The old close() walked the whole list on every attempt: a sink that
    raised left everything before it closed and everything after it open,
    and the retry closed the already-succeeded sinks a second time on its
    way back to the failure -- so the FanOut as a whole could report itself
    finished with one sink that had never closed. Close progress is per
    sink instead: a sink's bit moves only after its own close() has
    returned, a retry asks only the sinks that never succeeded, the failing
    sink's exception propagates unchanged, and the FanOut stays unfinished
    until the last sink has confirmed.
    """
    good = _CountingCloseSink(name="good")
    flaky = _CountingCloseSink(name="flaky", close_failures=1)
    tail = _CountingCloseSink(name="tail")
    fanout = FanOutSink([good, flaky, tail], process_instance_id="proc-close")

    with pytest.raises(RuntimeError, match="flaky close refused"):
        fanout.close()
    assert good.close_calls == 1
    assert flaky.close_calls == 1

    fanout.close()
    assert good.close_calls == 1, "a sink that closed is not closed again"
    assert flaky.close_calls == 2, "the failed sink is the one retried"
    assert tail.close_calls == 1, "the sink after the failure is still closed"

    # Fully confirmed, a further close asks nobody.
    fanout.close()
    assert (good.close_calls, flaky.close_calls, tail.close_calls) == (1, 2, 1)


def test_fanout_close_retries_sinks_that_report_incomplete() -> None:
    """False is an unfinished close; legacy None remains a confirmed close."""
    legacy = _CountingCloseSink(name="legacy")
    draining = _RetryableCloseSink(name="draining")
    tail = _CountingCloseSink(name="tail")
    fanout = FanOutSink(
        [legacy, draining, tail], process_instance_id="proc-retryable-close"
    )

    assert fanout.close() is False
    assert (legacy.close_calls, draining.close_calls, tail.close_calls) == (1, 1, 1)

    draining.close_complete = True
    assert fanout.close() is True
    assert (legacy.close_calls, draining.close_calls, tail.close_calls) == (1, 2, 1)

    assert fanout.close() is True
    assert (legacy.close_calls, draining.close_calls, tail.close_calls) == (1, 2, 1)


def test_each_logical_event_gets_one_distinct_identity_before_prepare() -> None:
    prepared_ids: list[tuple[str, str]] = []

    class IdentitySink(CapturingSink):
        def prepare(self, event: UnsequencedEvent) -> object:
            prepared_ids.append((self.name, event.event_id))
            return None

    minted = iter(("event-a", "event-b"))
    first_sink = IdentitySink(name="first")
    second_sink = IdentitySink(name="second")
    fanout = FanOutSink(
        [first_sink, second_sink],
        process_instance_id="proc-events",
        event_id_factory=lambda: next(minted),
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    payload = RunStarted(purpose="chat")

    first = fanout.emit(payload, envelope)
    second = fanout.emit(payload, envelope)

    assert (first.event_id, second.event_id) == ("event-a", "event-b")
    assert prepared_ids == [
        ("first", "event-a"),
        ("second", "event-a"),
        ("first", "event-b"),
        ("second", "event-b"),
    ]
    assert [event.event_id for event in first_sink.events] == [
        "event-a",
        "event-b",
    ]
    assert [event.event_id for event in second_sink.events] == [
        "event-a",
        "event-b",
    ]


def test_sequence_construction_control_exit_fails_fanout_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seq allocated without an envelope may never be stepped over."""
    failure = KeyboardInterrupt("sequenced event construction interrupted")
    real_sequenced_event = events_module.SequencedEvent
    calls = 0

    def interrupt_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return real_sequenced_event(*args, **kwargs)

    monkeypatch.setattr(events_module, "SequencedEvent", interrupt_once)
    sink = CapturingSink(name="capture")
    fanout = FanOutSink([sink], process_instance_id="proc-seq-fence")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit_linearized(
            RunStarted(purpose="chat"),
            envelope,
            boundary=nullcontext(),
            after_commit=lambda event: projected.append(event.seq),
        )
    assert raised.value is failure
    assert sink.events == []
    assert projected == [], "an unconstructed event cannot advance its projection"

    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)
    assert sink.events == [], "a later seq must not cross the missing seq"


def test_commit_loop_instruction_exit_resumes_every_prepared_sibling() -> None:
    """FanOut progress is not owned by one vulnerable ``for`` jump."""
    first = CapturingSink(name="first")
    middle = CapturingSink(name="middle")
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [first, middle, tail], process_instance_id="proc-commit-cursor"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    failure = KeyboardInterrupt("commit loop housekeeping interrupted")
    code = _commit_driver_code(FanOutSink._publish_guarded)
    source, first_line = inspect.getsourcelines(FanOutSink._publish_guarded)
    commit_lines = [
        first_line + index
        for index, text in enumerate(source)
        if "while commit_index < len(prepared):" in text
    ]
    commit_line = commit_lines[-1]
    instructions = tuple(dis.get_instructions(code))
    target = next(
        instruction.offset
        for instruction in instructions
        if instruction.opname == "JUMP_BACKWARD"
        and instruction.positions.lineno == commit_line
    )

    with interrupt_instruction_once(code, target, failure):
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert raised.value is failure
    assert [event.seq for event in first.events] == [1]
    assert [event.seq for event in middle.events] == [1]
    assert [event.seq for event in tail.events] == [1]


def test_post_commit_failure_cannot_reverse_publish_or_skip_later_cleanup(
    tmp_path,
) -> None:
    cleanup_secret = "post-commit-secret-must-not-survive"
    calls: list[str] = []

    class CleanupSink(CapturingSink):
        def __init__(self, *, name: str, fails: bool = False):
            super().__init__(name=name)
            self.fails = fails
            self.commit_calls = 0

        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            self.commit_calls += 1

            def cleanup() -> None:
                calls.append(self.name)
                if self.fails:
                    raise RuntimeError(cleanup_secret)

            return PostCommit(cleanup)

    broken = CleanupSink(name="broken", fails=True)
    released = CleanupSink(name="released")
    capture = CapturingSink(name="capture")
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="proc-post-commit",
    )
    fanout = FanOutSink(
        [broken, released, trace, capture],
        process_instance_id="proc-post-commit",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    published = fanout.emit(RunStarted(purpose="chat"), envelope)
    fanout.emit(RunStarted(purpose="chat"), envelope)

    assert published.seq == 1
    assert calls[:2] == ["broken", "released"], (
        "one cleanup failure must not strand later callbacks"
    )
    assert broken.commit_calls == 1, "the failed sink is disabled for the Run"
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1
    assert dict(notices[0].payload.detail) == {
        "sink": "broken",
        "stage": "post_commit",
    }
    document = json.dumps(capture.events, default=event_json_default)
    assert cleanup_secret not in document
    incomplete, reason = fanout.flush_barrier("r1")
    assert (incomplete, reason) == (False, None)
    fanout.close()
    trace_documents = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "traces").rglob("*")
        if path.is_file()
    ]
    assert trace_documents
    assert all(cleanup_secret not in document for document in trace_documents)


def test_commit_control_exit_settles_earlier_owner_without_disabling_sink() -> None:
    """A later sink cannot strand work already returned by an earlier one."""
    cleanup_calls: list[int] = []
    failure = KeyboardInterrupt("sibling commit interrupted")

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(event.seq))

    class InterruptingSink(CapturingSink):
        armed = True

        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if self.armed:
                raise failure

    cleanup = CleanupSink(name="cleanup")
    interrupted = InterruptingSink(name="interrupted")
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [cleanup, interrupted, tail], process_instance_id="proc-control-commit"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    linearized: list[int] = []

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit_linearized(
            RunStarted(purpose="chat"),
            envelope,
            boundary=nullcontext(),
            after_commit=lambda event: linearized.append(event.seq),
        )
    assert raised.value is failure
    assert cleanup_calls == [1]
    assert [event.seq for event in tail.events] == [1]
    assert linearized == [1]

    interrupted.armed = False
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert [event.seq for event in interrupted.events] == [1, 2], (
        "a process-control exit must not be booked as a run-local sink failure"
    )
    assert [event.seq for event in tail.events] == [1, 2]
    assert cleanup_calls == [1, 2]


@pytest.mark.parametrize(
    "failure",
    [
        KeyboardInterrupt("critical commit interrupted"),
        _SyntheticCommitControl("critical commit effect unknown"),
    ],
    ids=("keyboard-interrupt", "custom-base-exception"),
)
def test_critical_commit_control_exit_marks_only_that_run_incomplete(
    failure: BaseException,
) -> None:
    """A flushed result cannot erase a critical commit whose effect is unknown."""
    cleanup_calls: list[str] = []

    class CriticalSink(CapturingSink):
        def __init__(self) -> None:
            super().__init__(name="critical", flush_at_run_end=True)
            self.armed = True

        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> None:
            if self.armed and event.envelope.run_id == "r1":
                self.armed = False
                raise failure
            super().commit(prepared, event)

        def settle_interrupted_commit(
            self, interrupted: BaseException
        ) -> PostCommit:
            assert interrupted is failure
            return PostCommit(lambda: cleanup_calls.append(self.name))

    critical = CriticalSink()
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [critical, tail], process_instance_id="proc-critical-control"
    )

    with pytest.raises(type(failure)) as raised:
        fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))

    assert raised.value is failure
    assert cleanup_calls == ["critical"]
    assert [event.envelope.run_id for event in tail.events] == ["r1"]
    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    assert cleanup_calls == ["critical"]
    assert any(event.envelope.run_id == "r1" for event in critical.events), (
        "process control does not disable the critical sink within its Run"
    )
    incomplete, reason = fanout.flush_barrier("r1")
    assert (incomplete, reason) == (True, "critical commit failed")
    assert not any(
        getattr(event.payload, "code", None) == "sink_disabled"
        for event in tail.events
    )

    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))
    assert any(event.envelope.run_id == "r2" for event in critical.events)
    assert fanout.flush_barrier("r2") == (False, None)


def test_critical_commit_caller_exit_after_none_return_is_not_sink_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A captured successful None return is caller housekeeping, not loss."""
    failure = KeyboardInterrupt("caller interrupted after critical commit")
    calls = 0
    real_finish = events_module._finish_commit_iteration

    def interrupt_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        real_finish()

    monkeypatch.setattr(
        events_module, "_finish_commit_iteration", interrupt_once
    )
    critical = CapturingSink(name="critical", flush_at_run_end=True)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [critical, tail], process_instance_id="proc-critical-returned"
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))

    assert raised.value is failure
    assert [event.seq for event in critical.events] == [1]
    assert [event.seq for event in tail.events] == [1]
    assert fanout.flush_barrier("r1") == (False, None)
    assert not any(
        getattr(event.payload, "code", None) == "sink_disabled"
        for event in tail.events
    )


@pytest.mark.parametrize(
    ("returns_post_commit", "failure"),
    [
        (False, KeyboardInterrupt("critical None return not captured")),
        (
            True,
            _SyntheticCommitControl("critical PostCommit return not captured"),
        ),
    ],
    ids=("none", "post-commit"),
)
def test_critical_commit_return_store_exit_is_not_sink_loss(
    returns_post_commit: bool,
    failure: BaseException,
) -> None:
    """A C-returned commit result survives FanOut's STORE boundary."""
    cleanup_calls: list[int] = []

    class CriticalSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit | None:
            super().commit(prepared, event)
            if returns_post_commit:
                return PostCommit(lambda: cleanup_calls.append(event.seq))
            return None

    critical = CriticalSink(name="critical", flush_at_run_end=True)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [critical, tail], process_instance_id="proc-critical-return-store"
    )
    code = _commit_driver_code(FanOutSink._publish_guarded)
    target = _commit_return_store_instruction(code)

    with interrupt_instruction_once(
        code, target, failure
    ) as armed:
        with pytest.raises(type(failure)) as raised:
            fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))

    assert armed == [False]
    assert raised.value is failure
    assert [event.seq for event in critical.events] == [1]
    assert [event.seq for event in tail.events] == [1]
    assert cleanup_calls == ([1] if returns_post_commit else [])

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    assert [event.seq for event in critical.events] == [1, 2]
    assert [event.seq for event in tail.events] == [1, 2]
    assert cleanup_calls == ([1, 2] if returns_post_commit else [])
    assert fanout.flush_barrier("r1") == (False, None)
    assert not any(
        getattr(event.payload, "code", None) == "sink_disabled"
        for event in tail.events
    )

    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))
    assert [event.seq for event in critical.events] == [1, 2, 3]
    assert cleanup_calls == ([1, 2, 3] if returns_post_commit else [])
    assert fanout.flush_barrier("r2") == (False, None)


@pytest.mark.parametrize(
    "returns_post_commit", [False, True], ids=("none", "post-commit")
)
@pytest.mark.parametrize(
    "outer_exit_point", ["entry", "cursor_committed"]
)
def test_critical_commit_result_slot_survives_every_recovery_layer(
    returns_post_commit: bool,
    outer_exit_point: str,
) -> None:
    """Every commit recovery layer consumes the same returned-result fact."""
    primary = KeyboardInterrupt("commit return store interrupted")
    inner_exit = SystemExit("inner commit recovery interrupted")
    outer_exit = _SyntheticCommitControl("outer commit recovery interrupted")
    cleanup_calls: list[int] = []
    settlement_calls: list[BaseException] = []

    class CriticalSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit | None:
            super().commit(prepared, event)
            if returns_post_commit:
                return PostCommit(lambda: cleanup_calls.append(event.seq))
            return None

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> None:
            settlement_calls.append(failure)
            return None

    critical = CriticalSink(name="critical", flush_at_run_end=True)
    tail = CapturingSink(name="tail-critical", flush_at_run_end=True)
    fanout = FanOutSink(
        [critical, tail], process_instance_id="proc-critical-recovery-layers"
    )
    code = _commit_driver_code(FanOutSink._publish_guarded)
    targets = _commit_recovery_instruction_sequence(code, outer_exit_point)

    with _interrupt_instruction_sequence(
        code,
        tuple(zip(targets, (primary, inner_exit, outer_exit), strict=True)),
    ) as pending:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))

    assert sys.exception() is None
    assert pending == []
    assert raised.value is primary
    assert cleanup_calls == ([1] if returns_post_commit else [])
    assert settlement_calls == []
    assert [event.seq for event in tail.events] == [1]
    assert fanout.flush_barrier("r1") == (False, None)

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    assert cleanup_calls == ([1, 2] if returns_post_commit else [])
    assert [event.seq for event in tail.events] == [1, 2]
    assert fanout.flush_barrier("r1") == (False, None)
    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))
    assert cleanup_calls == ([1, 2, 3] if returns_post_commit else [])
    assert [event.seq for event in tail.events] == [1, 2, 3]
    assert fanout.flush_barrier("r2") == (False, None)


@pytest.mark.parametrize(
    "outer_exit_point", ["entry", "cursor_committed"]
)
def test_critical_unreturned_commit_survives_every_recovery_layer(
    outer_exit_point: str,
) -> None:
    """Nested recovery settles the original failed commit and resumes siblings."""
    primary = KeyboardInterrupt("critical commit did not return")
    inner_exit = SystemExit("inner commit recovery interrupted")
    outer_exit = _SyntheticCommitControl("outer commit recovery interrupted")
    cleanup_calls: list[str] = []

    class CriticalSink(CapturingSink):
        def __init__(self) -> None:
            super().__init__(name="critical", flush_at_run_end=True)
            self.armed = True

        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> None:
            if self.armed:
                self.armed = False
                raise primary
            super().commit(prepared, event)

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> PostCommit:
            assert failure is primary
            return PostCommit(lambda: cleanup_calls.append(self.name))

    critical = CriticalSink()
    tail = CapturingSink(name="tail-critical", flush_at_run_end=True)
    fanout = FanOutSink(
        [critical, tail], process_instance_id="proc-critical-unreturned-layers"
    )
    code = _commit_driver_code(FanOutSink._publish_guarded)
    _, inner, outer = _commit_recovery_instruction_sequence(
        code, outer_exit_point
    )

    with _interrupt_instruction_sequence(
        code, ((inner, inner_exit), (outer, outer_exit))
    ) as pending:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))

    assert sys.exception() is None
    assert pending == []
    assert raised.value is primary
    assert cleanup_calls == ["critical"]
    assert [event.seq for event in tail.events] == [1]

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    assert any(event.envelope.run_id == "r1" for event in critical.events)
    assert [event.seq for event in tail.events[:2]] == [1, 2]
    assert fanout.flush_barrier("r1") == (True, "critical commit failed")
    assert not any(
        getattr(event.payload, "code", None) == "sink_disabled"
        for event in tail.events
    )

    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))
    assert any(event.envelope.run_id == "r2" for event in critical.events)
    assert fanout.flush_barrier("r2") == (False, None)


def test_multiple_commit_control_exits_settle_every_owner_and_keep_first() -> None:
    """Every prepared sink commits once even when two control exits occur."""
    first_failure = KeyboardInterrupt("first commit control exit")
    second_failure = SystemExit("second commit control exit")
    cleanup_calls: list[str] = []

    class OwnedInterruptSink(CapturingSink):
        def __init__(self, *, name: str, failure: BaseException):
            super().__init__(name=name)
            self.failure = failure
            self.armed = True

        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if self.armed:
                raise self.failure

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> PostCommit:
            assert failure is self.failure
            return PostCommit(lambda: cleanup_calls.append(self.name))

    first = OwnedInterruptSink(name="first", failure=first_failure)
    second = OwnedInterruptSink(name="second", failure=second_failure)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [first, second, tail], process_instance_id="proc-multi-control"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert raised.value is first_failure
    assert cleanup_calls == ["first", "second"]
    assert [event.seq for event in first.events] == [1]
    assert [event.seq for event in second.events] == [1]
    assert [event.seq for event in tail.events] == [1]

    first.armed = False
    second.armed = False
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert [event.seq for event in first.events] == [1, 2]
    assert [event.seq for event in second.events] == [1, 2]
    assert [event.seq for event in tail.events] == [1, 2]


def test_control_exit_does_not_skip_disabled_notices_or_let_notice_replace_it() -> None:
    """An irrevocable ordinary failure is still reported before control exits."""
    first_control = KeyboardInterrupt("primary commit control exit")
    notice_control = SystemExit("first notice control exit")

    class FailingSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise RuntimeError(f"{self.name} commit failed")

    class PrimaryInterruptSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise first_control

    class NoticeInterruptSink(CapturingSink):
        armed = True

        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, Notice) and self.armed:
                self.armed = False
                raise notice_control

    first_broken = FailingSink(name="first-broken")
    second_broken = FailingSink(name="second-broken")
    primary = PrimaryInterruptSink(name="primary-control")
    notice_interrupt = NoticeInterruptSink(name="notice-control")
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            first_broken,
            second_broken,
            primary,
            notice_interrupt,
            tail,
        ],
        process_instance_id="proc-control-notices",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), envelope)

    assert raised.value is first_control
    notices = [
        event
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert [dict(event.payload.detail) for event in notices] == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    assert [event.seq for event in tail.events] == [1, 2, 3]


def test_after_commit_control_exit_still_settles_committed_owners() -> None:
    """Projection publication cannot bypass sink cleanup already owed."""
    cleanup_calls: list[int] = []
    failure = SystemExit("after_commit interrupted")

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(event.seq))

    sink = CleanupSink(name="cleanup")
    fanout = FanOutSink([sink], process_instance_id="proc-control-linearize")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    def interrupt_after_commit(_event: SequencedEvent) -> None:
        raise failure

    with pytest.raises(SystemExit) as raised:
        fanout.emit_linearized(
            RunStarted(purpose="chat"),
            envelope,
            boundary=nullcontext(),
            after_commit=interrupt_after_commit,
        )
    assert raised.value is failure
    assert cleanup_calls == [1]


def test_post_commit_control_exit_does_not_skip_later_owner() -> None:
    """All acquired cleanup owners settle before the first control exits."""
    calls: list[str] = []
    failure = KeyboardInterrupt("cleanup interrupted")

    class CleanupSink(CapturingSink):
        def __init__(self, *, name: str, interrupt_once: bool = False):
            super().__init__(name=name)
            self.interrupt_once = interrupt_once

        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)

            def cleanup() -> None:
                calls.append(self.name)
                if self.interrupt_once:
                    self.interrupt_once = False
                    raise failure

            return PostCommit(cleanup)

    first = CleanupSink(name="first", interrupt_once=True)
    second = CleanupSink(name="second")
    fanout = FanOutSink(
        [first, second], process_instance_id="proc-control-cleanup"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert raised.value is failure
    assert calls == ["first", "second"]

    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert [event.seq for event in first.events] == [1]
    assert [event.seq for event in second.events] == [1]


def _instruction_for_line(
    function,
    needle: str,
    *,
    opname: str,
    last: bool = False,
    code: CodeType | None = None,
) -> int:
    source, first_line = inspect.getsourcelines(function)
    line = first_line + next(
        index for index, text in enumerate(source) if needle in text
    )
    matches = [
        instruction.offset
        for instruction in dis.get_instructions(code or function)
        if instruction.opname == opname
        and instruction.positions.lineno == line
    ]
    assert matches, (needle, opname)
    return matches[-1 if last else 0]


def _commit_driver_code(function) -> CodeType:
    """Return the nested driver that owns the commit-call bytecode."""
    return next(
        value
        for value in function.__code__.co_consts
        if isinstance(value, CodeType) and value.co_name == "drain_commits_locked"
    )


def _commit_return_store_instruction(code: CodeType) -> int:
    """Locate the caller STORE immediately after the sink commit call."""
    instructions = tuple(dis.get_instructions(code))
    commit_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_ATTR" and instruction.argval == "commit"
    )
    call = next(
        index
        for index, instruction in enumerate(
            instructions[commit_load:], commit_load
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    store = next(
        instruction
        for instruction in instructions[call + 1 :]
        if instruction.opname == "STORE_DEREF"
        and instruction.argval == "post_commit"
    )
    return store.offset


def _commit_recovery_instruction_sequence(
    code: CodeType,
    outer_exit_point: str,
) -> tuple[int, int, int]:
    """Locate result STORE plus inner and outer commit recovery entries."""
    instructions = tuple(dis.get_instructions(code))
    store = _commit_return_store_instruction(code)
    control_store = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.offset > store
        and instruction.opname == "STORE_DEREF"
        and instruction.argval == "control_error"
    )
    inner = next(
        instruction.offset
        for instruction in instructions[control_store + 1 :]
        if instruction.opname == "LOAD_DEREF"
        and instruction.argval == "post_commit"
    )

    outer_match = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.offset > inner
        and instruction.opname == "CHECK_EXC_MATCH"
    )
    handler = next(
        index
        for index, instruction in enumerate(
            instructions[outer_match + 1 :], outer_match + 1
        )
        if instruction.opname == "STORE_FAST"
        and instruction.argval == "exc"
    )
    if outer_exit_point == "entry":
        outer = next(
            instruction.offset
            for instruction in instructions[handler + 1 :]
            if instruction.opname == "LOAD_DEREF"
        )
    else:
        assert outer_exit_point == "cursor_committed"
        active_cleared = next(
            index
            for index, instruction in enumerate(
                instructions[handler + 1 :], handler + 1
            )
            if instruction.opname == "STORE_DEREF"
            and instruction.argval == "active_index"
        )
        handler_exit = instructions[active_cleared + 1]
        assert handler_exit.opname == "POP_EXCEPT"
        outer = instructions[active_cleared + 2].offset
    return store, inner, outer


def _interrupt_task_instruction(task_type, code, offset, failure):
    """Select the same tail policy when several policies share one driver."""
    return interrupt_instruction_once(
        code, offset, failure,
        when=lambda frame: isinstance(frame.f_locals.get("task"), task_type),
    )


def _call_boundary_instruction(function, boundary: str) -> int:
    """Instruction before ``next`` admission or after its return."""
    instructions = tuple(dis.get_instructions(function))
    next_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL"
        and instruction.argval == "next"
    )
    call = next(
        index
        for index, instruction in enumerate(instructions[next_load:], next_load)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    if boundary == "pre_call":
        return instructions[call].offset
    assert boundary == "post_return"
    return instructions[call + 1].offset


def _instruction_after_named_call(
    function, needle: str, callee: str
) -> int:
    """Return the first caller instruction after a named call on one line."""
    source, first_line = inspect.getsourcelines(function)
    line = first_line + next(
        index for index, text in enumerate(source) if needle in text
    )
    instructions = tuple(dis.get_instructions(function))
    load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.positions.lineno == line
        and instruction.opname == "LOAD_GLOBAL"
        and instruction.argval == callee
    )
    call = next(
        index
        for index, instruction in enumerate(instructions[load:], load)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    return instructions[call + 1].offset


def _outcome_guard_install_instruction(function, boundary: str) -> int:
    instructions = tuple(dis.get_instructions(function))
    guard_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL"
        and instruction.argval == "_PublicationOutcomeGuard"
    )
    constructor_call = next(
        index
        for index, instruction in enumerate(
            instructions[guard_load:], guard_load
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    if boundary == "constructor_call":
        return instructions[constructor_call].offset
    if boundary == "constructor_return":
        return instructions[constructor_call + 1].offset
    assert boundary == "enter_call"
    enter_call = next(
        index
        for index, instruction in enumerate(
            instructions[constructor_call + 1 :], constructor_call + 1
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    return instructions[enter_call].offset


def _outcome_guard_exit_instruction(boundary: str) -> int:
    instructions = tuple(
        dis.get_instructions(events_module._PublicationOutcomeGuard.__exit__)
    )
    if boundary == "entry":
        return next(
            instruction.offset
            for instruction in instructions
            if instruction.opname != "RESUME"
        )
    if boundary == "condition":
        return next(
            instruction.offset
            for instruction in instructions
            if instruction.opname == "IS_OP"
        )
    assert boundary == "raise"
    return _instruction_for_line(
        events_module._PublicationOutcomeGuard.__exit__,
        "raise expected",
        opname="RAISE_VARARGS",
    )


@pytest.mark.parametrize("boundary", ["pre_call", "post_return"])
def test_after_action_driver_instruction_exit_runs_projection_once(
    boundary: str,
) -> None:
    sink = CapturingSink(name="capture")
    fanout = FanOutSink([sink], process_instance_id=f"proc-after-{boundary}")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []
    failure = KeyboardInterrupt(f"after action {boundary}")
    target = _call_boundary_instruction(
        events_module._call_task, boundary
    )

    with _interrupt_task_instruction(
        events_module._AfterCommitTask,
        events_module._call_task.__code__, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is failure
    assert projected == [1]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


@pytest.mark.parametrize("boundary", ["pre_call", "post_return"])
def test_post_action_driver_instruction_exit_runs_each_owner_once(
    boundary: str,
) -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit(f"post action {boundary}")
    calls: list[str] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: calls.append(self.name))

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    fanout = FanOutSink(
        [
            CleanupSink(name="first"),
            CleanupSink(name="second"),
            PrimarySink(name="control"),
        ],
        process_instance_id=f"proc-post-action-{boundary}",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _call_boundary_instruction(
        events_module._call_task, boundary
    )

    with _interrupt_task_instruction(
        events_module._PostCommitTask,
        events_module._call_task.__code__, target, interruption
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert calls == ["first", "second"]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


@pytest.mark.parametrize("boundary", ["pre_call", "post_return"])
def test_notice_action_driver_instruction_exit_publishes_each_notice_once(
    boundary: str,
) -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit(f"notice action {boundary}")

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            PrimarySink(name="control"),
            tail,
        ],
        process_instance_id=f"proc-notice-action-{boundary}",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _call_boundary_instruction(
        events_module._call_task, boundary
    )

    with _interrupt_task_instruction(
        events_module._NoticeTask,
        events_module._call_task.__code__, target, interruption
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    notices = [
        dict(event.payload.detail)
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert notices == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_after_task_materialization_return_exit_rebuilds_from_source() -> None:
    sink = CapturingSink(name="capture")
    fanout = FanOutSink([sink], process_instance_id="proc-after-materialize")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []
    failure = KeyboardInterrupt("after task returned before caller receipt")
    target = _instruction_after_named_call(
        events_module._PublicationTailState.drain_after,
        "_new_after_commit_task(action, event)",
        "_new_after_commit_task",
    )

    with interrupt_instruction_once(
        events_module._PublicationTailState.drain_after.__code__,
        target,
        failure,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is failure
    assert projected == [1]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_after_phase_condition_exit_resumes_projection() -> None:
    sink = CapturingSink(name="capture")
    fanout = FanOutSink([sink], process_instance_id="proc-after-phase")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []
    failure = KeyboardInterrupt("after phase condition interrupted")
    target = _instruction_for_line(
        events_module._PublicationTailState.drain_after,
        "self.after_phase.drain(step)",
        opname="LOAD_FAST_BORROW",
    )

    with interrupt_instruction_once(
        events_module._PublicationTailState.drain_after.__code__,
        target,
        failure,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is failure
    assert projected == [1]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_post_task_materialization_return_exit_rebuilds_from_source() -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit("post task returned before list admission")
    calls: list[str] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: calls.append(self.name))

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    fanout = FanOutSink(
        [
            CleanupSink(name="first"),
            CleanupSink(name="second"),
            PrimarySink(name="control"),
        ],
        process_instance_id="proc-post-materialize",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _instruction_after_named_call(
        events_module._PublicationTailState.drain,
        "_new_post_commit_task(sink, action)",
        "_new_post_commit_task",
    )

    with interrupt_instruction_once(
        events_module._PublicationTailState.drain.__code__,
        target,
        interruption,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert calls == ["first", "second"]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_notice_task_materialization_return_exit_rebuilds_from_source() -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit("notice task returned before list admission")

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            PrimarySink(name="control"),
            tail,
        ],
        process_instance_id="proc-notice-materialize",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _instruction_after_named_call(
        events_module._PublicationTailState.drain,
        "_new_notice_task(",
        "_new_notice_task",
    )

    with interrupt_instruction_once(
        events_module._PublicationTailState.drain.__code__,
        target,
        interruption,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    notices = [
        dict(event.payload.detail)
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert notices == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_after_commit_instruction_exit_cannot_silently_skip_projection() -> None:
    """An exit after callback admission cannot leave FanOut looking healthy."""
    sink = CapturingSink(name="capture")
    fanout = FanOutSink([sink], process_instance_id="proc-after-cursor")
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []
    failure = KeyboardInterrupt("after_commit admission interrupted")
    target = _instruction_for_line(
        events_module._invoke_task,
        "_call_task(task)",
        opname="LOAD_GLOBAL",
    )

    with _interrupt_task_instruction(
        events_module._AfterCommitTask,
        events_module._invoke_task.__code__, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is failure
    assert projected == [1]
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


def test_commit_return_instruction_exit_cannot_lose_plain_postcommit() -> None:
    """A returned ordinary PostCommit survives caller-side capture failure."""
    failure = KeyboardInterrupt("postcommit capture interrupted")
    cleanup_calls: list[str] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(self.name))

    owner = CleanupSink(name="owner", flush_at_run_end=True)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [owner, tail], process_instance_id="proc-plain-post-cursor"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    code = _commit_driver_code(FanOutSink._publish_guarded)
    target = _instruction_for_line(
        FanOutSink._publish_guarded,
        "_record_post_commit(",
        opname="LOAD_GLOBAL",
        code=code,
    )

    with interrupt_instruction_once(
        code, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is failure
    assert cleanup_calls == ["owner"]
    assert [event.seq for event in tail.events] == [1]
    assert fanout.flush_barrier("r1") == (False, None), (
        "a captured commit return is not a critical sink failure"
    )
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


def test_settlement_return_instruction_exit_cannot_lose_postcommit() -> None:
    """A settlement owner remains pending until FanOut captures its action."""
    primary = KeyboardInterrupt("commit interrupted")
    boundary = SystemExit("settlement returned before capture")
    cleanup_calls: list[str] = []

    class InterruptedSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> PostCommit:
            assert failure is primary
            return PostCommit(lambda: cleanup_calls.append(self.name))

    interrupted = InterruptedSink(name="interrupted")
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [interrupted, tail], process_instance_id="proc-settlement-cursor"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    instructions = tuple(
        dis.get_instructions(events_module._settle_interrupted_task)
    )
    completed = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "completed"
    )
    target = instructions[completed + 1].offset

    with interrupt_instruction_once(
        events_module._settle_interrupted_task.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert cleanup_calls == ["interrupted"]
    assert [event.seq for event in tail.events] == [1]
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


def test_postcommit_entered_instruction_exit_fails_closed_and_continues() -> None:
    """An uncertain entered owner is not mistaken for healthy completion."""
    primary = KeyboardInterrupt("commit interrupted first")
    boundary = SystemExit("postcommit entered before action")
    calls: list[str] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: calls.append(self.name))

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    fanout = FanOutSink(
        [
            CleanupSink(name="first"),
            CleanupSink(name="second"),
            PrimarySink(name="control"),
        ],
        process_instance_id="proc-post-entered",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _instruction_for_line(
        events_module._invoke_task,
        "_call_task(task)",
        opname="LOAD_GLOBAL",
    )

    with _interrupt_task_instruction(
        events_module._PostCommitTask,
        events_module._invoke_task.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert calls == ["first", "second"]
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


def test_notice_entered_instruction_exit_fails_closed_and_continues() -> None:
    """An uncertain entered notice cannot leave the sequence healthy."""
    primary = KeyboardInterrupt("commit interrupted first")
    boundary = SystemExit("notice entered before publish")

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            PrimarySink(name="control"),
            tail,
        ],
        process_instance_id="proc-notice-entered",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _instruction_for_line(
        events_module._invoke_task,
        "_call_task(task)",
        opname="LOAD_GLOBAL",
    )

    with _interrupt_task_instruction(
        events_module._NoticeTask,
        events_module._invoke_task.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    notices = [
        event
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert [dict(event.payload.detail) for event in notices] == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


@pytest.mark.parametrize("phase", ["settlement", "post", "notice"])
def test_phase_condition_instruction_exit_resumes_all_pending_work(
    phase: str,
) -> None:
    """A phase admission exit cannot replace the first cause or skip work."""
    primary = KeyboardInterrupt("commit interrupted first")
    boundary = SystemExit(f"{phase} phase condition interrupted")
    settlement_calls: list[str] = []
    cleanup_calls: list[str] = []

    class InterruptedSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> PostCommit:
            assert failure is primary
            return PostCommit(lambda: settlement_calls.append(self.name))

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit | None:
            super().commit(prepared, event)
            if not isinstance(event.payload, RunStarted):
                return None
            return PostCommit(lambda: cleanup_calls.append(self.name))

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            InterruptedSink(name="interrupted"),
            CleanupSink(name="first-cleanup"),
            CleanupSink(name="second-cleanup"),
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            tail,
        ],
        process_instance_id=f"proc-{phase}-condition",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    phase_condition = {
        "settlement": "if self.settlement_phase is None:",
        "post": "if self.post_phase is None:",
        "notice": "if self.notice_phase is None:",
    }[phase]
    target = _instruction_for_line(
        events_module._PublicationTailState.drain,
        phase_condition,
        opname="LOAD_ATTR",
    )

    with interrupt_instruction_once(
        events_module._PublicationTailState.drain.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert settlement_calls == ["interrupted"]
    assert cleanup_calls == ["first-cleanup", "second-cleanup"]
    notices = [
        dict(event.payload.detail)
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert notices == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(BaseException) as refused:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert isinstance(refused.value, ProcessFatalSinkError)


@pytest.mark.parametrize(
    ("transition", "surface", "needle", "opname", "occurrence"),
    [
        (
            "after_to_settlement",
            "publish",
            "            drain_tail()",
            "CALL",
            1,
        ),
        (
            "settlement_to_post",
            "tail",
            "if self.post_phase is None:",
            "LOAD_ATTR",
            1,
        ),
        (
            "post_to_notice",
            "tail",
            "if self.notice_phase is None:",
            "LOAD_ATTR",
            1,
        ),
        (
            "notice_to_fatal",
            "publish",
            "            finalize_tail()",
            "CALL",
            3,
        ),
        (
            "final_outcome_condition",
            "publish",
            "if outcome_error is not None:",
            "LOAD_FAST",
            3,
        ),
        (
            "final_outcome_raise",
            "publish",
            "raise outcome_error",
            "LOAD_FAST",
            1,
        ),
    ],
)
def test_tail_transition_exit_recovers_every_durable_task(
    transition: str,
    surface: str,
    needle: str,
    opname: str,
    occurrence: int,
) -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit(f"{transition} interrupted second")
    cleanup_calls: list[str] = []
    projected: list[int] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit | None:
            super().commit(prepared, event)
            if not isinstance(event.payload, RunStarted):
                return None
            return PostCommit(lambda: cleanup_calls.append(self.name))

    class InterruptedSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

        def settle_interrupted_commit(
            self, failure: BaseException
        ) -> PostCommit:
            assert failure is primary
            return PostCommit(lambda: cleanup_calls.append(self.name))

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            CleanupSink(name="normal-cleanup"),
            InterruptedSink(name="interrupted-cleanup"),
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            tail,
        ],
        process_instance_id=f"proc-tail-{transition}",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    function = (
        FanOutSink._publish_guarded
        if surface == "publish"
        else events_module._PublicationTailState.drain
    )
    target = _instruction_for_line(function, needle, opname=opname)

    with interrupt_instruction_once(
        function.__code__,
        target,
        interruption,
        occurrence=occurrence,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is primary
    assert projected == [1]
    assert cleanup_calls == ["normal-cleanup", "interrupted-cleanup"]
    notices = [
        dict(event.payload.detail)
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert notices == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


@pytest.mark.parametrize(
    "boundary",
    ["constructor_call", "constructor_return", "enter_call", "exit_entry"],
)
@pytest.mark.parametrize("has_primary", [True, False])
def test_outcome_guard_boundary_exit_preserves_first_cause(
    boundary: str,
    has_primary: bool,
) -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit(f"outcome guard {boundary} interrupted")
    cleanup_calls: list[int] = []
    projected: list[int] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(event.seq))

    class InterruptedSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    sinks: list[CapturingSink] = [CleanupSink(name="cleanup")]
    if has_primary:
        sinks.append(InterruptedSink(name="control"))
    fanout = FanOutSink(
        sinks,
        process_instance_id=f"proc-outcome-{boundary}-{has_primary}",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    if boundary == "exit_entry":
        function = events_module._PublicationOutcomeGuard.__exit__
        target = _outcome_guard_exit_instruction("entry")
    else:
        function = FanOutSink._publish_guarded
        target = _outcome_guard_install_instruction(function, boundary)

    with interrupt_instruction_once(
        function.__code__, target, interruption
    ) as armed:
        with pytest.raises(BaseException) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is (primary if has_primary else interruption)
    assert cleanup_calls == [1]
    assert projected == [1]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_outcome_guard_condition_exit_preserves_first_cause() -> None:
    primary = KeyboardInterrupt("commit interrupted first")
    interruption = SystemExit("outcome guard condition interrupted")
    cleanup_calls: list[int] = []
    projected: list[int] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(event.seq))

    class InterruptedSink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    fanout = FanOutSink(
        [CleanupSink(name="cleanup"), InterruptedSink(name="control")],
        process_instance_id="proc-outcome-condition",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    target = _outcome_guard_exit_instruction("condition")

    with interrupt_instruction_once(
        events_module._PublicationOutcomeGuard.__exit__.__code__,
        target,
        interruption,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is primary
    assert cleanup_calls == [1]
    assert projected == [1]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_outcome_guard_raise_exit_preserves_first_cause() -> None:
    primary = KeyboardInterrupt("selected publication outcome")
    trigger = RuntimeError("unexpected final-outcome exit")
    interruption = SystemExit("outcome guard raise interrupted")
    fanout = FanOutSink([], process_instance_id="proc-outcome-raise")
    state = events_module._PublicationOutcomeState(
        fanout,
        expected=primary,
        publication_started=True,
    )
    target = _outcome_guard_exit_instruction("raise")

    with interrupt_instruction_once(
        events_module._PublicationOutcomeGuard.__exit__.__code__,
        target,
        interruption,
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            with events_module._PublicationOutcomeGuard(state):
                with events_module._PublicationOutcomeGuard(state):
                    raise trigger

    assert armed == [False]
    assert raised.value is primary
    with pytest.raises(ProcessFatalSinkError) as refused:
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(0.0, "r1", None, None, None, None),
        )
    assert refused.value.__cause__ is trigger


def test_selected_control_outcome_alone_keeps_fanout_available() -> None:
    primary = KeyboardInterrupt("commit interrupted once")
    cleanup_calls: list[int] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(event.seq))

    class InterruptedSink(CapturingSink):
        armed = True

        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if self.armed:
                raise primary

    interrupted = InterruptedSink(name="control")
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [CleanupSink(name="cleanup"), interrupted, tail],
        process_instance_id="proc-outcome-primary-only",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert raised.value is primary
    assert cleanup_calls == [1]

    interrupted.armed = False
    assert fanout.emit(RunStarted(purpose="chat"), envelope).seq == 2
    assert cleanup_calls == [1, 2]
    assert [event.seq for event in tail.events] == [1, 2]


@pytest.mark.parametrize(
    "boundary", ["constructor_call", "constructor_return", "enter_call"]
)
def test_outer_outcome_guard_install_exit_precedes_publication(
    boundary: str,
) -> None:
    interruption = SystemExit(f"outer guard {boundary} interrupted")
    sink = CapturingSink(name="capture")
    fanout = FanOutSink(
        [sink], process_instance_id=f"proc-outer-outcome-{boundary}"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    projected: list[int] = []
    target = _outcome_guard_install_instruction(FanOutSink._publish, boundary)

    with interrupt_instruction_once(
        FanOutSink._publish.__code__, target, interruption
    ) as armed:
        with pytest.raises(SystemExit) as raised:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: projected.append(event.seq),
            )

    assert armed == [False]
    assert raised.value is interruption
    assert sink.events == []
    assert projected == []
    event = fanout.emit_linearized(
        RunStarted(purpose="chat"),
        envelope,
        boundary=nullcontext(),
        after_commit=lambda committed: projected.append(committed.seq),
    )
    assert event.seq == 1
    assert [committed.seq for committed in sink.events] == [1]
    assert projected == [1]


def test_postcommit_loop_instruction_exit_continues_remaining_owners() -> None:
    """A pre-call exit cannot replace the first control or skip an owner."""
    primary = KeyboardInterrupt("commit interrupted first")
    boundary = SystemExit("postcommit loop interrupted second")
    cleanup_calls: list[str] = []

    class CleanupSink(CapturingSink):
        def commit(
            self, prepared: object, event: SequencedEvent
        ) -> PostCommit:
            super().commit(prepared, event)
            return PostCommit(lambda: cleanup_calls.append(self.name))

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            raise primary

    first = CleanupSink(name="first")
    second = CleanupSink(name="second")
    control = PrimarySink(name="control")
    fanout = FanOutSink(
        [first, second, control], process_instance_id="proc-post-cursor"
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    instructions = tuple(
        dis.get_instructions(events_module._invoke_task)
    )
    target = instructions[1].offset

    with _interrupt_task_instruction(
        events_module._PostCommitTask,
        events_module._invoke_task.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    assert cleanup_calls == ["first", "second"]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_notice_loop_instruction_exit_continues_remaining_notices() -> None:
    """Every owed notice survives a caller-side pre-call interruption."""
    primary = KeyboardInterrupt("commit interrupted first")
    boundary = SystemExit("notice loop interrupted second")

    class PrimarySink(CapturingSink):
        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if isinstance(event.payload, RunStarted):
                raise primary

    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [
            _RunStartedFailingSink(name="first-broken"),
            _RunStartedFailingSink(name="second-broken"),
            PrimarySink(name="control"),
            tail,
        ],
        process_instance_id="proc-notice-cursor",
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    instructions = tuple(dis.get_instructions(events_module._invoke_task))
    target = instructions[1].offset

    with _interrupt_task_instruction(
        events_module._NoticeTask,
        events_module._invoke_task.__code__, target, boundary
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            fanout.emit(RunStarted(purpose="chat"), envelope)

    assert armed == [False]
    assert raised.value is primary
    notices = [
        event
        for event in tail.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert [dict(event.payload.detail) for event in notices] == [
        {"sink": "first-broken", "stage": "commit"},
        {"sink": "second-broken", "stage": "commit"},
    ]
    with pytest.raises(ProcessFatalSinkError):
        fanout.emit(RunStarted(purpose="chat"), envelope)


def test_prepare_failure_does_not_abort_the_run_or_revisit_the_dead_sink() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    boom = BoomPrepareSink()
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    fanout = FanOutSink(
        [boom, capture],
        process_instance_id="proc-sink",
    )
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel([_GATE_DECISION, "pong"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-sink",
        secrets=(SECRET,),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message=f"please hide {SECRET} in the log")
        )
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id, timeout=5)
        assert result.outcome == "completed"
        assert result.reply is not None
        assert message_plain_text(result.reply) == "pong"
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row == ("finished", "completed")
        assert host.snapshot().coordinator_state == "idle"
        assert host.snapshot().unrecorded_terminal_projection is None
        # First prepare fails, the sink is disabled, later events skip it.
        assert boom.prepare_calls == 1
        assert boom.commit_calls == 0
        names = [event.payload.name for event in capture.events]
        assert "run.finished" in names
        assert "notice" in names
        dumped = json.dumps(
            [
                {
                    "name": event.payload.name,
                    "payload": repr(event.payload),
                }
                for event in capture.events
            ],
            ensure_ascii=False,
        )
        assert SECRET not in dumped
        telemetry = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
        assert SECRET not in telemetry
        notices = [
            event.payload
            for event in capture.events
            if event.payload.name == "notice"
        ]
        assert notices
        codes = {getattr(notice, "code", None) for notice in notices}
        assert "sink_disabled" in codes
        assert codes <= {"sink_disabled", "trace_incomplete"}
    finally:
        host.close()


# --- a process-level fatal outlives the Run that discovered it ---------------


def _envelope(run_id: str) -> EventEnvelope:
    return EventEnvelope(
        ts=0.0,
        run_id=run_id,
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )


class _ProcessFatalStubSink:
    """Stands in for the SSE sink at the FanOut boundary.

    A dispatcher or replay-ring fatal is not one Run's problem (#23 §9): the
    real broker reports it with the typed process-fatal error, and what the
    FanOut owes in return -- skip this sink for every Run, notify once -- is
    the contract this stub drives out. The type is resolved at construction
    so a missing signal fails the test loudly here, instead of masquerading
    as an ordinary run-local error the FanOut would silently book.
    """

    def __init__(self, *, name: str = "sse"):
        from agent_alfred.events import ProcessFatalSinkError

        self.name = name
        self.flush_at_run_end = False
        self._fatal = ProcessFatalSinkError
        self.prepare_calls = 0
        self.commit_calls = 0

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        self.prepare_calls += 1
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event
        self.commit_calls += 1
        raise self._fatal("the dispatcher is down; this sink cannot deliver")

    def flush(self, run_id: str) -> BestEffortFlushResult:
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


def test_sink_fatal_in_one_run_disables_it_for_the_next_run() -> None:
    """A sink that went process-fatal in r1 is not re-called when r2 begins.

    Keeping the disable in the per-Run bookkeeping meant every Run's flush
    barrier cleared it: the next Run re-called a sink the process already
    knew was dead, drew the same fatal again, and published the same notice
    again. The typed process-fatal error moves the sink into a process-wide
    set that no Run's end clears, so r2 never reaches it -- not even
    prepare.
    """
    fatal = _ProcessFatalStubSink(name="sse")
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([fatal, capture], process_instance_id="proc-fatal")

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    assert (fatal.prepare_calls, fatal.commit_calls) == (1, 1)
    # r1 is over: the per-Run bookkeeping is cleared, as every Run's end does.
    fanout.flush_barrier("r1")

    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))
    assert (fatal.prepare_calls, fatal.commit_calls) == (1, 1), (
        "a process-fatal sink is not called again -- not even prepare"
    )
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2"], "the healthy sink keeps receiving events"


def test_healthy_sinks_still_receive_notification_and_events() -> None:
    """One sink's process-fatal must not interrupt the others.

    The fan-out still owes every healthy sink the process-level
    ``sink_disabled`` notice -- exactly one envelope, ever -- and every
    event published after the fatal, including the events of Runs that
    begin afterwards. A dead fan-out that took its healthy sinks down with
    it would trade one broken transport for several.
    """
    fatal = _ProcessFatalStubSink(name="sse")
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([fatal, capture], process_instance_id="proc-fatal")

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    fanout.flush_barrier("r1")
    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))

    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1, "the process-level notice is published once"
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2"], "the healthy sink keeps receiving events"
    assert (fatal.prepare_calls, fatal.commit_calls) == (1, 1)


class _RunLocalErrorSink:
    """An ordinary transient failure -- the run-local class.

    It raises a plain ``RuntimeError``, which is not the typed process-fatal
    signal and must never be mistaken for one.
    """

    def __init__(self, *, name: str = "flaky"):
        self.name = name
        self.flush_at_run_end = False
        self.prepare_calls = 0
        self.commit_calls = 0

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        self.prepare_calls += 1
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event
        self.commit_calls += 1
        raise RuntimeError("a transient commit failure")

    def flush(self, run_id: str) -> BestEffortFlushResult:
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


def test_run_local_sink_error_stays_run_local() -> None:
    """An ordinary sink exception must not escalate to a process-wide ban.

    The typed process-fatal error is the only ticket into the process-level
    set. A transient exception stays what it always was: this Run skips the
    sink and says so once, the next Run calls the sink again -- and says so
    again, because for this class every Run genuinely discovers the failure
    anew. Escalating every exception would take a whole process's sinks
    down over one bad call.
    """
    flaky = _RunLocalErrorSink(name="flaky")
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([flaky, capture], process_instance_id="proc-local")

    fanout.emit(RunStarted(purpose="chat"), _envelope("r1"))
    fanout.flush_barrier("r1")
    fanout.emit(RunStarted(purpose="chat"), _envelope("r2"))

    assert (flaky.prepare_calls, flaky.commit_calls) == (2, 2), (
        "a run-local error does not outlive its Run"
    )
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 2, "each Run that discovers it says so once"
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2"]
