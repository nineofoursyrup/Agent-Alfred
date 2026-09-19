"""Admission, recovery, finalizer, and the recording lease."""

from __future__ import annotations

import json
import queue
import sqlite3
import sys
import threading
from dataclasses import replace

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
)
from agent_alfred.evals.deterministic._thread_test_helpers import EnteredEvent
from agent_alfred.events import (
    BarrierFlushResult,
    CapturingSink,
    FanOutSink,
    FlushResult,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import message_plain_text, text_message
from agent_alfred.model import (
    ClientSnapshot,
    ModelAssignment,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.redact import Redactor
from agent_alfred.runtime.admission import RunAdmission
from agent_alfred.runtime.config import (
    MutableAssignmentProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.recording import RecordingStore
from agent_alfred.runtime.snapshot import (
    RuntimeSnapshot,
)
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import MAX_STEPS_REACHED_TEXT, Settings
from agent_alfred.wiring import build_default_host


def _reject_handoff(_item):
    raise RuntimeError("queue full")


def test_build_default_host_close_releases_owned_connection_and_leases_once(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import wiring as wiring_module

    real_open_database = wiring_module.open_database
    captured_connections = []

    def capture_database(state, _rollback=None):
        connection = real_open_database(state, _rollback=_rollback)
        captured_connections.append(connection)
        return connection

    monkeypatch.setattr(wiring_module, "open_database", capture_database)
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    conn = captured_connections[0]
    assert host.close() is True
    assert host.close() is True
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


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

    def flush(self, run_id: str) -> FlushResult:
        del run_id
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

    def flush(self, run_id: str) -> FlushResult:
        del run_id
        return BarrierFlushResult(outcome="failed", dropped_events=0)

    def close(self) -> None:
        return None


_GATE_DECISION = (
    '{"retrieve":true,"query":"runtime-fixture",'
    '"reason_code":"conservative_retrieve"}'
)


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
    snapshot_provider=None,
    factory=None,
    chat_script: bool = True,
) -> tuple[RuntimeHost, sqlite3.Connection, CapturingSink]:
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(conn)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    fanout = FanOutSink(sinks, process_instance_id="proc-test")
    responses = script or ["pong"]
    if chat_script:
        # Each fixture entry is an answer for a separate chat Run. The gate
        # makes its own real scripted request before that answer.
        responses = [item for answer in responses for item in (_GATE_DECISION, answer)]
    model = ScriptedModel(responses)
    host = RuntimeHost(
        conn=conn,
        factory=factory or ScriptedModelFactory(model),
        settings=settings or Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-test",
        publish_work=publish_work,
        secrets=secrets,
        before_recording_commit=before_recording_commit,
        after_recorded_snapshot=after_recorded_snapshot,
        before_recording_failed=before_recording_failed,
        snapshot_provider=snapshot_provider,
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
    model = ScriptedModel([_GATE_DECISION, "second-reply"])
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
        assert len(model.requests) == 2  # gate followed by answer
        roles = [message.role for message in model.requests[1].messages]
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


def test_wait_consumes_the_complete_result_and_waiter_exactly_once() -> None:
    host, _conn, _capture = _host(["pong"])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.run_id is not None
        result = host.wait(submitted.run_id)

        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "pong"
        assert len(result.model_results) == 2  # gate and answer share the Run
        assert host._done == {}
        assert host._results == {}
        with pytest.raises(KeyError, match=submitted.run_id):
            host.wait(submitted.run_id)
    finally:
        host.close()


def test_second_submit_while_busy_is_409_and_does_not_insert_a_run() -> None:
    hold = EnteredEvent()
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
        assert hold.entered.wait(2.0), "recording gate was not reached"
        assert host.snapshot().coordinator_state == "recording_pending"
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
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE RUNS")
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


def test_committed_acceptance_is_reconciled_when_commit_then_raises() -> None:
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)

    class CommitThenRaise:
        def __init__(self):
            self.armed = False

        def commit(self):
            raw.commit()
            if self.armed:
                self.armed = False
                raise sqlite3.OperationalError("commit outcome was uncertain")

        def __getattr__(self, name):
            return getattr(raw, name)

    uncertain = CommitThenRaise()
    host, conn, _ = _host(["pong"], conn=uncertain)
    host.start()
    try:
        uncertain.armed = True
        first = host.submit(SubmitRequest(message="uncertain"))

        assert first.kind == "admission_failed"
        assert conn.execute(
            "SELECT phase, outcome, started_at FROM runs"
        ).fetchone() == ("finished", "interrupted", None)
        assert conn.execute(
            "SELECT COUNT(*) FROM runs WHERE phase = 'accepted'"
        ).fetchone() == (0,)
        assert host.snapshot().coordinator_state == "idle"
        assert host._pending_handoff == set()

        second = host.submit(SubmitRequest(message="next"))
        assert second.kind == "accepted"
        assert host.wait(second.run_id).outcome == "completed"
    finally:
        host.close()


@pytest.mark.parametrize("failure_step", ("result", "release", "notify"))
def test_recorder_cleanup_owner_survives_two_post_commit_failures(
    failure_step: str,
) -> None:
    """Host close resumes settlement without notifying past a missing step."""
    host, _conn, _ = _host(["pong"])
    originals = {
        "result": host.publish_run_result,
        "release": host.recording_publish_recorded_then_release,
        "notify": host.notify_run_done,
    }
    calls = 0
    trace: list[str] = []

    def wrap(step: str):
        original = originals[step]

        def invoke(*args):
            nonlocal calls
            trace.append(step)
            if step == failure_step:
                calls += 1
                if calls <= 2:
                    raise RuntimeError(f"{step} unavailable")
            return original(*args)

        return invoke

    host.publish_run_result = wrap("result")
    host.recording_publish_recorded_then_release = wrap("release")
    host.notify_run_done = wrap("notify")
    host.start()
    submitted = host.submit(SubmitRequest(message="ping"))
    assert submitted.kind == "accepted"
    assert submitted.run_id is not None

    assert host.close(timeout=2) is True
    assert calls == 3
    assert host.wait(submitted.run_id, timeout=0).outcome == "completed"
    assert host.snapshot().coordinator_state == "idle"
    if failure_step != "notify":
        assert trace.index("notify") > max(
            index for index, step in enumerate(trace) if step == failure_step
        )
    else:
        assert trace[-1] == "notify"


@pytest.mark.parametrize("edge", ("reconcile_return", "capture_return"))
def test_recording_decision_return_edges_remain_host_reachable(
    monkeypatch, edge: str
) -> None:
    """A committed decision survives before Python stores its return value."""
    import dis

    from agent_alfred.runtime.recording import RunRecorder

    host, conn, _ = _host(["pong"])
    if edge == "reconcile_return":
        code = RunRecorder._reconcile_finalize.__code__
        instructions = tuple(dis.get_instructions(code))
        target = next(
            instruction.offset
            for index, instruction in enumerate(instructions[1:], 1)
            if instruction.opname == "RETURN_VALUE"
            and instructions[index - 1].opname == "LOAD_CONST"
            and instructions[index - 1].argval is True
        )
    else:
        code = RunRecorder.settle.__code__
        instructions = tuple(dis.get_instructions(code))
        capture_load = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "LOAD_ATTR"
            and instruction.argval == "capture_recording_decision"
        )
        capture_call = next(
            index
            for index, instruction in enumerate(
                instructions[capture_load:], capture_load
            )
            if instruction.opname in {"CALL", "CALL_KW"}
            and instructions[index + 1].opname == "POP_TOP"
        )
        target = instructions[capture_call + 1].offset
    reached = threading.Event()
    control = SystemExit("recording decision returned before publication")
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        with claimed_monitoring_tool(
            f"recording-decision-{edge}-owner", local_codes=(code,)
        ) as tool_id:

            def interrupt_after_decision(actual_code, offset) -> None:
                if actual_code is code and offset == target and not reached.is_set():
                    reached.set()
                    raise control

            sys.monitoring.register_callback(
                tool_id,
                sys.monitoring.events.INSTRUCTION,
                interrupt_after_decision,
            )
            sys.monitoring.set_local_events(
                tool_id, code, sys.monitoring.events.INSTRUCTION
            )
            submitted = host.submit(
                SubmitRequest(message="ping", wait_for_result=False)
            )
            assert submitted.kind == "accepted"
            assert submitted.run_id is not None
            assert reached.wait(2), "worker did not cross the decision return edge"

        assert conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone() == ("finished", "completed")
        pending = host.snapshot()
        assert pending.coordinator_state == "recording_pending"
        assert pending.unrecorded_terminal_projection is not None

        assert host.close(timeout=2) is True
        assert host.snapshot().coordinator_state == "idle"
        assert host.snapshot().unrecorded_terminal_projection is None
        assert worker_failures == [control]
    finally:
        host.close(timeout=2)


def test_handoff_failure_finalizes_interrupted() -> None:
    host, conn, _ = _host(["pong"], publish_work=_reject_handoff)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.kind == "handoff_failed"
        row = conn.execute("SELECT phase, outcome FROM runs").fetchone()
        assert row == ("finished", "interrupted")
        assert conn.execute("SELECT started_at FROM runs").fetchone() == (None,)
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
    finally:
        host.close()


def test_handoff_failures_discard_unreturnable_result_slots() -> None:
    host, _conn, _ = _host(["must not execute"], publish_work=_reject_handoff)
    host.start()
    try:
        for _ in range(3):
            submitted = host.submit(SubmitRequest(message="ping"))

            assert submitted.kind == "handoff_failed"
            assert submitted.run_id is not None
            assert submitted.run_id not in host._done
            assert submitted.run_id not in host._results
            assert host.snapshot().coordinator_state == "idle"

        assert host._done == {}
        assert host._results == {}
    finally:
        host.close()


def test_handoff_and_finalize_failure_publish_consistent_terminal_state() -> None:
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)
    fail_finalize = [True]
    wrapped = _FailOn(
        raw,
        when=lambda sql: fail_finalize[0]
        and sql.lstrip().upper().startswith("UPDATE RUNS")
        and "FINISHED_AT" in sql.upper(),
    )

    host, conn, _ = _host(
        ["must not execute"], conn=wrapped, publish_work=_reject_handoff
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="ping"))
        assert submitted.kind == "handoff_failed"
        assert submitted.run_id is not None

        row = conn.execute(
            "SELECT run_id, session_id, phase, outcome, started_at FROM runs"
        ).fetchone()
        run_id, session_id, phase, outcome, started_at = row
        assert run_id == submitted.run_id
        assert session_id is not None
        assert (phase, outcome, started_at) == ("accepted", None, None)
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)

        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.active_run is not None
        assert snap.unrecorded_terminal_projection is not None
        summary = snap.active_run
        projection = snap.unrecorded_terminal_projection
        assert (
            summary.phase,
            summary.outcome,
            summary.recording_state,
        ) == ("finished", "interrupted", "failed")
        assert (
            projection.outcome,
            projection.recording_state,
            projection.reply_text,
            projection.error,
        ) == ("interrupted", "failed", None, "handoff_failed")
        assert summary.run_id == projection.run_id == run_id
        assert summary.session_id == projection.session_id == session_id

        refused = host.submit(SubmitRequest(message="still closed"))
        assert refused.kind == "recording_unavailable"
        assert refused.snapshot == snap
        assert submitted.run_id not in host._done
        assert submitted.run_id not in host._results
    finally:
        host.close()

    fail_finalize[0] = False
    recovered, _conn, _ = _host(["pong"], conn=raw)
    recovered.start()
    try:
        stored = raw.execute(
            "SELECT phase, outcome, started_at FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert stored == ("finished", "interrupted", None)
        assert recovered.snapshot().coordinator_state == "idle"
    finally:
        recovered.close()
        raw.close()


def test_unwaited_handoff_failure_still_publishes_and_notifies_without_a_slot(
) -> None:
    host, _conn, _ = _host(["must not execute"], publish_work=_reject_handoff)
    published: list[str] = []
    notified: list[str] = []
    publish_run_result = host.publish_run_result
    notify_run_done = host.notify_run_done

    def track_result(run_id, result):
        published.append(run_id)
        publish_run_result(run_id, result)

    def track_notification(run_id):
        notified.append(run_id)
        notify_run_done(run_id)

    host.publish_run_result = track_result
    host.notify_run_done = track_notification
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="ping", wait_for_result=False)
        )

        assert submitted.kind == "handoff_failed"
        assert submitted.run_id is not None
        assert published == [submitted.run_id]
        assert notified == [submitted.run_id]
        assert host._done == {}
        assert host._results == {}
    finally:
        host.close()


@pytest.mark.parametrize("failure_step", ("result", "notify"))
def test_terminal_publication_failure_still_retires_handoff_ownership(
    failure_step: str,
) -> None:
    host, _conn, _ = _host(["must not execute"], publish_work=_reject_handoff)
    real_result = host.publish_run_result
    real_notify = host.notify_run_done

    def publish_result(run_id, result):
        if failure_step == "result":
            raise RuntimeError("result publication failed")
        real_result(run_id, result)

    def notify(run_id):
        if failure_step == "notify":
            raise RuntimeError("done notification failed")
        real_notify(run_id)

    host.publish_run_result = publish_result
    host.notify_run_done = notify
    host.start()

    with pytest.raises(
        RuntimeError, match="(?:result publication|done notification) failed"
    ):
        host.submit(SubmitRequest(message="ping"))

    assert host._pending_handoff == set()
    assert host._done == {}
    assert host._results == {}
    assert host.close(timeout=0.05) is True


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
def test_close_fence_resumes_after_control_before_retirement(control_type) -> None:
    host, conn, _ = _host(["must not execute"], publish_work=_reject_handoff)
    complete = host.admission_complete_handoff
    control = control_type("before fence retirement")
    calls = 0

    def interrupt_once(run_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise control
        complete(run_id)

    host.admission_complete_handoff = interrupt_once
    host.start()
    try:
        with pytest.raises(control_type) as caught:
            host.submit(SubmitRequest(message="ping"))

        assert caught.value is control
        assert calls == 2
        assert conn.execute(
            "SELECT phase, outcome, started_at FROM runs"
        ).fetchone() == ("finished", "interrupted", None)
        assert host._pending_handoff == set()
        assert host._done == {}
        assert host._results == {}
        assert host.close(timeout=0.05) is True
    finally:
        host.close(timeout=0.05)


def test_handoff_cleanup_owner_survives_two_retirement_failures() -> None:
    """Host close can resume a fence that submit could not retire."""

    host, _conn, _ = _host(["must not execute"], publish_work=_reject_handoff)
    complete = host.admission_complete_handoff
    attempts = 0

    def fail_twice(run_id):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("fence retirement unavailable")
        complete(run_id)

    host.admission_complete_handoff = fail_twice
    host.start()
    try:
        with pytest.raises(RuntimeError, match="fence retirement unavailable"):
            host.submit(SubmitRequest(message="ping"))

        assert attempts == 2
        assert host._pending_handoff
        assert host.close(timeout=0.05) is True
        assert attempts == 3
        assert host._pending_handoff == set()
    finally:
        host.admission_complete_handoff = complete
        for run_id in tuple(host._pending_handoff):
            complete(run_id)
        host.close(timeout=0.05)


@pytest.mark.parametrize(
    "error_type", (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
)
@pytest.mark.parametrize("after_retire", (False, True), ids=("before", "after"))
def test_public_host_resumes_an_interrupted_successful_handoff_retirement(
    error_type, after_retire: bool
) -> None:
    host, _conn, _ = _host(["pong"])
    original = host.admission_publish_handoff
    error = error_type("retire interrupted")

    def interrupt_retire(item):
        if after_retire:
            original(item)
        else:
            # Publish the monotonic queue-cell decision, then interrupt before
            # the public handoff seam can retire close()'s pending fence.
            host._queue.publish(item, enqueue=True)
        raise error

    host.admission_publish_handoff = interrupt_retire
    host.start()
    with pytest.raises(error_type) as caught:
        host.submit(SubmitRequest(message="ping"))
    assert caught.value is error
    assert host.close(timeout=0.2) is True
    assert host._pending_handoff == set()
    assert host._done == {}
    assert host._results == {}


@pytest.mark.parametrize(
    "error_type", (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
)
@pytest.mark.parametrize("after_release", (False, True), ids=("before", "after"))
def test_public_host_resumes_an_interrupted_pre_handoff_release(
    error_type, after_release: bool
) -> None:
    host, _conn, _ = _host([], factory=_BoomFactory())
    original = host.admission_release
    error = error_type("release interrupted")

    def interrupt_release(run_id):
        if after_release:
            original(run_id)
        raise error

    host.admission_release = interrupt_release
    host.start()
    try:
        if error_type is RuntimeError:
            submitted = host.submit(SubmitRequest(message="ping"))
            assert submitted.kind == "admission_failed"
        else:
            with pytest.raises(error_type) as caught:
                host.submit(SubmitRequest(message="ping"))
            assert caught.value is error
        assert host.snapshot().coordinator_state == "idle"
        assert host.close(timeout=0.2) is True
        assert host._pending_handoff == set()
        assert host._done == {}
        assert host._results == {}
    finally:
        host.close(timeout=0.2)


def test_control_after_reserve_cannot_leave_close_waiting_for_handoff() -> None:
    """The reservation itself belongs to submit's cleanup scope.

    A process-control exception may arrive after the coordinator has taken the
    lease but before ``admission_reserve`` returns its tuple to admission. The
    caller cannot observe that internal boundary; it can only require that the
    failed submit relinquishes every owner and that close subsequently finishes.
    """
    host, _conn, _ = _host(["must not execute"])
    real_reserve = host.admission_reserve
    reserved = threading.Event()
    release = threading.Event()
    submit_finished = threading.Event()
    control = KeyboardInterrupt("after reserve")
    caught: list[BaseException] = []

    def interrupt_after_reserve(*args, **kwargs):
        real_reserve(*args, **kwargs)
        reserved.set()
        assert release.wait(2), "test did not release reserved admission"
        raise control

    host.admission_reserve = interrupt_after_reserve
    host.start()

    def submit() -> None:
        try:
            host.submit(SubmitRequest(message="ping"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            caught.append(exc)
        finally:
            submit_finished.set()

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert reserved.wait(2), "submit did not reserve admission"
        assert host.close(timeout=0) is False
        release.set()
        assert submit_finished.wait(2), "submit did not finish unwinding"
        thread.join(2)

        assert caught == [control]
        assert host.close(timeout=0.2) is True
    finally:
        release.set()
        thread.join(2)
        host.close(timeout=0.2)


def test_control_after_work_is_published_does_not_reclassify_it_unstarted() -> None:
    """A publisher may be interrupted only after the worker owns the item."""
    release_execution = threading.Event()
    model = ScriptedModel([_GATE_DECISION, "pong"], gate=release_execution)
    host, _conn, _ = _host(factory=ScriptedModelFactory(model))
    publish = host.admission_publish_handoff
    control = KeyboardInterrupt("after publication")

    def interrupt_after_publication(item):
        publish(item)
        assert model.entered.wait(2), "published work did not reach execution"
        raise control

    host.admission_publish_handoff = interrupt_after_publication
    host.start()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            host.submit(SubmitRequest(message="ping"))

        assert caught.value is control
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "running"
        assert snapshot.active_run is not None
        assert snapshot.active_run.phase == "running"
    finally:
        release_execution.set()
        host.close(timeout=0.2)


def test_recovery_return_edge_keeps_a_worker_claimed_run_admitted() -> None:
    """A recovered ``True`` is owned before Python can lose its return value.

    The worker has physically claimed the published cell but is held before
    execution can write ``running``. Process control lands at the actual
    inner gap between recovery resolving ``True`` and the settlement slot
    storing it. Recovery must already have transferred that fact; it cannot
    ask the coordinator a second or third time.
    """
    import dis

    from agent_alfred.runtime.host import _HandoffCell

    host, conn, _ = _host(["pong"])
    worker_claimed = threading.Event()
    release_worker = threading.Event()
    real_get = host._queue.get  # noqa: SLF001 - exact handoff seam under test
    real_publish = host.admission_publish_handoff
    real_recover = host.admission_recover_handoff
    first_control = KeyboardInterrupt("first recovered result return")
    pending_controls: list[BaseException] = [first_control]
    recovery_calls = 0

    def gated_get():
        item = real_get()
        if item is not None and not worker_claimed.is_set():
            worker_claimed.set()
            assert release_worker.wait(3.0), "test did not release claimed work"
        return item

    def publish_then_lose_return(item):
        real_publish(item)
        assert worker_claimed.wait(3.0), "worker did not claim the handoff cell"
        raise RuntimeError("publish returned after its effect")

    def count_recovery(run_id, *, settle):
        nonlocal recovery_calls
        recovery_calls += 1
        return real_recover(run_id, settle=settle)

    host._queue.get = gated_get  # type: ignore[method-assign]  # noqa: SLF001
    host.admission_publish_handoff = publish_then_lose_return
    host.admission_recover_handoff = count_recovery
    code = _HandoffCell.recover.__code__
    instructions = tuple(dis.get_instructions(code))
    settle_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname.startswith("LOAD_FAST")
        and instruction.argval == "settle"
    )
    settle_call = next(
        index
        for index, instruction in enumerate(
            instructions[settle_load:], settle_load
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    target = instructions[settle_call + 1].offset
    host.start()
    try:
        with claimed_monitoring_tool(
            "admission-recovery-return-owner", local_codes=(code,)
        ) as tool_id:

            def interrupt_after_recovery_return(actual_code, offset) -> None:
                if (
                    actual_code is code
                    and offset == target
                    and pending_controls
                ):
                    raise pending_controls.pop(0)

            sys.monitoring.register_callback(
                tool_id,
                sys.monitoring.events.INSTRUCTION,
                interrupt_after_recovery_return,
            )
            sys.monitoring.set_local_events(
                tool_id, code, sys.monitoring.events.INSTRUCTION
            )
            with pytest.raises(KeyboardInterrupt) as caught:
                host.submit(SubmitRequest(message="first", wait_for_result=False))

        assert caught.value is first_control
        assert recovery_calls == 1
        assert pending_controls == []
        row = conn.execute(
            "SELECT phase, outcome, started_at FROM runs ORDER BY accepted_at"
        ).fetchone()
        assert row == ("accepted", None, None)
        refused = host.submit(
            SubmitRequest(message="second", wait_for_result=False)
        )
        assert refused.kind == "run_in_progress"
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    finally:
        release_worker.set()
        host.close(timeout=1.0)


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


def _probe_provider() -> MutableAssignmentProvider:
    return MutableAssignmentProvider(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
        api_key="sk-test-probe",
    )


def test_inference_probe_persists_telemetry_without_messages() -> None:
    host, conn, _ = _host(
        ["ok"], snapshot_provider=_probe_provider(), chat_script=False
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id="opencode-go",
                model_id="deepseek-v4-flash",
            )
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


def test_a_system_submit_request_cannot_name_a_chat_session() -> None:
    with pytest.raises(ValueError, match="system Run cannot name a Session"):
        SubmitRequest(
            message="probe",
            purpose="inference_probe",
            session_id="s1",
        )


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
        factory=ScriptedModelFactory(ScriptedModel([_GATE_DECISION, "pong"])),
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


# --- admission and execution stay on narrow seams (slice-1 re-review) --------


def test_admission_and_execution_only_use_narrow_seams() -> None:
    """admission and execution drive the lifecycle through narrow coordinator
    and store seams, the same shape the recorder already uses. Any access to
    the Host's private state from inside them is a regression."""
    import inspect

    from agent_alfred.runtime import admission, execution

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
        "_fanout",
        "_redactor",
        "_settings",
        "_clock",
        "_assistant",
        "_recorder",
        "_factory",
        "_snapshot_provider",
        "_publish_work",
        "_process_instance_id",
        "_admission",
        "_executor",
        "_before_recording_commit",
    )
    for module in (admission, execution):
        source = inspect.getsource(module)
        assert "._host" not in source, module.__name__
        for attribute in forbidden:
            assert f"h.{attribute}" not in source, (module.__name__, attribute)
            assert f"host.{attribute}" not in source, (module.__name__, attribute)
    for cls in (admission.RunAdmission, execution.RunExecutor):
        assert "host" not in inspect.signature(cls.__init__).parameters, cls
    # The narrow seams exist and are the documented transitions.
    for method in (
        "admission_observe",
        "admission_reserve",
        "admission_release",
        "admission_close_idle",
        "admission_fail_recording",
        "admission_discard_result_slot",
        "admission_publish_handoff",
        "admission_recover_handoff",
        "execution_mark_running",
    ):
        assert callable(getattr(RuntimeHost, method))


def _fake_runtime_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        process_instance_id="proc-fake",
        state_revision=1,
        coordinator_state="idle",
        active_run=None,
        unrecorded_terminal_projection=None,
    )


class _FakeAdmissionCoordinator:
    """Minimal coordinator double: records the driven transitions."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.state = "idle"
        self.done: dict[str, threading.Event] = {}
        self.fail_publish = False
        self.reserved_summaries: dict[str, object] = {}
        self.observe_calls = 0
        self.cleanups = {}

    def admission_observe(self):
        self.observe_calls += 1
        if self.state == "recording_failed":
            return "recording_unavailable", _fake_runtime_snapshot()
        if self.state != "idle":
            return "run_in_progress", _fake_runtime_snapshot()
        return "admissible", _fake_runtime_snapshot()

    def admission_reserve(self, run_id, summary, *, wait_for_result):
        if self.state == "recording_failed":
            return "recording_unavailable", _fake_runtime_snapshot()
        if self.state != "idle":
            return "run_in_progress", _fake_runtime_snapshot()
        self.state = "accepted"
        if wait_for_result:
            self.done[run_id] = threading.Event()
        self.reserved_summaries[run_id] = summary
        self.calls.append(("reserve", run_id))
        return "reserved", _fake_runtime_snapshot()

    def admission_release(self, run_id):
        self.state = "idle"
        self.done.pop(run_id, None)
        self.reserved_summaries.pop(run_id, None)
        self.calls.append(("release", run_id))

    def admission_recover_release(self, run_id):
        self.admission_release(run_id)

    def admission_close_idle(self, run_id):
        self.state = "idle"
        self.reserved_summaries.pop(run_id, None)
        self.calls.append(("close_idle", run_id))

    def admission_fail_recording(self, fallback, projection):
        self.state = "recording_failed"
        self.calls.append(
            ("fail_recording", projection.run_id, projection.recording_state)
        )

    def admission_discard_result_slot(self, run_id):
        self.done.pop(run_id, None)
        self.calls.append(("discard_result_slot", run_id))

    def admission_publish_handoff(self, item):
        if self.fail_publish:
            raise RuntimeError("queue full")
        self.calls.append(("publish", item.run_id))

    def admission_recover_handoff(self, run_id, *, settle):
        self.calls.append(("recover_handoff", run_id))
        settle(False)

    def admission_complete_handoff(self, run_id):
        self.calls.append(("complete_handoff", run_id))

    def admission_retain_cleanup(self, run_id, owner):
        return self.cleanups.setdefault(run_id, owner)

    def admission_retire_cleanup(self, run_id, owner):
        if self.cleanups.get(run_id) is owner:
            self.cleanups.pop(run_id)

    def publish_run_result(self, run_id, result):
        self.calls.append(("result", run_id, result.outcome))

    def notify_run_done(self, run_id):
        event = self.done.get(run_id)
        if event is not None:
            event.set()
        self.calls.append(("notify", run_id))


class _BoomFactory:
    def create(self, snapshot):
        del snapshot
        raise RuntimeError("no client")


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
@pytest.mark.parametrize("failure_stage", ("factory", "accepted_db", "publish"))
def test_reserved_admission_releases_every_owner_before_control_escapes(
    control_type, failure_stage: str
) -> None:
    control = control_type("stop")
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)
    coordinator = _FakeAdmissionCoordinator()

    class ControlFactory:
        def create(self, snapshot):
            del snapshot
            raise control

    class ControlConnection:
        def execute(self, sql, parameters=()):
            if failure_stage == "accepted_db" and "INSERT INTO runs" in sql:
                raise control
            return raw.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(raw, name)

    factory = ControlFactory() if failure_stage == "factory" else None
    database = ControlConnection() if failure_stage == "accepted_db" else raw
    if failure_stage == "publish":
        coordinator.admission_publish_handoff = lambda _item: (
            _ for _ in ()
        ).throw(control)
    admission = _fake_admission(database, factory=factory, coordinator=coordinator)

    with pytest.raises(control_type) as caught:
        admission.submit(SubmitRequest(message="hello"))

    assert caught.value is control
    assert coordinator.state == "idle"
    assert coordinator.done == {}
    assert [call[0] for call in coordinator.calls][-1] in {
        "release",
        "complete_handoff",
    }


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
def test_interrupted_finalize_control_exhausts_terminal_cleanup_then_escapes(
    control_type,
) -> None:
    control = control_type("stop")
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)

    class ControlFinalize:
        def execute(self, sql, parameters=()):
            if (
                sql.lstrip().upper().startswith("UPDATE")
                and "FINISHED_AT" in sql.upper()
            ):
                raise control
            return raw.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(raw, name)

    coordinator = _FakeAdmissionCoordinator()
    coordinator.fail_publish = True
    admission = _fake_admission(ControlFinalize(), coordinator=coordinator)

    with pytest.raises(control_type) as caught:
        admission.submit(SubmitRequest(message="hello"))

    assert caught.value is control
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "recover_handoff",
        "fail_recording",
        "result",
        "notify",
        "discard_result_slot",
        "complete_handoff",
    ]


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
@pytest.mark.parametrize(
    "failure_stage", ("factory", "accepted_db", "publish", "interrupted_db")
)
def test_public_host_unwinds_reserved_control_failures_before_close(
    control_type, failure_stage: str
) -> None:
    control = control_type("stop")
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)

    class ControlFactory:
        def create(self, snapshot):
            del snapshot
            raise control

    class ControlConnection:
        def execute(self, sql, parameters=()):
            upper = sql.lstrip().upper()
            if failure_stage == "accepted_db" and "INSERT INTO RUNS" in upper:
                raise control
            if failure_stage == "interrupted_db" and (
                upper.startswith("UPDATE RUNS") and "FINISHED_AT" in upper
            ):
                raise control
            return raw.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(raw, name)

    def publish(_item):
        if failure_stage == "publish":
            raise control
        if failure_stage == "interrupted_db":
            raise RuntimeError("handoff rejected")

    host, _conn, _capture = _host(
        ["unused"],
        conn=ControlConnection(),
        publish_work=publish,
        factory=ControlFactory() if failure_stage == "factory" else None,
    )
    host.start()
    try:
        with pytest.raises(control_type) as caught:
            host.submit(SubmitRequest(message="hello"))
        assert caught.value is control
        expected_state = (
            "recording_failed" if failure_stage == "interrupted_db" else "idle"
        )
        assert host.snapshot().coordinator_state == expected_state
        assert host.close(timeout=0.1) is True
    finally:
        host.close(timeout=0.1)


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
def test_unstarted_finalize_resumes_before_control_escapes(control_type) -> None:
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)
    control = control_type("finalize interrupted")

    class InterruptFinalizeOnce:
        def __init__(self):
            self.armed = True

        def execute(self, sql, parameters=()):
            upper = sql.lstrip().upper()
            if (
                self.armed
                and upper.startswith("UPDATE RUNS")
                and "FINISHED_AT" in upper
            ):
                self.armed = False
                raise control
            return raw.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(raw, name)

    host, conn, _ = _host(
        ["must not execute"],
        conn=InterruptFinalizeOnce(),
        publish_work=_reject_handoff,
    )
    host.start()
    try:
        with pytest.raises(control_type) as caught:
            host.submit(SubmitRequest(message="ping"))

        assert caught.value is control
        assert conn.execute(
            "SELECT phase, outcome, started_at FROM runs"
        ).fetchone() == ("finished", "interrupted", None)
        assert host.snapshot().coordinator_state == "idle"
        assert host._pending_handoff == set()
        assert host._done == {}
        assert host._results == {}
        assert host.close(timeout=0.1) is True
    finally:
        host.close(timeout=0.1)


def test_retried_close_idle_cannot_clear_a_successor_run() -> None:
    release_model = threading.Event()
    model = ScriptedModel([_GATE_DECISION, "pong"], gate=release_model)
    host, _conn, _ = _host(factory=ScriptedModelFactory(model))
    publish = host.admission_publish_handoff
    close_idle = host.admission_close_idle
    first_idle = threading.Event()
    resume_first = threading.Event()
    first_done = threading.Event()
    first_failure = KeyboardInterrupt("after opening admission")
    publish_calls = 0
    close_calls = 0
    failures: list[BaseException] = []

    def reject_first_handoff(item):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("first handoff rejected")
        publish(item)

    def interrupt_first_close(*args):
        nonlocal close_calls
        close_calls += 1
        close_idle(*args)
        if close_calls == 1:
            first_idle.set()
            assert resume_first.wait(2), "test did not resume first admission"
            raise first_failure

    host.admission_publish_handoff = reject_first_handoff
    host.admission_close_idle = interrupt_first_close
    host.start()

    def submit_first() -> None:
        try:
            host.submit(SubmitRequest(message="first"))
        except BaseException as exc:
            failures.append(exc)
        finally:
            first_done.set()

    thread = threading.Thread(target=submit_first)
    thread.start()
    try:
        assert first_idle.wait(2), "first run did not reopen admission"
        successor = host.submit(SubmitRequest(message="successor"))
        assert successor.kind == "accepted"
        assert model.entered.wait(2), "successor did not begin execution"
        running = host.snapshot()
        assert running.coordinator_state == "running"
        assert running.active_run is not None
        assert running.active_run.run_id == successor.run_id

        resume_first.set()
        assert first_done.wait(2), "first admission did not finish recovery"
        thread.join(2)

        after_retry = host.snapshot()
        assert failures == [first_failure]
        assert after_retry.coordinator_state == "running"
        assert after_retry.active_run is not None
        assert after_retry.active_run.run_id == successor.run_id
        assert host.submit(SubmitRequest(message="third")).kind == "run_in_progress"
    finally:
        resume_first.set()
        release_model.set()
        thread.join(2)
        host.close(timeout=0.2)


def _fake_admission(
    conn, *, factory=None, coordinator=None, snapshot_provider=None
):
    if factory is None:
        factory = ScriptedModelFactory(ScriptedModel([_GATE_DECISION, "pong"]))
    if coordinator is None:
        coordinator = _FakeAdmissionCoordinator()
    return RunAdmission(
        clock=FakeClock(),
        settings=Settings(),
        redactor=Redactor(()),
        factory=factory,
        snapshot_provider=snapshot_provider
        or SettingsBackedSnapshotProvider(Settings()),
        database=RecordingStore(conn, threading.Lock()),
        coordinator=coordinator,
    )


class _ControlledSnapshotProvider:
    def __init__(self, *, fail: bool = False, block: bool = False):
        self._delegate = SettingsBackedSnapshotProvider(Settings())
        self._fail = fail
        self._block = block
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def capture(self, *, stream: bool = False):
        self.calls += 1
        self.entered.set()
        if self._block:
            self.release.wait()
        if self._fail:
            raise RuntimeError("injected capture failure")
        return self._delegate.capture(stream=stream)


def test_known_busy_admission_returns_without_waiting_for_capture() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    coordinator.state = "accepted"
    provider = _ControlledSnapshotProvider(block=True)
    admission = _fake_admission(
        conn, coordinator=coordinator, snapshot_provider=provider
    )
    done = threading.Event()
    outcomes: list = []

    def submit() -> None:
        outcomes.append(admission.submit(SubmitRequest(message="second")))
        done.set()

    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert done.wait(0.5), "known-busy admission waited for configuration"
        assert outcomes[0].kind == "run_in_progress"
        assert provider.calls == 0
    finally:
        provider.release.set()
        worker.join(2.0)


def test_known_busy_admission_outranks_capture_failure() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    coordinator.state = "running"
    provider = _ControlledSnapshotProvider(fail=True)
    admission = _fake_admission(
        conn, coordinator=coordinator, snapshot_provider=provider
    )

    result = admission.submit(SubmitRequest(message="second"))

    assert result.kind == "run_in_progress"
    assert provider.calls == 0


def test_recording_failed_admission_outranks_capture_failure() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    coordinator.state = "recording_failed"
    provider = _ControlledSnapshotProvider(fail=True)
    admission = _fake_admission(
        conn, coordinator=coordinator, snapshot_provider=provider
    )

    result = admission.submit(SubmitRequest(message="second"))

    assert result.kind == "recording_unavailable"
    assert provider.calls == 0


def test_admission_reserves_again_after_capture_closes_the_idle_race() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()

    class _RacingProvider(_ControlledSnapshotProvider):
        def capture(self, *, stream: bool = False):
            captured = super().capture(stream=stream)
            coordinator.state = "running"
            return captured

    provider = _RacingProvider()
    admission = _fake_admission(
        conn, coordinator=coordinator, snapshot_provider=provider
    )

    result = admission.submit(SubmitRequest(message="loser"))

    assert result.kind == "run_in_progress"
    assert provider.calls == 1
    assert coordinator.observe_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


def test_run_winning_during_another_capture_is_not_queued() -> None:
    class _FirstCaptureBlocks:
        def __init__(self):
            self._delegate = SettingsBackedSnapshotProvider(Settings())
            self._lock = threading.Lock()
            self.calls = 0
            self.first_entered = threading.Event()
            self.release_first = threading.Event()

        def capture(self, *, stream: bool = False):
            with self._lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                self.first_entered.set()
                self.release_first.wait()
            return self._delegate.capture(stream=stream)

    provider = _FirstCaptureBlocks()
    published: list[WorkItem] = []
    host, conn, _capture = _host(
        snapshot_provider=provider,
        publish_work=published.append,
    )
    outcomes: list = []
    host.start()

    def submit_loser() -> None:
        outcomes.append(host.submit(SubmitRequest(message="loser")))

    loser = threading.Thread(target=submit_loser)
    loser.start()
    try:
        assert provider.first_entered.wait(2.0)
        winner = host.submit(SubmitRequest(message="winner"))
        assert winner.kind == "accepted"
        provider.release_first.set()
        loser.join(2.0)

        assert not loser.is_alive()
        assert len(outcomes) == 1
        assert outcomes[0].kind == "run_in_progress"
        assert outcomes[0].snapshot is not None
        assert outcomes[0].snapshot.active_run is not None
        assert outcomes[0].snapshot.active_run.run_id == winner.run_id
        assert provider.calls == 2
        assert len(published) == 1
        assert published[0].run_id == winner.run_id
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    finally:
        provider.release_first.set()
        loser.join(2.0)
        host.close()


def test_admission_submits_through_the_narrow_seams_without_a_host() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    admission = _fake_admission(conn, coordinator=coordinator)

    result = admission.submit(SubmitRequest(message="hello"))

    assert result.kind == "accepted"
    assert result.run_id is not None
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "publish",
    ]
    # The summary reached the coordinator with the reserve, fully formed:
    # the lease and its busy card are one publication.
    summary = coordinator.reserved_summaries[result.run_id]
    assert summary.phase == "accepted"
    assert summary.prompt_preview == "hello"
    assert summary.session_id == result.session_id
    assert result.session_id is not None
    row = conn.execute(
        "SELECT purpose, phase, prompt_preview FROM runs"
    ).fetchone()
    assert row == ("chat", "accepted", "hello")
    assert result.session_id is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone() == (1,), "a chat run without a session creates one"

    # A busy coordinator answers 409 without admitting anything else.
    calls_after_first = list(coordinator.calls)
    busy = admission.submit(SubmitRequest(message="again"))
    assert busy.kind == "run_in_progress"
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
    assert coordinator.calls == calls_after_first, (
        "the 409 answer drives no further transitions"
    )


def test_admission_releases_the_lease_when_client_capture_fails() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    admission = _fake_admission(conn, factory=_BoomFactory(), coordinator=coordinator)

    result = admission.submit(SubmitRequest(message="hello"))

    assert result.kind == "admission_failed"
    assert [call[0] for call in coordinator.calls] == ["reserve", "release"]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


def test_admission_releases_the_lease_when_the_accepted_write_fails() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(conn, when=lambda sql: "INSERT INTO runs" in sql)
    coordinator = _FakeAdmissionCoordinator()
    admission = _fake_admission(wrapped, coordinator=coordinator)

    result = admission.submit(SubmitRequest(message="hello"))

    assert result.kind == "admission_failed"
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "recover_handoff",
        "release",
        "discard_result_slot",
        "complete_handoff",
    ]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


def test_unstarted_handoff_failure_finalizes_interrupted_through_seams() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = _FakeAdmissionCoordinator()
    coordinator.fail_publish = True
    admission = _fake_admission(conn, coordinator=coordinator)

    result = admission.submit(SubmitRequest(message="hello"))

    assert result.kind == "handoff_failed"
    assert result.run_id is not None
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "recover_handoff",
        "close_idle",
        "result",
        "notify",
        "discard_result_slot",
        "complete_handoff",
    ]
    row = conn.execute(
        "SELECT phase, outcome, started_at FROM runs"
    ).fetchone()
    assert row == ("finished", "interrupted", None)
    assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)


def test_unstarted_db_failure_fails_closed_through_seams() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(
        conn,
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE RUNS")
        and "finished_at" in sql,
    )
    coordinator = _FakeAdmissionCoordinator()
    coordinator.fail_publish = True
    admission = _fake_admission(wrapped, coordinator=coordinator)

    result = admission.submit(SubmitRequest(message="hello"))

    assert result.kind == "handoff_failed"
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "recover_handoff",
        "fail_recording",
        "result",
        "notify",
        "discard_result_slot",
        "complete_handoff",
    ]
    assert coordinator.state == "recording_failed"
    fail_call = next(
        call for call in coordinator.calls if call[0] == "fail_recording"
    )
    assert fail_call[2] == "failed", "the projection carries recording_state=failed"


@pytest.mark.parametrize(
    "control_type", (KeyboardInterrupt, SystemExit, GeneratorExit)
)
def test_late_handoff_recovery_control_dominates_earlier_ordinary_failures(
    control_type, monkeypatch
) -> None:
    """Cleanup finishes, but a later process-control keeps its identity."""
    control = control_type("stop recovery")

    class FailingRecovery(_FakeAdmissionCoordinator):
        def admission_recover_handoff(self, run_id, *, settle):
            super().admission_recover_handoff(run_id, settle=settle)
            raise RuntimeError("ordinary recovery failure")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = FailingRecovery()
    coordinator.fail_publish = True
    admission = _fake_admission(conn, coordinator=coordinator)

    def interrupt_unstarted(item, *, acceptance_uncertain=False):
        del acceptance_uncertain
        coordinator.calls.append(("interrupt_unstarted", item.run_id))
        raise control

    monkeypatch.setattr(admission, "interrupt_unstarted", interrupt_unstarted)

    with pytest.raises(control_type) as caught:
        admission.submit(SubmitRequest(message="hello"))

    assert caught.value is control
    assert [call[0] for call in coordinator.calls] == [
        "reserve",
        "recover_handoff",
        "interrupt_unstarted",
        "discard_result_slot",
        "complete_handoff",
    ]


def test_handoff_recovery_attempts_every_cleanup_and_preserves_first_error() -> None:
    class FailingRecovery(_FakeAdmissionCoordinator):
        def publish_run_result(self, run_id, result):
            super().publish_run_result(run_id, result)
            raise RuntimeError("first result failure")

        def notify_run_done(self, run_id):
            super().notify_run_done(run_id)
            raise RuntimeError("later notification failure")

        def admission_discard_result_slot(self, run_id):
            super().admission_discard_result_slot(run_id)
            raise RuntimeError("later discard failure")

        def admission_complete_handoff(self, run_id):
            super().admission_complete_handoff(run_id)
            raise RuntimeError("later fence failure")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = FailingRecovery()
    coordinator.fail_publish = True
    admission = _fake_admission(conn, coordinator=coordinator)

    with pytest.raises(RuntimeError, match="first result failure"):
        admission.submit(SubmitRequest(message="hello"))

    names = [call[0] for call in coordinator.calls]
    assert names == [
        "reserve",
        "recover_handoff",
        "close_idle",
        "result",
        "result",
        "notify",
        "notify",
        "discard_result_slot",
        "discard_result_slot",
        "complete_handoff",
        "complete_handoff",
    ]


@pytest.mark.parametrize("cleanup_type", (RuntimeError, KeyboardInterrupt))
def test_ordinary_handoff_failure_exhausts_cleanup_before_cleanup_error_escapes(
    cleanup_type,
) -> None:
    cleanup_error = cleanup_type("cleanup failed")

    class FailingCleanup(_FakeAdmissionCoordinator):
        def admission_discard_result_slot(self, run_id):
            super().admission_discard_result_slot(run_id)
            raise cleanup_error

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    coordinator = FailingCleanup()
    coordinator.fail_publish = True
    admission = _fake_admission(conn, coordinator=coordinator)

    with pytest.raises(cleanup_type) as caught:
        admission.submit(SubmitRequest(message="hello"))

    assert caught.value is cleanup_error
    assert [call[0] for call in coordinator.calls][-2:] == [
        "discard_result_slot",
        "complete_handoff",
    ]


@pytest.mark.parametrize("state_failure", ("close_idle", "fail_recording"))
def test_handoff_recovery_publishes_terminal_notice_after_state_failure(
    state_failure: str,
) -> None:
    class FailingState(_FakeAdmissionCoordinator):
        def admission_close_idle(self, run_id):
            super().admission_close_idle(run_id)
            if state_failure == "close_idle":
                raise RuntimeError("close idle failed first")

        def admission_fail_recording(self, fallback, projection):
            super().admission_fail_recording(fallback, projection)
            if state_failure == "fail_recording":
                raise RuntimeError("fail recording failed first")

    raw = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(raw)
    database = raw
    if state_failure == "fail_recording":
        database = _FailOn(
            raw,
            when=lambda sql: sql.lstrip().upper().startswith("UPDATE")
            and "FINISHED_AT" in sql.upper(),
        )
    coordinator = FailingState()
    coordinator.fail_publish = True
    admission = _fake_admission(database, coordinator=coordinator)

    with pytest.raises(RuntimeError, match="failed first"):
        admission.submit(SubmitRequest(message="hello"))

    names = [call[0] for call in coordinator.calls]
    state_call = "close_idle" if state_failure == "close_idle" else "fail_recording"
    assert names == [
        "reserve",
        "recover_handoff",
        state_call,
        state_call,
        "result",
        "notify",
        "discard_result_slot",
        "complete_handoff",
    ]


class _FakeExecutionCoordinator:
    def __init__(self):
        self.marked: list[str] = []
        self.stopping = False

    def execution_mark_running(self, started_at: str):
        self.marked.append(started_at)

    def execution_mark_stopping(self) -> None:
        self.stopping = True


class _FakeAssistant:
    def __init__(self, result: LoopResult):
        self._result = result
        self.calls: list = []

    def respond(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return self._result


class _FakeRecorder:
    def __init__(self):
        self.settled: list[dict] = []

    def settle(self, item, **kwargs):
        self.settled.append({"item": item, **kwargs})


def _client_snapshot() -> ClientSnapshot:
    return ClientSnapshot(
        config_version="1",
        primary=ModelAssignment(
            endpoint_id="ep", model_id="m", wire_style="chat_completions"
        ),
        retrieval_gate=None,
        api_key=None,
        stream=False,
        stream_fallback=True,
        overall_deadline_s=None,
        per_attempt_timeout_s=30.0,
    )


def test_execution_runs_one_item_through_the_narrow_seams() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    schema.insert_accepted_run(
        conn,
        run_id="exec-1",
        purpose="chat",
        session_id=None,
        gateway="cli",
        accepted_at="2026-08-28T00:00:00Z",
    )
    conn.commit()
    capture = CapturingSink(name="capture", flush_at_run_end=False)
    fanout = FanOutSink([capture], process_instance_id="proc-exec")
    coordinator = _FakeExecutionCoordinator()
    recorder = _FakeRecorder()
    work: queue.Queue = queue.Queue()
    work.put(
        WorkItem(
            run_id="exec-1",
            request=SubmitRequest(message="hi"),
            snapshot=_client_snapshot(),
            client=ScriptedModel(["pong"]),
            session_id=None,
            prompt_preview="hi",
            accepted_at="2026-08-28T00:00:00Z",
        )
    )
    work.put(None)
    executor = RunExecutor(
        clock=FakeClock(),
        settings=Settings(),
        redactor=Redactor(()),
        assistant=_FakeAssistant(
            LoopResult(
                outcome="completed",
                reply=text_message("assistant", "pong"),
                error=None,
                step_count=1,
                duration_ms=1,
                model_results=(),
            )
        ),
        fanout=fanout,
        store=RecordingStore(conn, threading.Lock()),
        recorder=recorder,
        coordinator=coordinator,
        work_queue=work,
    )

    executor.run_loop()

    assert conn.execute(
        "SELECT phase FROM runs WHERE run_id = 'exec-1'"
    ).fetchone() == ("running",)
    assert len(coordinator.marked) == 1
    assert [event.payload.name for event in capture.events] == [
        "run.started", "path.stage"
    ]
    assert capture.events[-1].payload.reason == "disabled_by_config"
    assert len(recorder.settled) == 1
    settled = recorder.settled[0]
    assert settled["item"].run_id == "exec-1"
    assert settled["outcome"] == "completed"
    assert settled["step_count"] == 1


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
    hold = EnteredEvent()
    host, _conn, _capture = _host(["pong"], before_recording_commit=hold)
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert hold.entered.wait(2.0), "recording gate was not reached"
        assert host.snapshot().coordinator_state == "recording_pending"
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


class _FailFinalizeAndRollback(_FailOn):
    """Poison the Store and publish exactly when rollback was attempted."""

    def __init__(self, inner: sqlite3.Connection, rollback_attempted: threading.Event):
        super().__init__(
            inner,
            when=lambda sql: sql.lstrip().upper().startswith("UPDATE RUNS")
            and "finished_at" in sql,
        )
        self._rollback_attempted = rollback_attempted

    def rollback(self):
        self._rollback_attempted.set()
        raise sqlite3.OperationalError("injected rollback failure")


def test_recording_failed_snapshot_is_authoritative_before_503() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    wrapped = _FailOn(
        conn,
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE RUNS")
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
    after_recorded = EnteredEvent()
    host, _conn, _ = _host(["pong"], after_recorded_snapshot=after_recorded)
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert after_recorded.entered.wait(2.0), (
            "recorded snapshot gate was not reached"
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
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE RUNS")
        and "finished_at" in sql,
    )
    before_failed = EnteredEvent()
    host, _conn, _ = _host(
        ["pong"], conn=wrapped, before_recording_failed=before_failed
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert before_failed.entered.wait(2.0), (
            "recording-failed publication gate was not reached"
        )
        pending = host.submit(SubmitRequest(message="while-pending"))
        assert pending.kind == "run_in_progress"
        assert pending.snapshot is not None
        assert pending.snapshot.coordinator_state == "recording_pending"
        before_failed.set()
        host.wait(first.run_id)
        failed = host.submit(SubmitRequest(message="after-failed"))
        assert failed.kind == "recording_unavailable"
        assert failed.snapshot is not None
        assert failed.snapshot.coordinator_state == "recording_failed"
        assert failed.snapshot.active_run is not None
        assert failed.snapshot.active_run.recording_state == "failed"
        assert failed.snapshot.unrecorded_terminal_projection is not None
        assert (
            failed.snapshot.unrecorded_terminal_projection.recording_state
            == "failed"
        )
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.active_run is not None
        assert snap.active_run.recording_state == "failed"
    finally:
        before_failed.set()
        host.close()


def test_poisoned_store_remains_409_until_recording_failed_is_published() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    rollback_attempted = threading.Event()
    before_failed = threading.Event()
    wrapped = _FailFinalizeAndRollback(conn, rollback_attempted)
    host, _conn, _ = _host(
        ["pong"], conn=wrapped, before_recording_failed=before_failed
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert first.kind == "accepted"
        assert rollback_attempted.wait(timeout=2), "rollback was not attempted"

        pending = host.snapshot()
        assert pending.coordinator_state == "recording_pending"
        assert pending.active_run is not None
        assert pending.active_run.recording_state == "pending"
        assert host._store.available is False

        second = host.submit(SubmitRequest(message="two"))
        assert second.kind == "run_in_progress"
        assert second.snapshot is not None
        assert second.snapshot.coordinator_state == "recording_pending"

        created = DashboardApi(facade=host).create_session()
        assert created.status == 409
        assert created.code == "mutation_in_flight"
        assert created.session_id is None

        before_failed.set()
        host.wait(first.run_id)
        failed = host.submit(SubmitRequest(message="three"))
        assert failed.kind == "recording_unavailable"
        assert failed.snapshot is not None
        assert failed.snapshot.coordinator_state == "recording_failed"
    finally:
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
        assert len(attempts) == 3  # gate, aborted answer, committed answer
        assert attempts[0]["outcome"] == "committed"
        assert attempts[0]["usage"]["total_input_tokens"] is None
        first = attempts[1]
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
        assert attempts[2]["usage"]["total_input_tokens"] == 4
        log_tel = conn.execute(
            "SELECT telemetry FROM agent_log WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchall()
        assert log_tel == [(None,), (None,)]
    finally:
        host.close()


@pytest.mark.parametrize("interrupt", [False, True], ids=["control", "return-edge"])
def test_recorded_run_keeps_attempts_after_telemetry_return_interrupt(
    interrupt: bool,
) -> None:
    """A lost serializer return cannot turn a known Attempt into an empty ledger."""
    from contextlib import nullcontext

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.messages import TextBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )
    from agent_alfred.runtime.telemetry import serialize_run_telemetry

    model_result = ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id="att-recorded",
                streamed=False,
                outcome="committed",
                usage=Usage(total_input_tokens=11, output_tokens=7),
            ),
        ),
        response=ModelResponse(
            blocks=(TextBlock("pong"),),
            stop_reason="end_turn",
            model=ModelRef(endpoint_id="ep", model_id="m"),
        ),
        final_error=None,
    )
    host, conn, _ = _host([model_result])
    boundary = (
        interrupt_py_return_once(
            "telemetry-return-ledger-test",
            serialize_run_telemetry.__code__,
            KeyboardInterrupt("telemetry return interrupted"),
        )
        if interrupt else nullcontext([False])
    )
    host.start()
    try:
        with boundary as armed:
            submitted = host.submit(SubmitRequest(message="hello"))
            assert submitted.kind == "accepted"
            assert submitted.run_id is not None
            result = host.wait(submitted.run_id, timeout=2)
            assert not armed[0], "the serializer return injection was not reached"

        assert result.outcome == "completed"
        assert len(result.model_results) == 2
        assert result.model_results[0].response.blocks[0].text == _GATE_DECISION
        assert result.model_results[1] == replace(
            model_result,
            attempts=(replace(
                model_result.attempts[0],
                model=ModelRef(endpoint_id="opencode-go", model_id="deepseek-v4-flash"),
            ),),
        )
        row = conn.execute(
            "SELECT phase, telemetry FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "finished"
        attempts = json.loads(row[1])["attempts"]
        recorded_usage = [
            (
                attempt["attempt_id"],
                attempt["usage"]["total_input_tokens"],
                attempt["usage"]["output_tokens"],
            )
            for attempt in attempts
        ]
        gate_attempt_id = result.model_results[0].attempts[0].attempt_id
        assert recorded_usage == [
            (gate_attempt_id, None, None), ("att-recorded", 11, 7)
        ], "a recorded Run omitted its known Attempt usage"
    finally:
        host.close(timeout=2)
        conn.close()


@pytest.mark.parametrize(
    "failure_type", (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
)
def test_unserializable_telemetry_keeps_the_reply_unrecorded(failure_type) -> None:
    """Repeated serializer failures cannot commit a reply with missing accounts."""
    from agent_alfred.runtime.telemetry import serialize_run_telemetry

    code = serialize_run_telemetry.__code__
    injected = threading.Event()

    def interrupt(actual_code, offset, result):
        del offset, result
        if actual_code is code:
            injected.set()
            raise failure_type("telemetry return interrupted")

    host, conn, _ = _host(["pong"])
    host.start()
    try:
        with claimed_monitoring_tool(
            "telemetry-recording-failure-test", local_codes=(code,)
        ) as tool_id:
            sys.monitoring.register_callback(
                tool_id, sys.monitoring.events.PY_RETURN, interrupt
            )
            sys.monitoring.set_local_events(
                tool_id, code, sys.monitoring.events.PY_RETURN
            )
            submitted = host.submit(SubmitRequest(message="hello"))
            assert submitted.kind == "accepted"
            assert submitted.run_id is not None
            result = host.wait(submitted.run_id, timeout=2)
            assert injected.is_set(), "the serializer injection was not reached"

        assert result.outcome == "completed"
        assert len(result.model_results[0].attempts) == 1
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "recording_failed"
        projection = snapshot.unrecorded_terminal_projection
        assert projection is not None
        assert projection.run_id == submitted.run_id
        assert projection.recording_state == "failed"
        assert projection.reply_text == "pong"
        assert conn.execute(
            "SELECT phase, telemetry FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone() == ("running", None)
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_log WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone() == (0,)
        assert host.submit(SubmitRequest(message="next")).kind == (
            "recording_unavailable"
        )
    finally:
        host.close(timeout=2)
        conn.close()


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
    host, conn, _ = _host(
        [model_result], snapshot_provider=_probe_provider(), chat_script=False
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id="opencode-go",
                model_id="deepseek-v4-flash",
            )
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
    model = ScriptedModel([_GATE_DECISION, "second"])
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
        assert len(model.requests) == 2  # gate followed by answer
        roles = [message.role for message in model.requests[1].messages]
        assert roles == ["user", "assistant", "user"]
    finally:
        host2.close()
        conn2.close()
