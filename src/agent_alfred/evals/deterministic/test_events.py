"""EventSink failures must not steer the Run (issue #3 failure matrix)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BestEffortFlushResult,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    PostCommit,
    RunStarted,
    SequencedEvent,
    UnsequencedEvent,
    event_json_default,
)
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink

SECRET = "supersecret-key-value"


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
        root=tmp_path / "traces",
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
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
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
