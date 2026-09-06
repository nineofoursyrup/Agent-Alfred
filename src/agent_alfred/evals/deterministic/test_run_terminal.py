"""Issue #13 review: a Run's terminal state is reachable and then final.

Two contracts were not executable:

1. ``RunExecutor.execute`` settled the Run *after* its ``try/except``, not in
   a ``finally``. Any ``BaseException`` that is not ``KeyboardInterrupt`` --
   ``SystemExit`` first among them -- skipped ``settle()`` entirely: the Run
   stayed ``running`` with a NULL outcome, no ``run.finished`` was emitted,
   and the admission lease was never released, so every later submit answered
   ``run_in_progress`` forever.
2. ``schema.update_run_phase`` trusted the caller's ``from_phase`` as the
   only guard, so ``from_phase="finished", to_phase="finished"`` rewrote a
   terminal Run's outcome, ``finished_at``, ``activity_revision``, and its
   Session's ``activity_revision``. A terminal Run must have no outgoing
   edges at all.

The tests below fail against the pre-fix code for the reasons named in their
docstrings.
"""

from __future__ import annotations

import gc
import json
import queue
import sqlite3
import threading
import weakref
from collections.abc import Callable

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BarrierFlushResult,
    CapturingSink,
    FanOutSink,
    RunFinished,
)
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import message_plain_text, text_message
from agent_alfred.model import (
    AttemptRecord,
    ClientSnapshot,
    ModelAssignment,
    ModelRef,
    ModelRequest,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.redact import Redactor
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.recording import RecordingStore, RunRecorder
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT, Settings

TS = "2026-08-28T00:00:00Z"


# --- harness ---------------------------------------------------------------


class _SpyRecorder:
    """Counts settles and forwards to the real recorder when given one."""

    def __init__(self, inner=None):
        self.inner = inner
        self.settled: list[dict] = []

    def settle(self, item, **kwargs) -> None:
        self.settled.append({"item": item, **kwargs})
        if self.inner is not None:
            self.inner.settle(item, **kwargs)


class _FakeExecutionCoordinator:
    def __init__(self) -> None:
        self.marked: list[str] = []
        self.stopping = False

    def execution_mark_running(self, started_at: str):
        self.marked.append(started_at)
        return None

    def execution_mark_stopping(self) -> None:
        self.stopping = True


class _RaisingAssistant:
    """Runs one real Attempt through the client, then raises."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.calls = 0

    def respond(self, message, **kwargs):
        del message
        self.calls += 1
        client = kwargs["client"]
        client.respond(
            ModelRequest(
                model=ModelRef(endpoint_id="ep", model_id="m"),
                system=(),
                messages=(),
                max_tokens=16,
            ),
            events=kwargs.get("events"),
            deadline=None,
        )
        raise self._exc


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


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    schema.insert_session(conn, session_id="sess-1", created_at=TS)
    conn.commit()
    return conn


def _accepted_run(conn: sqlite3.Connection, run_id: str = "run-term") -> None:
    schema.insert_accepted_run(
        conn,
        run_id=run_id,
        purpose="chat",
        session_id="sess-1",
        gateway="cli",
        accepted_at=TS,
    )
    conn.commit()


def _work_item(run_id: str = "run-term") -> WorkItem:
    return WorkItem(
        run_id=run_id,
        request=SubmitRequest(message="hello", session_id="sess-1"),
        snapshot=_client_snapshot(),
        client=ScriptedModel([_ledger_result()]),
        session_id="sess-1",
        prompt_preview="hello",
        accepted_at=TS,
    )


def _ledger_result() -> ModelResult:
    """A ModelResult with a real, billable Attempt behind it."""
    return ModelResult(
        attempts=(
            AttemptRecord(
                attempt_id="attempt-1",
                streamed=False,
                outcome="committed",
                usage=Usage(output_tokens=7),
            ),
        ),
        response=ModelResponse(
            blocks=(), stop_reason="end_turn",
            model=ModelRef(endpoint_id="ep", model_id="m"),
        ),
        final_error=None,
    )


def _executor(
    conn: sqlite3.Connection,
    *,
    assistant,
    recorder,
    capture: CapturingSink | None = None,
) -> tuple[RunExecutor, CapturingSink]:
    sink = capture or CapturingSink(name="capture", flush_at_run_end=True)
    fanout = FanOutSink([sink], process_instance_id="proc-terminal")
    return (
        RunExecutor(
            clock=FakeClock(),
            settings=Settings(),
            redactor=Redactor(()),
            assistant=assistant,
            fanout=fanout,
            store=RecordingStore(conn, threading.Lock()),
            recorder=recorder,
            coordinator=_FakeExecutionCoordinator(),
            work_queue=queue.Queue(),
        ),
        sink,
    )


def _real_recorder(conn: sqlite3.Connection, coordinator) -> RunRecorder:
    return RunRecorder(
        clock=FakeClock(),
        fanout=FanOutSink([], process_instance_id="proc-terminal"),
        redactor=Redactor(()),
        store=RecordingStore(conn, threading.Lock()),
        coordinator=coordinator,
    )


class _RecordingCoordinator:
    """Minimal coordinator capturing what the recorder drives."""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.done: dict[str, threading.Event] = {}
        self.results: dict[str, LoopResult] = {}

    def recording_enter_pending(self, projection) -> None:
        self.states.append("recording_pending")

    def recording_enter_failed(self, projection) -> None:
        self.states.append("recording_failed")

    def recording_publish_recorded_then_release(self, run_id: str) -> None:
        del run_id
        self.states.append("recorded")

    def publish_run_result(self, run_id, result) -> None:
        self.results[run_id] = result

    def notify_run_done(self, run_id) -> None:
        self.done.setdefault(run_id, threading.Event()).set()


# --- problem 5: no BaseException may skip the Run's settle -----------------


@pytest.mark.parametrize(
    ("make_exc", "propagates"),
    [
        pytest.param(lambda: SystemExit("boom"), True, id="SystemExit"),
        pytest.param(lambda: KeyboardInterrupt(), False, id="KeyboardInterrupt"),
        pytest.param(lambda: _CustomBaseException("boom"), False, id="custom"),
    ],
)
def test_a_base_exception_still_settles_the_run_exactly_once(
    make_exc, propagates
) -> None:
    """``settle()`` sat after the ``try/except``: SystemExit and any other
    BaseException walked straight past it, leaving the Run ``running``.

    Only SystemExit keeps unwinding, and only after the settle; the others
    are recorded as the Run's terminal outcome instead of being allowed to
    take the execution thread with them.
    """
    conn = _database()
    _accepted_run(conn)
    recorder = _SpyRecorder()
    executor, _sink = _executor(
        conn, assistant=_RaisingAssistant(make_exc()), recorder=recorder
    )

    raised = False
    try:
        executor.execute(_work_item())
    except SystemExit:
        raised = True

    assert raised is propagates, (
        f"{type(make_exc()).__name__} propagates={raised}, expected {propagates}"
    )
    assert len(recorder.settled) == 1, (
        f"settle ran {len(recorder.settled)} times; it must run exactly once"
    )
    settled = recorder.settled[0]
    assert settled["item"].run_id == "run-term"
    assert settled["outcome"] == "interrupted"


def test_the_settled_outcome_matches_the_interruption_semantics() -> None:
    """KeyboardInterrupt and process-control exits are not business
    successes: the Run they interrupt has no provable terminal outcome."""
    conn = _database()
    _accepted_run(conn)
    recorder = _SpyRecorder()
    executor, _sink = _executor(
        conn, assistant=_RaisingAssistant(KeyboardInterrupt()), recorder=recorder
    )
    executor.execute(_work_item())  # must not propagate out of the Run
    assert recorder.settled[0]["outcome"] == "interrupted"
    assert recorder.settled[0]["reply"] is None, "no reply was produced"

    conn2 = _database()
    _accepted_run(conn2)
    recorder2 = _SpyRecorder()
    executor2, _sink2 = _executor(
        conn2, assistant=_RaisingAssistant(SystemExit("boom")), recorder=recorder2
    )
    with pytest.raises(SystemExit):
        executor2.execute(_work_item())
    assert recorder2.settled[0]["outcome"] == "interrupted", (
        "SystemExit is not a business failure the index can prove"
    )


def test_a_plain_exception_settles_failed_and_does_not_propagate() -> None:
    conn = _database()
    _accepted_run(conn)
    recorder = _SpyRecorder()
    executor, _sink = _executor(
        conn, assistant=_RaisingAssistant(RuntimeError("boom")), recorder=recorder
    )
    executor.execute(_work_item())  # must not raise
    assert len(recorder.settled) == 1
    assert recorder.settled[0]["outcome"] == "failed"
    assert recorder.settled[0]["error"] == "RuntimeError"
    reply = recorder.settled[0]["reply"]
    assert reply is not None
    assert message_plain_text(reply) == CONTROLLED_FAILURE_TEXT, (
        "an ordinary failure still answers the user with a controlled reply"
    )


def test_the_attempt_ledger_survives_a_base_exception() -> None:
    """The Attempts that really hit the network -- and were really billed --
    must still be recorded when the loop dies on a BaseException."""
    conn = _database()
    _accepted_run(conn)
    coordinator = _RecordingCoordinator()
    recorder = _real_recorder(conn, coordinator)
    executor, _sink = _executor(
        conn, assistant=_RaisingAssistant(SystemExit("boom")), recorder=recorder
    )

    with pytest.raises(SystemExit):
        executor.execute(_work_item())

    telemetry = conn.execute(
        "SELECT telemetry FROM runs WHERE run_id = 'run-term'"
    ).fetchone()[0]
    payload = json.loads(telemetry)
    assert [attempt["attempt_id"] for attempt in payload["attempts"]] == [
        "attempt-1"
    ], "the Attempt that happened is not lost with the loop's return value"
    assert payload["attempts"][0]["usage"]["output_tokens"] == 7


def test_a_base_exception_leaves_a_terminal_run_and_frees_the_lease() -> None:
    """Host level, the reported symptom: the Run must not stay ``running``
    and the next submit must not answer ``run_in_progress`` forever."""
    from agent_alfred.model import ScriptedModelFactory

    conn = _database()
    host = _build_host(
        conn, ScriptedModelFactory(ScriptedModel([_CustomBaseException("boom")]))
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello", session_id="sess-1"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id, timeout=10)
        assert result.outcome == "interrupted"
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "finished", "the Run never stays running"
        assert row[1] is not None
        again = host.submit(SubmitRequest(message="again", session_id="sess-1"))
        assert again.kind == "accepted", "the lease is not held forever"
        host.wait(again.run_id, timeout=10)
    finally:
        host.close()


def test_system_exit_settles_the_run_then_unwinds_the_worker() -> None:
    """SystemExit is process control: it is re-raised after the Run is
    settled, so the Run is decided and the lease released before the thread
    goes. The Host must then refuse Runs it can no longer execute rather
    than accept them into a dead queue."""
    from agent_alfred.model import ScriptedModelFactory

    conn = _database()
    host = _build_host(conn, ScriptedModelFactory(ScriptedModel([SystemExit("boom")])))
    captured: dict[str, object] = {}
    previous = threading.excepthook

    def hook(args) -> None:
        captured["exc_type"] = args.exc_type

    threading.excepthook = hook
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello", session_id="sess-1"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id, timeout=10)
        assert result.outcome == "interrupted"
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row == ("finished", "interrupted"), "the Run is decided before unwind"
        assert host.snapshot().coordinator_state == "idle", "the lease is released"
        assert captured.get("exc_type") is SystemExit, (
            f"SystemExit still unwinds after the settle: {captured}"
        )
        refused = host.submit(SubmitRequest(message="again", session_id="sess-1"))
        assert refused.kind == "admission_failed", (
            "a Host with no execution thread must not accept Runs"
        )
    finally:
        # close() joins the worker, and the hook is only invoked as the
        # thread finishes -- so the hook has to stay installed past it.
        host.close()
        threading.excepthook = previous


def test_system_exit_cannot_admit_work_between_release_and_worker_stop(
    monkeypatch,
) -> None:
    """A settled SystemExit must close admission before its lease releases.

    ``notify_run_done`` is the public coordinator call immediately after the
    recorded state releases the lease.  Holding it here exposes the former
    idle-before-``stopped_by`` window without relying on scheduler timing.
    """
    from agent_alfred.model import ScriptedModelFactory

    conn = _database()
    host = _build_host(conn, ScriptedModelFactory(ScriptedModel([SystemExit(70)])))
    released = threading.Event()
    resume_unwind = threading.Event()
    original_notify = host.notify_run_done

    def pause_after_release(run_id: str) -> None:
        released.set()
        assert resume_unwind.wait(5), "test did not release the worker unwind"
        original_notify(run_id)

    monkeypatch.setattr(host, "notify_run_done", pause_after_release)
    previous = threading.excepthook
    threading.excepthook = lambda _args: None
    host.start()
    try:
        first = host.submit(SubmitRequest(message="first", session_id="sess-1"))
        assert first.kind == "accepted"
        assert released.wait(2), "worker did not reach the post-release boundary"
        assert host.snapshot().coordinator_state == "idle"

        second = host.submit(SubmitRequest(message="second", session_id="sess-1"))
    finally:
        resume_unwind.set()
        host.close()
        threading.excepthook = previous

    stranded = conn.execute(
        """SELECT phase, outcome FROM runs
           WHERE run_id != ? AND phase = 'accepted' AND outcome IS NULL""",
        (first.run_id,),
    ).fetchall()
    assert second.kind == "admission_failed"
    assert second.run_id is None
    assert stranded == [], "dead worker retained an accepted Run with no outcome"


def test_exactly_one_run_finished_is_emitted_when_the_loop_dies() -> None:
    from agent_alfred.model import ScriptedModelFactory

    conn = _database()
    host = _build_host(
        conn, ScriptedModelFactory(ScriptedModel([_CustomBaseException("boom")]))
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello", session_id="sess-1"))
        host.wait(submitted.run_id, timeout=10)
        sink = next(
            item for item in host._fanout.sinks if isinstance(item, CapturingSink)
        )
        finished = [
            event
            for event in sink.events
            if isinstance(event.payload, RunFinished)
        ]
        assert len(finished) == 1, f"run.finished emitted {len(finished)} times"
        assert finished[0].payload.outcome == "interrupted"
        assert conn.execute(
            "SELECT role FROM agent_log WHERE run_id = ? ORDER BY id",
            (submitted.run_id,),
        ).fetchall() == [("user",)], "an interrupted Run writes no assistant reply"
    finally:
        host.close()


def test_a_failed_settle_enters_recording_failed_and_keeps_answering_503() -> None:
    """``settle()`` failing must land in ``recording_failed`` with its
    projection kept, never release the lease back to ``idle``."""

    class _FailOn:
        def __init__(self, inner, when):
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

        def __getattr__(self, name):
            return getattr(self._inner, name)

    from agent_alfred.model import ScriptedModelFactory

    inner = _database()
    conn = _FailOn(
        inner,
        when=lambda sql: sql.lstrip().upper().startswith("UPDATE")
        and "finished_at" in sql,
    )
    # A non-control BaseException: the worker survives it, so what governs
    # the next submit is the recording failure rather than a missing thread.
    host = _build_host(
        conn, ScriptedModelFactory(ScriptedModel([_CustomBaseException("boom")]))
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello", session_id="sess-1"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id, timeout=10)
        assert result.outcome == "interrupted"
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "recording_failed"
        projection = snapshot.unrecorded_terminal_projection
        assert projection is not None, "the projection is kept, not dropped"
        assert projection.run_id == submitted.run_id
        assert projection.recording_state == "failed"
        for _ in range(2):
            assert host.submit(SubmitRequest(message="again")).kind == (
                "recording_unavailable"
            )
    finally:
        host.close()


def _build_host(
    conn,
    factory,
    *,
    extra_sinks=(),
    after_recorded_snapshot=None,
    clock=None,
    snapshot_listener=None,
) -> RuntimeHost:
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    fanout = FanOutSink(
        [*extra_sinks, capture], process_instance_id="proc-terminal"
    )
    return RuntimeHost(
        conn=conn,
        factory=factory,
        settings=Settings(),
        clock=clock or FakeClock(),
        fanout=fanout,
        process_instance_id="proc-terminal",
        after_recorded_snapshot=after_recorded_snapshot,
        snapshot_listener=snapshot_listener,
    )


class _CustomBaseException(BaseException):
    """A BaseException that is neither SystemExit nor KeyboardInterrupt."""


_ExceptionFactory = Callable[[], BaseException]
_SETTLEMENT_EXCEPTIONS = (
    pytest.param(
        lambda: KeyboardInterrupt("settlement interrupted"),
        id="KeyboardInterrupt",
    ),
    pytest.param(
        lambda: SystemExit("settlement stopping"),
        id="SystemExit",
    ),
    pytest.param(
        lambda: _CustomBaseException("settlement fault"),
        id="custom",
    ),
)


class _InterruptRunFinishedPrepare:
    name = "interrupt-run-finished"
    flush_at_run_end = False

    def __init__(self, make_exception: _ExceptionFactory) -> None:
        self._make_exception = make_exception
        self._raised = False
        self.entered = threading.Event()
        self.calls = 0

    def prepare(self, event):
        if isinstance(event.payload, RunFinished) and not self._raised:
            self.calls += 1
            self._raised = True
            self.entered.set()
            raise self._make_exception()
        return None

    def commit(self, prepared, event) -> None:
        del prepared, event

    def flush(self, run_id):
        del run_id
        return None

    def close(self) -> None:
        return None


def _assert_completed_once(
    conn: sqlite3.Connection, run_id: str
) -> dict[str, object]:
    row = conn.execute(
        "SELECT phase, outcome, telemetry FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert row[:2] == ("finished", "completed")
    assert conn.execute(
        "SELECT role FROM agent_log WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall() == [("user",), ("assistant",)]
    return json.loads(row[2])


def _assert_worker_accepts_a_successor(host: RuntimeHost) -> None:
    successor = host.submit(
        SubmitRequest(message="successor", session_id="sess-1")
    )
    assert successor.kind == "accepted"
    assert successor.run_id is not None
    assert host.wait(successor.run_id, timeout=2).outcome == "completed"


class _InterruptWorkerStartClock(FakeClock):
    """Raise only at RunExecutor's pre-body wall-clock read."""

    def __init__(self, make_exception: _ExceptionFactory) -> None:
        super().__init__()
        self._make_exception = make_exception
        self._raised = False
        self.entered = threading.Event()

    def wall_utc(self):
        if threading.current_thread().name == "run-worker" and not self._raised:
            self._raised = True
            self.entered.set()
            raise self._make_exception()
        return super().wall_utc()


@pytest.mark.parametrize("fail_before_running", [False, True])
def test_closed_host_releases_client_after_execution_start_failure(
    fail_before_running,
) -> None:
    clients: list[weakref.ReferenceType[ScriptedModel]] = []

    class FreshClientFactory:
        def create(self, snapshot):
            del snapshot
            client = ScriptedModel(["pong"])
            clients.append(weakref.ref(client))
            return client

    clock = (
        _InterruptWorkerStartClock(lambda: RuntimeError("injected start failure"))
        if fail_before_running else FakeClock()
    )
    conn = _database()
    host = _build_host(conn, FreshClientFactory(), clock=clock)
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello", session_id="sess-1"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id, timeout=2)
        if fail_before_running:
            assert clock.entered.is_set(), "execution-start failure was not reached"
            assert result.error == "RuntimeError"
        assert result.outcome == ("failed" if fail_before_running else "completed")
        assert host.snapshot().coordinator_state == "idle"
        assert len(clients) == 1
        _assert_worker_accepts_a_successor(host)
        gc.collect()
        assert clients[0]() is None, "serving Host retained its previous Run client"
        assert host.close(timeout=2)
        assert len(clients) == 2
        gc.collect()
        assert all(client() is None for client in clients), (
            "closed Host retained a completed Run client"
        )
    finally:
        host.close(timeout=2)
        conn.close()


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_execution_start_clock_base_exception_still_settles_and_notifies(
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    """The pre-body started_at read is part of execution's ownership scope."""
    conn = _database()
    clock = _InterruptWorkerStartClock(make_exception)
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong"])),
        clock=clock,
    )
    worker_failures: list[BaseException] = []
    worker_stopped = threading.Event()

    def capture_worker_failure(args) -> None:
        worker_failures.append(args.exc_value)
        worker_stopped.set()

    monkeypatch.setattr(
        threading,
        "excepthook",
        capture_worker_failure,
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert clock.entered.wait(2), "worker did not read started_at"

        result = host.wait(submitted.run_id, timeout=2)
        assert result.outcome == "interrupted"
        assert conn.execute(
            "SELECT phase, outcome, started_at FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone() == ("finished", "interrupted", None)
        assert host.snapshot().coordinator_state == "idle"

        if isinstance(make_exception(), SystemExit):
            assert worker_stopped.wait(2), "SystemExit did not unwind the worker"
            assert worker_failures and isinstance(worker_failures[0], SystemExit)
            refused = host.submit(
                SubmitRequest(message="successor", session_id="sess-1")
            )
            assert refused.kind == "admission_failed"
        else:
            assert worker_failures == []
            _assert_worker_accepts_a_successor(host)
    finally:
        host.close(timeout=2)


def test_running_snapshot_listener_control_retires_handoff_cell() -> None:
    """An after-effect listener failure cannot retain the accepted WorkItem."""
    conn = _database()
    interrupted = threading.Event()
    raised = False

    def interrupt_running_snapshot(snapshot) -> None:
        nonlocal raised
        if snapshot.coordinator_state == "running" and not raised:
            raised = True
            interrupted.set()
            raise KeyboardInterrupt("running snapshot listener interrupted")

    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
        snapshot_listener=interrupt_running_snapshot,
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert interrupted.wait(2), "worker did not publish the running snapshot"

        host.wait(submitted.run_id, timeout=2)
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row is not None and row[0] == "finished" and row[1] is not None
        assert submitted.run_id not in host._queue._cells

        _assert_worker_accepts_a_successor(host)
        assert host._queue._cells == {}
    finally:
        host.close(timeout=2)


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_run_finished_prepare_control_cannot_skip_terminal_settlement(
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    """A terminal sink interruption cannot strand the Run or its waiter."""
    conn = _database()
    interrupting = _InterruptRunFinishedPrepare(make_exception)
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
        extra_sinks=(interrupting,),
    )
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert interrupting.entered.wait(2), "worker did not reach settlement"

        result = host.wait(submitted.run_id, timeout=2)
        assert result.outcome == "completed"
        telemetry = _assert_completed_once(conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is True
        assert host.snapshot().coordinator_state == "idle"
        assert interrupting.calls == 1, "an uncertain event publish is not retried"
        _assert_worker_accepts_a_successor(host)
        assert worker_failures == []
    finally:
        host.close(timeout=2)


class _InterruptRunFinishedAfterPublish:
    """Lose FanOut's return edge only after the event was committed."""

    def __init__(
        self,
        inner: Callable[..., object],
        make_exception: _ExceptionFactory,
    ) -> None:
        self._inner = inner
        self._make_exception = make_exception
        self._raised = False
        self.entered = threading.Event()
        self.run_finished_calls = 0

    def __call__(self, payload, envelope=None):
        result = self._inner(payload, envelope)
        if isinstance(payload, RunFinished):
            self.run_finished_calls += 1
            if not self._raised:
                self._raised = True
                self.entered.set()
                raise self._make_exception()
        return result


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_run_finished_after_publish_control_is_not_retried(
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    """An uncertain committed terminal event is degraded, never duplicated."""
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
    )
    interrupted = _InterruptRunFinishedAfterPublish(
        host._fanout.emit,
        make_exception,
    )
    monkeypatch.setattr(host._fanout, "emit", interrupted)
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert interrupted.entered.wait(2), "run.finished was not published"

        assert host.wait(submitted.run_id, timeout=2).outcome == "completed"
        telemetry = _assert_completed_once(conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is True
        assert interrupted.run_finished_calls == 1
        capture = next(
            sink for sink in host._fanout.sinks if isinstance(sink, CapturingSink)
        )
        committed = [
            event
            for event in capture.events
            if isinstance(event.payload, RunFinished)
            and event.envelope.run_id == submitted.run_id
        ]
        assert len(committed) == 1, "uncertain run.finished was emitted again"
        _assert_worker_accepts_a_successor(host)
        assert worker_failures == []
    finally:
        host.close(timeout=2)


class _InterruptOnceAround:
    """Raise once before or after one settlement-owned public operation."""

    def __init__(
        self,
        inner: Callable[..., object],
        make_exception: _ExceptionFactory,
        *,
        position: str,
    ) -> None:
        self._inner = inner
        self._make_exception = make_exception
        self._position = position
        self._raised = False
        self.entered = threading.Event()
        self.resumed = threading.Event()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._raised:
            self.resumed.set()
            return self._inner(*args, **kwargs)
        self._raised = True
        self.entered.set()
        if self._position == "before":
            raise self._make_exception()
        self._inner(*args, **kwargs)
        raise self._make_exception()


_SETTLEMENT_BOUNDARIES = (
    pytest.param("pending", "recording_enter_pending", "after", id="pending-after"),
    pytest.param("result", "publish_run_result", "after", id="result-after"),
    pytest.param("flush", "flush_barrier", "after", id="flush-after"),
    pytest.param(
        "release",
        "recording_publish_recorded_then_release",
        "after",
        id="release-after-idle",
    ),
    pytest.param("notify", "notify_run_done", "before", id="notify-before-set"),
)


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
@pytest.mark.parametrize(
    ("stage", "method_name", "position"), _SETTLEMENT_BOUNDARIES
)
def test_settlement_boundary_base_exception_is_recovered(
    stage: str,
    method_name: str,
    position: str,
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    """Uncertain coordinator and flush calls cannot escape Run settlement."""
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
    )
    owner = host._fanout if stage == "flush" else host
    boundary = _InterruptOnceAround(
        getattr(owner, method_name),
        make_exception,
        position=position,
    )
    monkeypatch.setattr(owner, method_name, boundary)
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert boundary.entered.wait(2), f"worker did not reach {stage}"
        if stage != "flush":
            assert boundary.resumed.wait(2), f"worker did not recover {stage}"

        result = host.wait(submitted.run_id, timeout=2)
        assert result.outcome == "completed"
        telemetry = _assert_completed_once(conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is (stage == "flush")
        assert host.snapshot().coordinator_state == "idle"
        expected_calls = 1 if stage == "flush" else 2
        assert boundary.calls == expected_calls
        _assert_worker_accepts_a_successor(host)
        assert worker_failures == []
    finally:
        host.close(timeout=2)


@pytest.mark.parametrize("interrupt", [False, True], ids=["control", "return-edge"])
def test_recorded_snapshot_precedes_idle_after_summary_return_interrupt(
    interrupt: bool,
) -> None:
    """A constructed summary is not yet the recorded authority (ADR-0026)."""
    import dis
    from contextlib import nullcontext

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.runtime.snapshot import RuntimeSnapshot

    code = RuntimeHost.recording_publish_recorded_then_release.__code__
    offsets = [
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_FAST" and instruction.argval == "recorded"
    ]
    assert len(offsets) == 1, "the recorded-summary return boundary changed"
    observed: list[RuntimeSnapshot] = []
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
        snapshot_listener=observed.append,
    )
    boundary = (
        interrupt_instruction_once(
            code, offsets[0], KeyboardInterrupt("recorded summary return interrupted")
        )
        if interrupt else nullcontext([False])
    )
    host.start()
    try:
        with boundary as armed:
            first = host.submit(SubmitRequest(message="first", session_id="sess-1"))
            assert first.kind == "accepted"
            assert first.run_id is not None
            assert host.wait(first.run_id, timeout=2).outcome == "completed"
            assert not armed[0], "the recorded-summary injection was not reached"

        assert host.snapshot().coordinator_state == "idle"
        _assert_completed_once(conn, first.run_id)
        _assert_worker_accepts_a_successor(host)
        pending_index = next(
            index
            for index, snapshot in enumerate(observed)
            if snapshot.active_run is not None
            and snapshot.active_run.run_id == first.run_id
            and snapshot.active_run.recording_state == "pending"
        )
        settlement = observed[pending_index:]
        idle_index = next(
            index
            for index, snapshot in enumerate(settlement)
            if snapshot.coordinator_state == "idle"
        )
        release_sequence = [
            (
                snapshot.coordinator_state,
                snapshot.active_run.run_id if snapshot.active_run else None,
                snapshot.active_run.recording_state if snapshot.active_run else None,
            )
            for snapshot in settlement[:idle_index + 1]
        ]
        expected_recorded = ("recording_pending", first.run_id, "recorded")
        assert expected_recorded in release_sequence[:-1], (
            f"admission reached idle without recorded authority: {release_sequence}"
        )
    finally:
        host.close(timeout=2)
        conn.close()


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_consecutive_terminal_listener_failures_cannot_retain_admission(
    make_exception: _ExceptionFactory,
) -> None:
    """Recorded authority must release its lease after retry budget is spent."""
    conn = _database()
    armed = False
    failures: list[BaseException] = []

    def interrupt_terminal_delivery(snapshot) -> None:
        nonlocal armed
        summary = snapshot.active_run
        if summary is not None and summary.recording_state == "recorded":
            armed = True
        if armed and len(failures) < 2:
            failure = make_exception()
            failures.append(failure)
            raise failure

    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
        snapshot_listener=interrupt_terminal_delivery,
    )
    host.start()
    try:
        first = host.submit(
            SubmitRequest(message="first", session_id="sess-1")
        )
        assert first.kind == "accepted"
        assert first.run_id is not None
        assert host.wait(first.run_id, timeout=2).outcome == "completed"

        assert len(failures) == 2, "the listener did not exhaust both retries"
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "idle"
        assert snapshot.active_run is None
        assert snapshot.unrecorded_terminal_projection is None

        successor = host.submit(
            SubmitRequest(message="second", session_id="sess-1")
        )
        assert successor.kind == "accepted"
        assert successor.run_id is not None
        assert host.wait(successor.run_id, timeout=2).outcome == "completed"
    finally:
        host.close(timeout=2)


class _InterruptFlushOnce:
    name = "interrupt-flush"
    flush_at_run_end = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._raised = False

    def prepare(self, event):
        del event
        return None

    def commit(self, prepared, event) -> None:
        del prepared, event

    def flush(self, run_id: str):
        self.calls.append(run_id)
        if not self._raised:
            self._raised = True
            raise SystemExit("critical flush interrupted")
        return BarrierFlushResult(outcome="flushed")

    def close(self) -> None:
        return None


class _CountingFlush:
    name = "counting-flush"
    flush_at_run_end = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def prepare(self, event):
        del event
        return None

    def commit(self, prepared, event) -> None:
        del prepared, event

    def flush(self, run_id: str):
        self.calls.append(run_id)
        return BarrierFlushResult(outcome="flushed")

    def close(self) -> None:
        return None


def test_flush_control_cleans_run_bookkeeping_without_retry() -> None:
    """A critical sink's BaseException still completes the one-pass barrier."""
    conn = _database()
    interrupting = _InterruptFlushOnce()
    trailing = _CountingFlush()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
        extra_sinks=(interrupting, trailing),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert host.wait(submitted.run_id, timeout=2).outcome == "completed"

        telemetry = _assert_completed_once(conn, submitted.run_id)
        assert telemetry["trace_incomplete"] is True
        assert interrupting.calls == [submitted.run_id], "flush must not retry"
        assert submitted.run_id not in host._fanout._disabled
        assert submitted.run_id not in host._fanout._persist_lost
        assert submitted.run_id not in host._fanout._last_envelope
        assert trailing.calls == [submitted.run_id]

        successor = host.submit(
            SubmitRequest(message="successor", session_id="sess-1")
        )
        assert successor.kind == "accepted"
        assert successor.run_id is not None
        assert host.wait(successor.run_id, timeout=2).outcome == "completed"
        assert interrupting.calls == [submitted.run_id, successor.run_id]
        assert trailing.calls == [submitted.run_id, successor.run_id]
        assert host._fanout._disabled == {}
        assert host._fanout._persist_lost == {}
        assert host._fanout._last_envelope == {}
    finally:
        host.close(timeout=2)


class _InterruptFinalizeCommit:
    """Commit the final Run transaction, then lose its return edge once."""

    def __init__(
        self,
        inner: sqlite3.Connection,
        make_exception: _ExceptionFactory,
    ) -> None:
        self._inner = inner
        self._make_exception = make_exception
        self._armed = False
        self._raised = False
        self.entered = threading.Event()
        self.calls = 0

    def execute(self, sql, parameters=()):
        result = self._inner.execute(sql, parameters)
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("UPDATE RUNS SET") and "FINISHED_AT = ?" in normalized:
            self._armed = True
        return result

    def commit(self):
        if not self._armed or self._raised:
            return self._inner.commit()
        self._armed = False
        self._raised = True
        self.calls += 1
        self._inner.commit()
        self.entered.set()
        raise self._make_exception()

    def rollback(self):
        return self._inner.rollback()

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_finalize_commit_then_base_exception_is_reconciled_without_duplicates(
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    raw = _database()
    interrupted = _InterruptFinalizeCommit(raw, make_exception)
    host = _build_host(
        interrupted,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
    )
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert interrupted.entered.wait(2), "finalizing commit was not reached"

        assert host.wait(submitted.run_id, timeout=2).outcome == "completed"
        telemetry = _assert_completed_once(raw, submitted.run_id)
        assert telemetry["trace_incomplete"] is False
        assert interrupted.calls == 1
        assert host.snapshot().coordinator_state == "idle"
        _assert_worker_accepts_a_successor(host)
        assert worker_failures == []
    finally:
        host.close(timeout=2)


@pytest.mark.parametrize("make_exception", _SETTLEMENT_EXCEPTIONS)
def test_idle_snapshot_publication_before_effect_is_resumed(
    make_exception: _ExceptionFactory,
    monkeypatch,
) -> None:
    """An interrupted idle snapshot cannot diverge from the internal owner."""
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
    )
    replace_snapshot = host._states.replace
    entered = threading.Event()
    resumed = threading.Event()
    idle_calls = 0

    def interrupt_before_idle_snapshot(*args, **kwargs):
        nonlocal idle_calls
        if kwargs.get("coordinator_state") == "idle":
            idle_calls += 1
            if idle_calls == 1:
                entered.set()
                raise make_exception()
            resumed.set()
        return replace_snapshot(*args, **kwargs)

    monkeypatch.setattr(host._states, "replace", interrupt_before_idle_snapshot)
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="hello", session_id="sess-1")
        )
        assert submitted.kind == "accepted"
        assert submitted.run_id is not None
        assert entered.wait(2), "worker did not reach the idle snapshot publish"

        assert host.wait(submitted.run_id, timeout=2).outcome == "completed"
        assert host._coord == "idle"
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "idle"
        assert snapshot.active_run is None
        assert snapshot.unrecorded_terminal_projection is None
        assert resumed.is_set(), "owner retry did not republish the idle snapshot"
        assert idle_calls == 2
        _assert_completed_once(conn, submitted.run_id)
        _assert_worker_accepts_a_successor(host)
        assert worker_failures == []
    finally:
        host.close(timeout=2)


def test_unstarted_handoff_idle_snapshot_before_effect_is_resumed(
    monkeypatch,
) -> None:
    """The finalized, rejected handoff resumes its half-published idle edge."""
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong"])),
    )
    publish_handoff = host.admission_publish_handoff
    replace_snapshot = host._states.replace
    rejected_run_ids: list[str] = []
    idle_calls = 0
    interrupted = SystemExit("unstarted idle snapshot interrupted")
    resumed = threading.Event()

    def reject_first_handoff(item) -> None:
        if not rejected_run_ids:
            rejected_run_ids.append(item.run_id)
            raise RuntimeError("handoff rejected")
        publish_handoff(item)

    def interrupt_first_idle_snapshot(*args, **kwargs):
        nonlocal idle_calls
        if kwargs.get("coordinator_state") == "idle":
            idle_calls += 1
            if idle_calls == 1:
                raise interrupted
            resumed.set()
        return replace_snapshot(*args, **kwargs)

    host.start()
    monkeypatch.setattr(host, "admission_publish_handoff", reject_first_handoff)
    monkeypatch.setattr(host._states, "replace", interrupt_first_idle_snapshot)
    try:
        with pytest.raises(SystemExit) as caught:
            host.submit(SubmitRequest(message="first", session_id="sess-1"))
        assert caught.value is interrupted
        assert len(rejected_run_ids) == 1
        first_run_id = rejected_run_ids[0]

        assert resumed.is_set(), "admission did not retry the idle publication"
        assert idle_calls == 2
        assert host._coord == "idle"
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "idle"
        assert snapshot.active_run is None
        assert snapshot.unrecorded_terminal_projection is None
        assert conn.execute(
            "SELECT phase, outcome, started_at FROM runs WHERE run_id = ?",
            (first_run_id,),
        ).fetchone() == ("finished", "interrupted", None)
        assert host._pending_handoff == set()
        assert first_run_id not in host._queue._cells
        assert first_run_id not in host._done
        assert first_run_id not in host._results

        _assert_worker_accepts_a_successor(host)
    finally:
        host.close(timeout=2)


def test_release_after_idle_retry_cannot_clear_a_concurrent_successor(
    monkeypatch,
) -> None:
    """An after-inner stale retry is owner-scoped across idle -> accepted ABA."""
    conn = _database()
    host = _build_host(
        conn,
        ScriptedModelFactory(ScriptedModel(["pong", "pong"])),
    )
    release = host.recording_publish_recorded_then_release
    released = threading.Event()
    allow_exception = threading.Event()
    retried = threading.Event()
    allow_retry_return = threading.Event()
    calls = 0

    def interrupt_after_idle(run_id: str) -> None:
        nonlocal calls
        calls += 1
        release(run_id)
        if calls == 1:
            released.set()
            assert allow_exception.wait(2), "test did not release settlement"
            raise SystemExit("after idle")
        if calls == 2:
            retried.set()
            assert allow_retry_return.wait(2), "test did not release the retry"

    monkeypatch.setattr(
        host, "recording_publish_recorded_then_release", interrupt_after_idle
    )
    worker_failures: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: worker_failures.append(args.exc_value),
    )
    host.start()
    first = None
    second = None
    try:
        first = host.submit(SubmitRequest(message="first", session_id="sess-1"))
        assert first.kind == "accepted"
        assert first.run_id is not None
        assert released.wait(2), "first Run did not release admission"
        assert host.snapshot().coordinator_state == "idle"

        second = host.submit(SubmitRequest(message="second", session_id="sess-1"))
        assert second.kind == "accepted"
        assert second.run_id is not None
        successor = host.snapshot()
        assert successor.coordinator_state == "accepted"
        assert successor.active_run is not None
        assert successor.active_run.run_id == second.run_id

        allow_exception.set()
        assert retried.wait(2), "old Run did not retry its release"
        during_retry = host.snapshot()
        assert during_retry.coordinator_state == "accepted"
        assert during_retry.active_run is not None
        assert during_retry.active_run.run_id == second.run_id
        assert conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (second.run_id,),
        ).fetchone() == ("accepted", None)

        allow_retry_return.set()
        assert host.wait(first.run_id, timeout=2).outcome == "completed"
        assert host.wait(second.run_id, timeout=2).outcome == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM runs WHERE phase != 'finished' OR outcome IS NULL"
        ).fetchone() == (0,)
        assert host.snapshot().coordinator_state == "idle"
        assert worker_failures == []
    finally:
        allow_exception.set()
        allow_retry_return.set()
        host.close(timeout=2)


# --- problem 6: a terminal Run has no outgoing edges -----------------------


def _finished_run(conn: sqlite3.Connection, outcome: str = "interrupted") -> str:
    _accepted_run(conn, "run-done")
    schema.update_run_phase(
        conn,
        run_id="run-done",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=TS,
        session_id="sess-1",
    )
    revision = schema.allocate_activity_revision(conn)
    schema.update_run_phase(
        conn,
        run_id="run-done",
        from_phase="running",
        to_phase="finished",
        activity_revision=revision,
        outcome=outcome,
        finished_at=TS,
        session_id="sess-1",
    )
    conn.commit()
    return "run-done"


def _row(conn: sqlite3.Connection, run_id: str = "run-done") -> tuple:
    return conn.execute(
        """SELECT phase, outcome, started_at, finished_at, activity_revision,
                  telemetry, purpose, session_id
             FROM runs WHERE run_id = ?""",
        (run_id,),
    ).fetchone()


def test_a_finished_run_cannot_move_to_finished() -> None:
    conn = _database()
    run_id = _finished_run(conn)
    before = _row(conn)
    with pytest.raises(schema.RunPhaseError, match="terminal"):
        schema.update_run_phase(
            conn,
            run_id=run_id,
            from_phase="finished",
            to_phase="finished",
            activity_revision=schema.allocate_activity_revision(conn),
            outcome="completed",
            finished_at="2026-08-28T00:00:01Z",
            session_id="sess-1",
        )
    assert _row(conn) == before


def test_a_finished_run_cannot_move_to_running() -> None:
    conn = _database()
    run_id = _finished_run(conn, outcome="completed")
    before = _row(conn)
    with pytest.raises(schema.RunPhaseError, match="terminal"):
        schema.update_run_phase(
            conn,
            run_id=run_id,
            from_phase="finished",
            to_phase="running",
            activity_revision=schema.allocate_activity_revision(conn),
            started_at="2026-08-28T00:00:01Z",
            session_id="sess-1",
        )
    assert _row(conn) == before


def test_finished_interrupted_cannot_become_completed() -> None:
    """The exact rewrite the ticket names: outcome, finished_at, the Run's
    activity_revision and the Session's must all stay put."""
    conn = _database()
    run_id = _finished_run(conn, outcome="interrupted")
    before = _row(conn)
    before_session = conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone()
    with pytest.raises(schema.RunPhaseError):
        schema.update_run_phase(
            conn,
            run_id=run_id,
            from_phase="finished",
            to_phase="finished",
            activity_revision=schema.allocate_activity_revision(conn),
            outcome="completed",
            finished_at="2026-08-28T00:00:02Z",
            telemetry=json.dumps({"attempts": []}),
            session_id="sess-1",
        )
    after = _row(conn)
    assert after == before, "every column is untouched"
    assert after[1] == "interrupted"
    assert conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone() == before_session


def test_an_illegal_transition_leaves_the_clock_and_session_untouched() -> None:
    """The caller allocates its activity_revision before the UPDATE and
    stamps the Session after it; a rejected transition must roll the whole
    transaction back, leaving no clock hole."""
    conn = _database()
    run_id = _finished_run(conn)
    clock_before = conn.execute("SELECT next_revision FROM activity_clock").fetchone()
    session_before = conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone()

    with pytest.raises(schema.RunPhaseError):
        with conn:  # one transaction: the caller's revision and the rewrite
            revision = schema.allocate_activity_revision(conn)
            schema.update_run_phase(
                conn,
                run_id=run_id,
                from_phase="finished",
                to_phase="running",
                activity_revision=revision,
                started_at=TS,
                session_id="sess-1",
            )
    conn.rollback()

    assert conn.execute("SELECT next_revision FROM activity_clock").fetchone() == (
        clock_before
    ), "no clock hole is left behind"
    assert conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone() == session_before
    assert _row(conn)[0] == "finished"


def test_the_legal_path_accepted_running_finished_still_works() -> None:
    conn = _database()
    _accepted_run(conn, "run-ok")
    schema.update_run_phase(
        conn,
        run_id="run-ok",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=TS,
        session_id="sess-1",
    )
    schema.update_run_phase(
        conn,
        run_id="run-ok",
        from_phase="running",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome="completed",
        finished_at=TS,
        session_id="sess-1",
    )
    conn.commit()
    assert _row(conn, "run-ok")[:3] == ("finished", "completed", TS)


@pytest.mark.parametrize("outcome", ["failed", "interrupted"])
def test_accepted_to_finished_failure_outcomes_still_work(outcome: str) -> None:
    """Pre-running execution failure, failed handoff, and recovery use this edge."""
    conn = _database()
    _accepted_run(conn, "run-never")
    schema.update_run_phase(
        conn,
        run_id="run-never",
        from_phase="accepted",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome=outcome,
        finished_at=TS,
        session_id="sess-1",
    )
    conn.commit()
    row = _row(conn, "run-never")
    assert row[0] == "finished"
    assert row[1] == outcome
    assert row[2] is None, "a Run that never started has no started_at"


@pytest.mark.parametrize("outcome", ["completed", "max_steps"])
def test_an_unstarted_run_cannot_finish_with_an_execution_outcome(
    outcome: str,
) -> None:
    conn = _database()
    _accepted_run(conn, "run-unstarted")
    before = _row(conn, "run-unstarted")
    clock_before = conn.execute("SELECT next_revision FROM activity_clock").fetchone()
    session_before = conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone()

    with pytest.raises(schema.RunPhaseError, match="unstarted"):
        with conn:
            revision = schema.allocate_activity_revision(conn)
            schema.update_run_phase(
                conn,
                run_id="run-unstarted",
                from_phase="accepted",
                to_phase="finished",
                activity_revision=revision,
                outcome=outcome,
                finished_at=TS,
                session_id="sess-1",
            )

    assert _row(conn, "run-unstarted") == before
    assert before[:4] == ("accepted", None, None, None)
    assert conn.execute("SELECT next_revision FROM activity_clock").fetchone() == (
        clock_before
    )
    assert conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 'sess-1'"
    ).fetchone() == session_before


@pytest.mark.parametrize(
    "outcome", ["completed", "max_steps", "failed", "interrupted"]
)
def test_a_running_run_accepts_every_terminal_outcome(outcome: str) -> None:
    conn = _database()
    _accepted_run(conn, "run-started")
    schema.update_run_phase(
        conn,
        run_id="run-started",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=TS,
        session_id="sess-1",
    )
    schema.update_run_phase(
        conn,
        run_id="run-started",
        from_phase="running",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome=outcome,
        finished_at=TS,
        session_id="sess-1",
    )

    assert _row(conn, "run-started")[:4] == ("finished", outcome, TS, TS)


def test_execution_failure_before_running_finalizes_the_accepted_run_failed() -> None:
    class _FailRunningUpdate:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner
            self._failed = False

        def execute(self, sql, parameters=()):
            if not self._failed and "started_at = ?" in sql:
                self._failed = True
                raise sqlite3.OperationalError("injected pre-running failure")
            return self._inner.execute(sql, parameters)

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    inner = _database()
    _accepted_run(inner)
    conn = _FailRunningUpdate(inner)
    coordinator = _RecordingCoordinator()
    recorder = _real_recorder(conn, coordinator)
    executor, _sink = _executor(
        conn,
        assistant=_RaisingAssistant(AssertionError("must not execute")),
        recorder=recorder,
    )

    executor.execute(_work_item())

    row = _row(inner, "run-term")
    assert row[:3] == ("finished", "failed", None)
    assert row[3] is not None
    assert coordinator.states == ["recording_pending", "recorded"]


def test_running_to_accepted_is_rejected() -> None:
    conn = _database()
    _accepted_run(conn, "run-back")
    schema.update_run_phase(
        conn,
        run_id="run-back",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=TS,
        session_id="sess-1",
    )
    before = _row(conn, "run-back")
    with pytest.raises(schema.RunPhaseError, match="illegal run transition"):
        schema.update_run_phase(
            conn,
            run_id="run-back",
            from_phase="running",
            to_phase="accepted",
            activity_revision=schema.allocate_activity_revision(conn),
        )
    assert _row(conn, "run-back") == before


def test_non_terminal_phases_reject_an_outcome() -> None:
    conn = _database()
    _accepted_run(conn, "run-shape")
    with pytest.raises(schema.RunPhaseError, match="outcome"):
        schema.update_run_phase(
            conn,
            run_id="run-shape",
            from_phase="accepted",
            to_phase="running",
            activity_revision=schema.allocate_activity_revision(conn),
            started_at=TS,
            outcome="completed",
        )


def test_finished_requires_a_legal_outcome() -> None:
    conn = _database()
    _accepted_run(conn, "run-outcome")
    for bad in (None, "banana"):
        with pytest.raises(schema.RunPhaseError, match="outcome"):
            schema.update_run_phase(
                conn,
                run_id="run-outcome",
                from_phase="accepted",
                to_phase="finished",
                activity_revision=schema.allocate_activity_revision(conn),
                outcome=bad,
                finished_at=TS,
            )
    assert _row(conn, "run-outcome")[0] == "accepted"


def test_a_repeated_finalizer_does_not_rewrite_the_first_terminal_state() -> None:
    """The finalizer's own guard used to be the only thing standing between a
    second finalize and the first terminal state."""
    conn = _database()
    _accepted_run(conn, "run-twice")
    schema.update_run_phase(
        conn,
        run_id="run-twice",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=TS,
        session_id="sess-1",
    )
    coordinator = _RecordingCoordinator()
    recorder = _real_recorder(conn, coordinator)
    item = _work_item("run-twice")
    for outcome in ("completed", "failed"):
        recorder._finalize(
            conn,
            item,
            outcome,
            text_message("assistant", "reply"),
            json.dumps({"attempts": []}),
            now=TS,
        )
    after = _row(conn, "run-twice")
    assert after[0] == "finished"
    assert after[1] == "completed", "the first terminal state is the one that stands"
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_log WHERE run_id = 'run-twice'"
    ).fetchone() == (2,), "the messages are written once, by the first finalize"


def test_the_transition_graph_is_closed() -> None:
    """The graph itself, read as data: every edge lands on a phase the CHECK
    accepts, and ``finished`` has no outgoing edge -- not even one back to
    itself."""
    graph = schema.RUN_PHASE_TRANSITIONS
    assert set(graph) == set(schema.PHASES), "the graph covers every phase"
    for source, targets in graph.items():
        for target in targets:
            assert target in schema.PHASES, f"{source} -> {target} leaves the phases"
    assert graph["finished"] == (), "a terminal Run never moves again"
    assert graph["accepted"] == ("running", "finished")
    assert graph["running"] == ("finished",)
    assert schema.OUTCOMES == (
        "completed",
        "max_steps",
        "failed",
        "interrupted",
    ), "the closed outcome set the graph pairs with 'finished'"
