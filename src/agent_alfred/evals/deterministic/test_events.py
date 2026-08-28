"""EventSink failures must not steer the Run (issue #3 failure matrix)."""

from __future__ import annotations

import json
import sqlite3

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    CapturingSink,
    FanOutSink,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings

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

    def flush(self):
        from agent_alfred.events import BestEffortFlushResult

        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


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
        assert all(
            getattr(notice, "code", None) == "sink_disabled" for notice in notices
        )
    finally:
        host.close()
