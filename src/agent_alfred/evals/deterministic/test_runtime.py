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
    after_recorded_snapshot: threading.Event | None = None,
    before_recording_failed: threading.Event | None = None,
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
        after_recorded_snapshot=after_recorded_snapshot,
        before_recording_failed=before_recording_failed,
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


def test_a_new_host_reuses_session_record_as_working_memory() -> None:
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


class PublishExplodingFanout(FanOutSink):
    """The central publish path itself raises on run.finished."""

    def __init__(self, *args, secret: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._secret = secret

    def emit(self, payload, envelope=None):
        if getattr(payload, "name", None) == "run.finished":
            raise RuntimeError(f"fanout exploded {self._secret}")
        return super().emit(payload, envelope)


def test_run_finished_publish_failure_is_merged_into_the_barrier_result() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    secret = "supersecret-key-value"
    fanout = PublishExplodingFanout(
        [CapturingSink(name="capture", flush_at_run_end=True)],
        process_instance_id="proc-pub-boom",
        secret=secret,
    )
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-pub-boom",
        secrets=(secret,),
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        result = host.wait(submitted.run_id)
        # The reply is delivered exactly as produced; tracing must not be able
        # to rewrite a completed run into a business failure.
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        raw = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
        payload = json.loads(raw)
        assert payload["trace_incomplete"] is True
        reason = payload["trace_incomplete_reason"]
        assert "run.finished publish failed" in reason
        assert "RuntimeError" in reason
        assert len(reason) <= 500
        # The reason went through the central redactor: no secret, no raw text.
        assert secret not in reason
        assert secret not in raw
        rows = conn.execute(
            "SELECT role, json_extract(content, '$[0].text') FROM agent_log"
        ).fetchall()
        assert rows == [("user", "hi"), ("assistant", "pong")]
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()


# --- the recorder stays out of Host private state (slice-1 re-review) --------


def test_run_recorder_only_uses_narrow_seams() -> None:
    """The recorder drives the lifecycle through the narrow coordinator and
    store seams; the state machine's invariants must not be bypassable from
    inside it. Any `.<host-attribute>` access here is a regression."""
    import inspect

    from agent_alfred.runtime import recording

    source = inspect.getsource(recording)
    assert "._host" not in source
    forbidden = (
        "_lock",
        "_coord",
        "_states",
        "_done",
        "_results",
        "_active_summary",
        "_queue",
        "_conn",
        "_db_lock",
        "_fanout",  # reached via self._fanout, never via a host reference
        "_redactor",
        "_before_recording_commit",
        "_after_recorded_snapshot",
        "_before_recording_failed",
    )
    for attribute in forbidden:
        assert f"h.{attribute}" not in source, attribute
        assert f"host.{attribute}" not in source, attribute
    init = inspect.signature(recording.RunRecorder.__init__)
    assert "host" not in init.parameters
    # The narrow seams exist and are the documented transitions.
    for method in (
        "recording_enter_pending",
        "recording_enter_failed",
        "recording_publish_recorded_then_release",
        "publish_run_result",
        "notify_run_done",
    ):
        assert callable(getattr(RuntimeHost, method))


def test_unrecorded_projection_survives_every_rejection() -> None:
    hold = threading.Event()
    host, _conn, _capture = _host(["pong"], before_recording_commit=hold)
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        for _ in range(3):
            rejected = host.submit(SubmitRequest(message="busy"))
            assert rejected.kind == "run_in_progress"
            projection = host.snapshot().unrecorded_terminal_projection
            assert projection is not None
            assert projection.run_id == first.run_id, (
                "the second Run must never overwrite the bounded slot"
            )
        hold.set()
        host.wait(first.run_id)
    finally:
        hold.set()
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


def test_recording_failed_snapshot_is_authoritative_before_503() -> None:
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
        host.wait(submitted.run_id)
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.active_run is not None
        assert snap.active_run.recording_state == "failed"
        assert snap.unrecorded_terminal_projection is not None
        assert snap.unrecorded_terminal_projection.recording_state == "failed"
        assert snap.unrecorded_terminal_projection.run_id == submitted.run_id
        again = host.submit(SubmitRequest(message="next"))
        assert again.kind == "recording_unavailable"
        assert again.snapshot is not None
        assert again.snapshot.coordinator_state == "recording_failed"
        assert again.snapshot.active_run is not None
        assert again.snapshot.active_run.recording_state == "failed"
        assert again.snapshot.unrecorded_terminal_projection is not None
        assert again.snapshot.unrecorded_terminal_projection.recording_state == (
            "failed"
        )
    finally:
        host.close()


def test_pending_to_recorded_has_no_idle_unrecorded_window() -> None:
    after_recorded = threading.Event()
    host, _conn, _ = _host(["pong"], after_recorded_snapshot=after_recorded)
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        _wait_until(
            lambda: host.snapshot().active_run is not None
            and host.snapshot().active_run.recording_state == "recorded"
        )
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_pending"
        assert snap.active_run is not None
        assert snap.active_run.recording_state == "recorded"
        second = host.submit(SubmitRequest(message="two"))
        assert second.kind == "run_in_progress"
        after_recorded.set()
        host.wait(first.run_id)
        idle = host.snapshot()
        assert idle.coordinator_state == "idle"
        assert idle.active_run is None
        third = host.submit(SubmitRequest(message="three"))
        assert third.kind == "accepted"
        after_recorded.set()
        host.wait(third.run_id)
    finally:
        after_recorded.set()
        host.close()


def test_pending_to_failed_never_returns_503_with_pending_snapshot() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(
        conn,
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE")
        and "finished_at" in sql,
    )
    before_failed = threading.Event()
    host, _conn, _ = _host(
        ["pong"], conn=wrapped, before_recording_failed=before_failed
    )
    seen: list[tuple[str, str | None, str | None]] = []
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            result = host.submit(SubmitRequest(message="x"))
            snap = result.snapshot or host.snapshot()
            active = (
                None
                if snap.active_run is None
                else snap.active_run.recording_state
            )
            proj = snap.unrecorded_terminal_projection
            proj_state = None if proj is None else proj.recording_state
            seen.append((result.kind, active, proj_state))
            if result.kind == "recording_unavailable":
                assert snap.coordinator_state == "recording_failed"
                assert active == "failed"
                assert proj_state == "failed"
            time.sleep(0.005)

    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        worker = threading.Thread(target=hammer)
        worker.start()
        time.sleep(0.05)
        before_failed.set()
        host.wait(first.run_id)
        time.sleep(0.05)
        stop.set()
        worker.join(timeout=2)
        for kind, active, proj_state in seen:
            if kind == "recording_unavailable":
                assert active == "failed"
                assert proj_state == "failed"
            if kind == "accepted":
                raise AssertionError("a second Run was admitted during recording")
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.active_run is not None
        assert snap.active_run.recording_state == "failed"
    finally:
        stop.set()
        before_failed.set()
        host.close()


def test_run_telemetry_aggregates_every_usage_field() -> None:
    from decimal import Decimal

    from agent_alfred.messages import TextBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelError,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )

    secret = "supersecret-key-value"
    aborted = ModelError(
        retryable=True,
        status_code=None,
        body_excerpt="incomplete",
        attempt_id="att-abort",
        code="incomplete_stream",
    )
    result = ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id="att-abort",
                streamed=True,
                outcome="aborted",
                usage=Usage(
                    total_input_tokens=10,
                    uncached_input_tokens=8,
                    cache_read_tokens=2,
                    cache_write_tokens=0,
                    output_tokens=None,
                    reasoning_tokens=3,
                    endpoint_reported_cost_usd=Decimal("0.123456789012345678"),
                    raw={
                        "prompt_tokens": 10,
                        "api_key": secret,
                        "nested": {"password": secret},
                    },
                ),
                error=aborted,
            ),
            AttemptRecord(
                attempt_id="att-ok",
                streamed=False,
                outcome="committed",
                usage=Usage(total_input_tokens=4, output_tokens=5),
            ),
        ),
        response=ModelResponse(
            blocks=(TextBlock("ok"),),
            stop_reason="end_turn",
            model=ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash"),
        ),
        final_error=None,
    )
    host, conn, _ = _host([result], secrets=(secret,))
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hi"))
        host.wait(submitted.run_id)
        raw = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
        payload = json.loads(raw)
        attempts = payload["attempts"]
        assert len(attempts) == 2
        first = attempts[0]
        assert first["attempt_id"] == "att-abort"
        assert first["streamed"] is True
        assert first["outcome"] == "aborted"
        usage = first["usage"]
        assert usage["total_input_tokens"] == 10
        assert usage["uncached_input_tokens"] == 8
        assert usage["cache_read_tokens"] == 2
        assert usage["cache_write_tokens"] == 0
        assert usage["output_tokens"] is None
        assert usage["reasoning_tokens"] == 3
        assert usage["endpoint_reported_cost_usd"] == "0.123456789012345678"
        assert usage["raw"]["prompt_tokens"] == 10
        assert secret not in json.dumps(payload)
        assert attempts[1]["usage"]["total_input_tokens"] == 4
        log_tel = conn.execute(
            "SELECT telemetry FROM agent_log WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchall()
        assert log_tel == [(None,), (None,)]
    finally:
        host.close()


def test_events_none_and_inference_probe_still_record_usage() -> None:
    from agent_alfred.messages import TextBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )
    from agent_alfred.runtime.telemetry import serialize_run_telemetry

    usage = Usage(total_input_tokens=9, output_tokens=1)
    model_result = ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id="probe-1",
                streamed=False,
                outcome="committed",
                usage=usage,
            ),
        ),
        response=ModelResponse(
            blocks=(TextBlock("ok"),),
            stop_reason="end_turn",
            model=ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash"),
        ),
        final_error=None,
    )
    serialized = json.loads(
        serialize_run_telemetry((model_result,), False, None, redactor=None)
    )
    assert serialized["attempts"][0]["usage"]["total_input_tokens"] == 9
    host, conn, _ = _host([model_result])
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="probe", purpose="inference_probe")
        )
        host.wait(submitted.run_id)
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
        raw = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
        payload = json.loads(raw)
        assert payload["attempts"][0]["usage"]["total_input_tokens"] == 9
    finally:
        host.close()


def test_chat_outcomes_write_the_specified_message_pairs() -> None:
    from agent_alfred.settings import CONTROLLED_FAILURE_TEXT

    cases = [
        ("completed", ["pong"], "pong"),
        ("max_steps", ["unused"], None),
        (
            "failed",
            [RuntimeError("provider exploded sk-secret")],
            CONTROLLED_FAILURE_TEXT,
        ),
        ("interrupted", [KeyboardInterrupt()], None),
    ]
    for outcome, script, expected_assistant in cases:
        settings = Settings(max_steps=0) if outcome == "max_steps" else Settings()
        host, conn, _ = _host(script, settings=settings)
        host.start()
        try:
            submitted = host.submit(SubmitRequest(message="hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == outcome
            rows = conn.execute(
                "SELECT role, json_extract(content, '$[0].text') FROM agent_log "
                "WHERE run_id = ? ORDER BY id",
                (submitted.run_id,),
            ).fetchall()
            roles = [row[0] for row in rows]
            texts = [row[1] for row in rows]
            assert roles[0] == "user"
            assert texts[0] == "hello"
            if outcome == "interrupted":
                assert roles == ["user"]
            else:
                assert roles == ["user", "assistant"]
                if expected_assistant is not None:
                    assert texts[1] == expected_assistant
                if outcome == "failed":
                    assert "provider exploded" not in texts[1]
                    assert "sk-secret" not in texts[1]
                    assert result.outcome == "failed"
        finally:
            host.close()


def test_file_database_survives_closing_the_host_and_connection(tmp_path) -> None:
    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    schema.migrate(conn)
    host, conn, _ = _host(["remembered"], conn=conn)
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message="hello file", session_id=session_id)
        )
        host.wait(submitted.run_id)
    finally:
        host.close()
        conn.close()

    conn2 = sqlite3.connect(str(path), check_same_thread=False)
    model = ScriptedModel(["second"])
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host2 = RuntimeHost(
        conn=conn2,
        factory=ScriptedModelFactory(model),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-reopen"),
        process_instance_id="proc-reopen",
    )
    host2.start()
    try:
        stored = conn2.execute(
            "SELECT role, json_extract(content, '$[0].text') FROM agent_log "
            "ORDER BY id"
        ).fetchall()
        assert ("user", "hello file") in stored
        assert ("assistant", "remembered") in stored
        again = host2.submit(
            SubmitRequest(message="again", session_id=session_id)
        )
        host2.wait(again.run_id)
        roles = [message.role for message in model.requests[0].messages]
        assert roles == ["user", "assistant", "user"]
    finally:
        host2.close()
        conn2.close()
