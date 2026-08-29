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

import json
import queue
import sqlite3
import threading

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink, RunFinished
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

    def execution_mark_running(self, started_at: str):
        self.marked.append(started_at)
        return None


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

    def recording_publish_recorded_then_release(self) -> None:
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


def _build_host(conn, factory) -> RuntimeHost:
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    fanout = FanOutSink([capture], process_instance_id="proc-terminal")
    return RuntimeHost(
        conn=conn,
        factory=factory,
        settings=Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-terminal",
    )


class _CustomBaseException(BaseException):
    """A BaseException that is neither SystemExit nor KeyboardInterrupt."""


# --- problem 6: a terminal Run has no outgoing edges -----------------------


def _finished_run(conn: sqlite3.Connection, outcome: str = "interrupted") -> str:
    _accepted_run(conn, "run-done")
    revision = schema.allocate_activity_revision(conn)
    schema.update_run_phase(
        conn,
        run_id="run-done",
        from_phase="accepted",
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
    assert _row(conn, "run-ok")[:2] == ("finished", "completed")


def test_accepted_to_finished_interrupted_still_works() -> None:
    """Both callers that skip ``running`` -- the failed handoff and the
    startup recovery -- use this edge."""
    conn = _database()
    _accepted_run(conn, "run-never")
    schema.update_run_phase(
        conn,
        run_id="run-never",
        from_phase="accepted",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome="interrupted",
        finished_at=TS,
        session_id="sess-1",
    )
    conn.commit()
    row = _row(conn, "run-never")
    assert row[0] == "finished"
    assert row[1] == "interrupted"
    assert row[2] is None, "a Run that never started has no started_at"


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
