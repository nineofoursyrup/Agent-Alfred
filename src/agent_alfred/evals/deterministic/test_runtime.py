"""Admission, recovery, finalizer, and the recording lease."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BarrierFlushResult,
    CapturingSink,
    FanOutSink,
    FlushResult,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import MAX_STEPS_REACHED_TEXT, Settings


class HoldingBarrierSink:
    def __init__(self, hold: threading.Event, *, name: str = "hold"):
        self.name = name
        self.flush_at_run_end = True
        self._hold = hold
        self.events: list[SequencedEvent] = []

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared
        self.events.append(event)

    def flush(self) -> FlushResult:
        self._hold.wait()
        return BarrierFlushResult(outcome="flushed", dropped_events=0)

    def close(self) -> None:
        self._hold.set()


class FailingBarrierSink:
    def __init__(self, *, name: str = "fail-barrier"):
        self.name = name
        self.flush_at_run_end = True

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event

    def flush(self) -> FlushResult:
        return BarrierFlushResult(outcome="failed", dropped_events=0)

    def close(self) -> None:
        return None


def _host(
    script: list | None = None,
    *,
    settings: Settings | None = None,
    extra_sinks: list | None = None,
    publish_work=None,
    conn: sqlite3.Connection | None = None,
    secrets: tuple[str, ...] = (),
    before_recording_commit: threading.Event | None = None,
) -> tuple[RuntimeHost, sqlite3.Connection, CapturingSink]:
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(conn)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    fanout = FanOutSink(sinks, process_instance_id="proc-test")
    model = ScriptedModel(script or ["pong"])
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        settings=settings or Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-test",
        publish_work=publish_work,
        secrets=secrets,
        before_recording_commit=before_recording_commit,
    )
    return host, conn, capture


def test_prompt_preview_redacts_loaded_secrets() -> None:
    host, conn, _ = _host(["ok"], secrets=("supersecret-key-value",))
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="my key is supersecret-key-value please")
        )
        host.wait(submitted.run_id)
        preview = conn.execute("SELECT prompt_preview FROM runs").fetchone()[0]
        assert "supersecret-key-value" not in preview
        assert "***" in preview
    finally:
        host.close()


def test_a_new_host_reuses_session_history_from_sqlite() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    host1, conn, _ = _host(["first-reply"], conn=conn)
    host1.start()
    try:
        session_id = host1.create_session()
        first = host1.submit(
            SubmitRequest(message="hello", session_id=session_id)
        )
        host1.wait(first.run_id)
    finally:
        host1.close()
    model = ScriptedModel(["second-reply"])
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host2 = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-2"),
        process_instance_id="proc-2",
    )
    host2.start()
    try:
        second = host2.submit(
            SubmitRequest(message="again", session_id=session_id)
        )
        result = host2.wait(second.run_id)
        assert result.outcome == "completed"
        roles = [message.role for message in model.requests[0].messages]
        assert roles == ["user", "assistant", "user"]
    finally:
        host2.close()


def test_scripted_chat_run_records_one_message_pair_and_run_telemetry() -> None:
    host, conn, _ = _host(["pong"])
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message="ping", session_id=session_id)
        )
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        assert result.reply is not None
        assert message_plain_text(result.reply) == "pong"
        rows = conn.execute(
            "SELECT role, json_extract(content, '$[0].text'), run_id, telemetry "
            "FROM agent_log ORDER BY id"
        ).fetchall()
        assert rows == [
            ("user", "ping", submitted.run_id, None),
            ("assistant", "pong", submitted.run_id, None),
        ]
        run = conn.execute(
            "SELECT phase, outcome, purpose, telemetry FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert run[0] == "finished"
        assert run[1] == "completed"
        assert run[2] == "chat"
        telemetry = json.loads(run[3])
        assert telemetry["trace_incomplete"] is False
        assert host.snapshot().unrecorded_terminal_projection is None
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()


def test_second_submit_while_busy_is_409_and_does_not_insert_a_run() -> None:
    hold = threading.Event()
    published: list[object] = []
    host, conn, _ = _host(["pong"], before_recording_commit=hold)

    def _publish(item):
        published.append(item)
        host._queue.put_nowait(item)

    host._publish_work = _publish
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert first.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        projection = host.snapshot().unrecorded_terminal_projection
        assert projection is not None
        assert projection.run_id == first.run_id
        second = host.submit(SubmitRequest(message="two"))
        assert second.kind == "run_in_progress"
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        accepted = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE phase = 'accepted'"
        ).fetchone()
        assert accepted == (0,)
        assert len(published) == 1
        assert host.snapshot().unrecorded_terminal_projection.run_id == first.run_id
        hold.set()
        host.wait(first.run_id)
        third = host.submit(SubmitRequest(message="three"))
        assert third.kind == "accepted"
        hold.set()
        host.wait(third.run_id)
    finally:
        hold.set()
        host.close()


def test_recording_failure_keeps_the_projection_and_returns_503() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(
        conn,
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE")
        and "finished_at" in sql,
    )
    host, _conn, _ = _host(["pong"], conn=wrapped)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.unrecorded_terminal_projection is not None
        assert snap.unrecorded_terminal_projection.run_id == submitted.run_id
        assert snap.unrecorded_terminal_projection.recording_state == "failed"
        again = host.submit(SubmitRequest(message="next"))
        assert again.kind == "recording_unavailable"
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    finally:
        host.close()


def test_accepted_persist_failure_does_not_return_accepted() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(conn, when=lambda sql: "INSERT INTO runs" in sql)
    host, _, _ = _host(["pong"], conn=wrapped)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.kind == "admission_failed"
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()


def test_handoff_failure_finalizes_interrupted() -> None:
    def boom(_item):
        raise RuntimeError("queue full")

    host, conn, _ = _host(["pong"], publish_work=boom)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.kind == "admission_failed"
        row = conn.execute("SELECT phase, outcome FROM runs").fetchone()
        assert row == ("finished", "interrupted")
        assert conn.execute("SELECT started_at FROM runs").fetchone() == (None,)
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
    finally:
        host.close()


def test_startup_recovery_marks_leftover_runs_interrupted() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    schema.insert_session(conn, session_id="s1", created_at="2026-08-28T00:00:00Z")
    schema.insert_accepted_run(
        conn,
        run_id="orphan-accepted",
        purpose="chat",
        session_id="s1",
        gateway="cli",
        accepted_at="2026-08-28T00:00:00Z",
    )
    running_rev = schema.allocate_activity_revision(conn)
    schema.insert_accepted_run(
        conn,
        run_id="orphan-running",
        purpose="chat",
        session_id="s1",
        gateway="cli",
        accepted_at="2026-08-28T00:00:01Z",
    )
    schema.update_run_phase(
        conn,
        run_id="orphan-running",
        from_phase="accepted",
        to_phase="running",
        activity_revision=running_rev,
        started_at="2026-08-28T00:00:02Z",
        session_id="s1",
    )
    conn.commit()
    host, conn, _ = _host(["pong"], conn=conn)
    host.start()
    try:
        stored = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT run_id, phase, outcome, started_at FROM runs"
            )
        }
        assert stored["orphan-accepted"][0] == "finished"
        assert stored["orphan-accepted"][1] == "interrupted"
        assert stored["orphan-accepted"][2] is None
        assert stored["orphan-running"][0] == "finished"
        assert stored["orphan-running"][1] == "interrupted"
        assert stored["orphan-running"][2] is not None
        submitted = host.submit(SubmitRequest(message="after-recovery"))
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
    finally:
        host.close()


def test_inference_probe_persists_telemetry_without_messages() -> None:
    host, conn, _ = _host(["ok"])
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="probe", purpose="inference_probe")
        )
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
        row = conn.execute(
            "SELECT purpose, session_id, phase, outcome, telemetry FROM runs"
        ).fetchone()
        assert row[0] == "inference_probe"
        assert row[1] is None
        assert row[2] == "finished"
        assert row[3] == "completed"
        assert json.loads(row[4])["attempts"]
    finally:
        host.close()


def test_max_steps_run_records_the_controlled_message() -> None:
    host, conn, _ = _host(["unused"], settings=Settings(max_steps=0))
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "max_steps"
        text = conn.execute(
            "SELECT json_extract(content, '$[0].text') FROM agent_log"
            " WHERE role = 'assistant'"
        ).fetchone()[0]
        assert text == MAX_STEPS_REACHED_TEXT
    finally:
        host.close()


def test_barrier_failure_marks_trace_incomplete_but_still_records() -> None:
    host, conn, _ = _host(["pong"], extra_sinks=[FailingBarrierSink()])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        raw = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
        payload = json.loads(raw)
        assert payload["trace_incomplete"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_log WHERE role = 'assistant'"
        ).fetchone() == (1,)
    finally:
        host.close()


def test_seq_state_revision_and_activity_revision_are_not_compared() -> None:
    host, conn, capture = _host(["pong"])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        host.wait(submitted.run_id)
        seqs = [event.seq for event in capture.events]
        state_revision = host.snapshot().state_revision
        activity = conn.execute(
            "SELECT activity_revision FROM runs"
        ).fetchone()[0]
        assert seqs
        assert state_revision >= 1
        assert activity >= 1
        # They happen to be integers. Nothing here treats them as one clock.
        assert host.snapshot().process_instance_id == "proc-test"
    finally:
        host.close()


def test_chat_run_rejects_a_second_assistant_row_for_the_same_run() -> None:
    host, conn, _ = _host(["pong"])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        host.wait(submitted.run_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO agent_log (
                     session_id, role, content, source, created_at, run_id
                   ) VALUES (
                     'x', 'assistant', ?, 'cli', '2026-08-28T00:00:00Z', ?
                   )""",
                (json.dumps([{"type": "text", "text": "dup"}]), submitted.run_id),
            )
    finally:
        host.close()


class _FailOn:
    """Connection wrapper that raises on matching SQL. Forwards everything else."""

    def __init__(self, inner: sqlite3.Connection, when):
        self._inner = inner
        self._when = when

    def execute(self, sql, parameters=()):
        if self._when(sql):
            raise sqlite3.OperationalError("injected write failure")
        return self._inner.execute(sql, parameters)

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")
