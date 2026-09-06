"""Issue #13 review: the RuntimeHost lifecycle.

Two contracts were not executable:

1. ``start()`` ran ``recover()`` before checking whether the Host had already
   started, so a second ``start()`` rewrote the live Run's index row out from
   under the worker that was still executing it.
2. ``close()`` closed the FanOut after a bounded 5s join, whether or not the
   worker had actually stopped. A worker still inside a model round-trip, a
   retry, or a finalizer then kept emitting events and running durability
   barriers against a closed sink.

A later closure adds a third contract: a sink whose ``close()`` raises must
leave the close unfinished and retryable, not reported as done.

Every test below fails against the pre-fix code for the reason named in its
docstring; none of them sleeps through a real timeout.
"""

from __future__ import annotations

import dis
import inspect
import os
import sqlite3
import threading

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    interrupt_instruction_once,
)
from agent_alfred.evals.deterministic._thread_test_helpers import (
    ProbeInterruptedUnstartedThread,
)
from agent_alfred.events import (
    BarrierFlushResult,
    BestEffortFlushResult,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    FlushResult,
    RunStarted,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.resource_rollback import (
    thread_exit_confirmed,
    thread_start_effect_happened,
)
from agent_alfred.runtime import host as host_module
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink

CLOSE_GRACE_S = 5.0




def _worker_start_instruction(*, after_effect: bool) -> int:
    instructions = tuple(dis.get_instructions(RuntimeHost.start))
    start_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_ATTR" and instruction.argval == "start"
    )
    call = next(
        index
        for index, instruction in enumerate(instructions[start_load:], start_load)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    return instructions[call + int(after_effect)].offset


def _native_thread_creation_boundary() -> int:
    """Land after CPython created the handle but before `_started.wait()`."""
    instructions = tuple(dis.get_instructions(threading.Thread.start))
    native_start = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL"
        and instruction.argval == "_start_joinable_thread"
    )
    call = next(
        index
        for index in range(native_start + 1, len(instructions))
        if instructions[index].opname in {"CALL", "CALL_KW"}
    )
    return instructions[call + 1].offset


def _worker_started_publication_instruction() -> int:
    """Locate the ownership publication after ``Thread.start`` returned."""
    source, first_line = inspect.getsourcelines(RuntimeHost.start)
    publication_line = first_line + max(
        index
        for index, text in enumerate(source)
        if "self._worker_started = True" in text
    )
    matches = [
        instruction.offset
        for instruction in dis.get_instructions(RuntimeHost.start)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_worker_started"
        and instruction.positions.lineno == publication_line
    ]
    assert len(matches) == 1
    return matches[0]


def _host(
    script: list | None = None,
    *,
    model: ScriptedModel | None = None,
    extra_sinks: list | None = None,
    publish_work=None,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
) -> tuple[RuntimeHost, sqlite3.Connection, CapturingSink, ScriptedModel]:
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(conn)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    fanout = FanOutSink(sinks, process_instance_id="proc-lifecycle")
    scripted = model if model is not None else ScriptedModel(script or ["pong"])
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(scripted),
        settings=settings or Settings(),
        clock=FakeClock(),
        fanout=fanout,
        process_instance_id="proc-lifecycle",
        publish_work=publish_work,
    )
    return host, conn, capture, scripted


def _instrument_recover(host: RuntimeHost) -> list[int]:
    """Count how many times the Host actually runs a recovery pass."""
    calls: list[int] = []
    real = host.recover

    def counting() -> None:
        calls.append(1)
        return real()

    host.recover = counting  # type: ignore[method-assign]
    return calls


class _FakeWorker:
    """A Thread stand-in that counts lifecycle calls without OS threads."""

    def __init__(self, start_error: BaseException | None = None):
        self.start_calls = 0
        self.join_calls = 0
        self._start_error = start_error
        self._alive = False

    def start(self) -> None:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls += 1
        self._alive = False


class _BlockingBarrierSink:
    """A persistence-critical sink whose barrier blocks until released."""

    name = "blocking-barrier"
    flush_at_run_end = True

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = 0

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event

    def flush(self, run_id: str) -> FlushResult:
        del run_id
        self.entered.set()
        self.release.wait(30.0)
        return BarrierFlushResult(outcome="flushed", dropped_events=0)

    def close(self) -> None:
        self.closed += 1


class _SentinelCountingQueue(host_module._HandoffQueue):
    """Counts the logical stop effects the Host posts to its work queue."""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.sentinels = 0

    def request_stop(self) -> bool:
        requested = super().request_stop()
        if requested:
            self.sentinels += 1
        return requested


class _StopRequestInterrupted(BaseException):
    """An asynchronous-looking control-flow exit with stable identity."""


class _InterruptingStopQueue(_SentinelCountingQueue):
    """Raise once immediately before or after the logical stop effect."""

    def __init__(
        self, store, *, after_effect: bool, failure: _StopRequestInterrupted
    ) -> None:
        super().__init__(store)
        self.after_effect = after_effect
        self.failure = failure
        self.request_calls = 0

    def request_stop(self) -> bool:
        self.request_calls += 1
        if self.request_calls == 1:
            if self.after_effect:
                super().request_stop()
            raise self.failure
        return super().request_stop()


def _run_worker_alive() -> bool:
    return any(
        thread.name == "run-worker" and thread.is_alive()
        for thread in threading.enumerate()
    )


def _idle_worker() -> _FakeWorker:
    """A replacement worker for tests that swapped in a counting stand-in, so
    teardown has something joinable that was never an OS thread."""
    return _FakeWorker()


# --- problem 1: start() must only ever recover on the true first start -----


def test_first_start_recovers_exactly_once() -> None:
    host, _conn, _capture, _model = _host(["pong"])
    calls = _instrument_recover(host)
    host.start()
    try:
        assert calls == [1], "the first start runs one recovery pass"
        assert host.started is True
    finally:
        host.close()


def test_repeat_start_on_an_idle_host_neither_recovers_nor_starts_twice() -> None:
    """The old code recovered before checking ``_started``: every repeat
    ``start()`` re-ran the startup-recovery rewrite."""
    host, _conn, _capture, _model = _host(["pong"])
    calls = _instrument_recover(host)
    host.start()
    try:
        host.start()
        host.start()
        assert calls == [1], f"recovery ran {len(calls)} times"
        assert host.started is True
    finally:
        host.close()


def test_repeat_start_never_starts_a_second_worker() -> None:
    host, _conn, _capture, _model = _host(["pong"])
    worker = _FakeWorker()
    host._worker = worker  # type: ignore[assignment]
    host.start()
    try:
        host.start()
        host.start()
        assert worker.start_calls == 1, "exactly one worker is ever started"
    finally:
        host._worker = _idle_worker()  # type: ignore[assignment]
        host.close()


def test_repeat_start_during_an_active_run_leaves_the_index_untouched() -> None:
    """The failure that matters: a second ``start()`` while a Run executes
    used to rewrite the live Run to finished/interrupted, after which the
    still-running worker's finalizer silently dropped the reply."""
    gate = threading.Event()
    host, conn, _capture, model = _host(model=ScriptedModel(["real-reply"], gate=gate))
    calls = _instrument_recover(host)
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message="hello", session_id=session_id)
        )
        assert submitted.kind == "accepted"
        assert model.entered.wait(2.0), "the worker must reach the model"
        assert host.snapshot().coordinator_state == "running"

        row_sql = """SELECT phase, outcome, started_at, finished_at,
                            activity_revision, telemetry
                       FROM runs WHERE run_id = ?"""
        before_run = conn.execute(row_sql, (submitted.run_id,)).fetchone()
        before_session = conn.execute(
            "SELECT activity_revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert before_run[0] == "running"
        assert before_run[1] is None

        host.start()  # the second start must not recover an active Run

        after_run = conn.execute(row_sql, (submitted.run_id,)).fetchone()
        after_session = conn.execute(
            "SELECT activity_revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert after_run == before_run, "the live Run's index row must not move"
        assert after_session == before_session
        assert calls == [1], "recovery must not run again"

        gate.set()
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        rows = conn.execute(
            "SELECT role FROM agent_log WHERE run_id = ? ORDER BY id",
            (submitted.run_id,),
        ).fetchall()
        assert rows == [("user",), ("assistant",)], (
            "the reply must still be recorded, not dropped by a stolen finalize"
        )
    finally:
        gate.set()
        host.close()


def test_concurrent_start_recovers_and_starts_exactly_once() -> None:
    """Two threads racing into ``start()`` must not both recover or both
    start a worker, and neither may observe a half-started Host."""
    host, _conn, _capture, _model = _host(["pong"])
    calls = _instrument_recover(host)
    worker = _FakeWorker()
    host._worker = worker  # type: ignore[assignment]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def racer() -> None:
        try:
            barrier.wait()
            host.start()
        except BaseException as exc:  # noqa: BLE001 - recorded, re-asserted below
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        assert errors == [], f"start() raised under concurrency: {errors}"
        assert calls == [1], "recovery is linearized to one pass"
        assert worker.start_calls == 1, "exactly one worker is started"
        assert host.started is True
    finally:
        host._worker = _idle_worker()  # type: ignore[assignment]
        host.close()


def test_recovery_failure_leaves_the_host_honestly_unstarted() -> None:
    """A Host whose recovery failed must not be marked started, must not
    recover a second time, and must not accept Runs nobody will execute."""
    host, _conn, _capture, _model = _host(["pong"])
    calls: list[int] = []

    def failing_recover() -> None:
        calls.append(1)
        raise sqlite3.OperationalError("injected recovery failure")

    host.recover = failing_recover  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        host.start()
    assert host.started is False, "a failed start must not publish 'started'"
    assert calls == [1]

    # A stable refusal: the Host does not silently retry recovery.
    with pytest.raises(RuntimeError, match="did not start"):
        host.start()
    assert calls == [1], "the failed Host never recovers a second time"

    refused = host.submit(SubmitRequest(message="hello"))
    assert refused.kind == "admission_failed", (
        "an unstarted Host must not accept a Run nobody will execute"
    )
    assert host.started is False


def test_worker_start_failure_leaves_the_host_honestly_unstarted() -> None:
    host, _conn, _capture, _model = _host(["pong"])
    calls = _instrument_recover(host)
    host._worker = _FakeWorker(  # type: ignore[assignment]
        start_error=RuntimeError("injected thread failure")
    )
    with pytest.raises(RuntimeError, match="injected thread failure"):
        host.start()
    assert host.started is False, "a worker that never started is not a started Host"
    assert calls == [1], "recovery ran once and its result stands"

    with pytest.raises(RuntimeError, match="did not start"):
        host.start()
    assert calls == [1]


def test_start_probe_detects_a_native_thread_before_started_is_published() -> None:
    """The CPython native-handle window is already a start effect."""
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    target_ran = threading.Event()

    class PausedBootstrapThread(threading.Thread):
        def _bootstrap_inner(self) -> None:
            bootstrap_entered.set()
            release_bootstrap.wait()
            super()._bootstrap_inner()

    worker = PausedBootstrapThread(target=target_ran.set, daemon=True)
    failure = KeyboardInterrupt("native thread created before _started")
    target = _native_thread_creation_boundary()

    try:
        with interrupt_instruction_once(
            threading.Thread.start.__code__, target, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                worker.start()
        assert armed == [False]
        assert raised.value is failure
        assert bootstrap_entered.wait(2.0), "native thread never bootstrapped"
        assert worker._started.is_set() is False
        assert worker._os_thread_handle.ident != 0

        assert thread_start_effect_happened(worker) is True
        assert thread_exit_confirmed(worker, timeout=0) is False
    finally:
        release_bootstrap.set()
        assert thread_exit_confirmed(worker, timeout=2.0) is True
    assert target_ran.is_set()


def test_host_close_retries_an_interrupted_unstarted_worker_probe() -> None:
    """An unresolved start refusal cannot strand Host close forever."""
    host, _conn, _capture, _model = _host(["pong"])
    worker = ProbeInterruptedUnstartedThread(
        start_failure=RuntimeError("host worker did not start"),
        probe_failure=KeyboardInterrupt("host start probe interrupted"),
        name="unstarted-run-worker",
    )
    host._worker = worker  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as raised:
        host.start()
    assert raised.value is worker.start_failure
    assert host.started is False
    assert worker.probe_calls == 1

    assert host.close(timeout=2.0) is True
    assert worker.probe_calls == 2
    assert host.closed is True


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_worker_start_exit_keeps_the_real_worker_owned_until_close(
    after_effect: bool,
) -> None:
    """A start-effect worker cannot outlive closed FanOut/database owners."""
    host, conn, _capture, _model = _host(["pong"])
    failure = KeyboardInterrupt(f"worker start {after_effect=}")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def gated_worker() -> None:
        entered.set()
        release.wait()
        exited.set()

    worker = threading.Thread(
        target=gated_worker, name="run-worker-start-boundary", daemon=True
    )
    host._worker = worker  # type: ignore[assignment]
    target = _worker_start_instruction(after_effect=after_effect)

    try:
        with interrupt_instruction_once(
            RuntimeHost.start.__code__, target, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                host.start()
        assert armed == [False]
        assert raised.value is failure
        assert host.started is False
        assert host.start_error is failure
        if not after_effect:
            assert worker.ident is None
            assert host.close(timeout=0) is True
            return

        assert entered.wait(2.0), "real worker target never entered"
        assert host.close(timeout=0) is False
        assert host._fanout_closed is False  # noqa: SLF001
        assert exited.is_set() is False
        release.set()
        assert exited.wait(2.0), "owned worker never exited"
        assert host.close(timeout=2.0) is True
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(timeout=2.0)
        host.close(timeout=2.0)
        conn.close()


def test_worker_start_store_exit_keeps_the_real_worker_owned_until_close() -> None:
    """A returned Thread.start effect is owned before its final publication."""
    host, conn, _capture, _model = _host(["pong"])
    failure = KeyboardInterrupt("worker start ownership publication interrupted")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def gated_worker() -> None:
        entered.set()
        release.wait()
        exited.set()

    worker = threading.Thread(
        target=gated_worker, name="run-worker-store-boundary", daemon=True
    )
    host._worker = worker  # type: ignore[assignment]
    target = _worker_started_publication_instruction()

    try:
        with interrupt_instruction_once(
            RuntimeHost.start.__code__, target, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                host.start()
        assert armed == [False]
        assert raised.value is failure
        assert entered.wait(2.0), "real worker target never entered"
        assert host.started is False
        assert host.start_error is failure
        assert host.close(timeout=0) is False
        assert host._fanout_closed is False  # noqa: SLF001
        assert exited.is_set() is False
        release.set()
        assert exited.wait(2.0), "owned worker never exited"
        assert host.close(timeout=2.0) is True
    finally:
        release.set()
        worker.join(timeout=2.0)
        host.close(timeout=2.0)
        conn.close()


def test_start_after_close_is_refused() -> None:
    host, _conn, _capture, _model = _host(["pong"])
    host.start()
    assert host.close() is True
    with pytest.raises(RuntimeError, match="closed"):
        host.start()
    assert host.started is True, "a closed Host does not rewrite its own history"


# --- problem 3: close() must never close the FanOut under a live worker ----


def test_close_keeps_the_fanout_open_while_the_worker_is_still_working() -> None:
    """The old ``close()`` closed the FanOut 5 seconds after asking the
    worker to stop, whether or not the worker had stopped."""
    sink = _BlockingBarrierSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "accepted"
        assert sink.entered.wait(2.0), "the worker must reach the barrier"

        closed = host.close(timeout=0.05)

        assert closed is False, "an unfinished worker is an unfinished close"
        assert sink.closed == 0, "the FanOut must not close under a live worker"
        assert host.closed is False
        assert _run_worker_alive(), "the worker is still finishing the Run"

        sink.release.set()
        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert sink.closed == 1, "the FanOut closes once, after the worker ends"
        assert not _run_worker_alive()
        assert host.closed is True
    finally:
        sink.release.set()
        host.close()


def test_default_close_timeout_is_bounded_and_honest(monkeypatch) -> None:
    """With the default timeout shrunk, the no-argument ``close()`` must
    report 'not closed' instead of closing the FanOut anyway."""
    monkeypatch.setattr(host_module, "_WORKER_JOIN_TIMEOUT_S", 0.05)
    sink = _BlockingBarrierSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "accepted"
        assert sink.entered.wait(2.0), "the worker must reach the barrier"

        assert host.close() is False
        assert sink.closed == 0, "a timed-out close closes nothing"

        sink.release.set()
        assert host.close() is True
        assert sink.closed == 1
    finally:
        sink.release.set()
        host.close()


def test_close_is_idempotent_and_concurrent_close_tears_down_once() -> None:
    sink = _BlockingBarrierSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    counting = _SentinelCountingQueue(host._store)
    host._queue = counting  # type: ignore[assignment]
    host._executor._work_queue = counting  # type: ignore[assignment]
    host.start()
    try:
        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert sink.closed == 1, "the FanOut is closed exactly once"
        assert counting.sentinels == 1, "one stop sentinel is ever posted"
    finally:
        host.close()

    sink2 = _BlockingBarrierSink()
    second, _conn2, _capture2, _model2 = _host(["pong"], extra_sinks=[sink2])
    counting2 = _SentinelCountingQueue(second._store)
    second._queue = counting2  # type: ignore[assignment]
    second._executor._work_queue = counting2  # type: ignore[assignment]
    second.start()
    barrier = threading.Barrier(4)
    results: list[bool] = []
    errors: list[BaseException] = []

    def closer() -> None:
        try:
            barrier.wait()
            results.append(second.close(timeout=CLOSE_GRACE_S))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=closer) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(CLOSE_GRACE_S)
        assert errors == [], f"close() raised under concurrency: {errors}"
        assert sorted(results) == [True] * 4, f"honest answers only: {results}"
        assert sink2.closed == 1, "concurrent close closes the FanOut exactly once"
        assert counting2.sentinels == 1
    finally:
        second.close()


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_interrupted_stop_request_remains_owned_and_retryable(
    after_effect: bool,
) -> None:
    """A stop request has one durable owner across either exit boundary.

    Raising the Host's completion flag before the queue call orphaned the
    worker after a before-effect exit. Moving that flag after the call alone
    would make an after-effect exit retry by appending a second sentinel. The
    queue must instead own one idempotent logical stop fact: the first close
    propagates the exact control-flow exception and the second completes the
    same request without leaving another item behind the exited worker.
    """
    sink = _BlockingBarrierSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    failure = _StopRequestInterrupted(f"stop request {after_effect=}")
    work = _InterruptingStopQueue(
        host._store, after_effect=after_effect, failure=failure
    )
    host._queue = work  # type: ignore[assignment]
    host._executor._work_queue = work  # type: ignore[assignment]
    host.start()
    try:
        with pytest.raises(_StopRequestInterrupted) as raised:
            host.close(timeout=CLOSE_GRACE_S)
        assert raised.value is failure
        assert host.closed is False
        assert host._stop_sent is False  # noqa: SLF001
        assert sink.closed == 0
        assert work.request_calls == 1
        assert work.sentinels == int(after_effect)

        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert host._worker.is_alive() is False  # noqa: SLF001
        assert work.request_calls == 2
        assert work.sentinels == 1, "the logical stop effect occurs exactly once"
        assert work.qsize() == 0, "no duplicate physical sentinel is left behind"
        assert sink.closed == 1

        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert work.request_calls == 2
        assert work.sentinels == 1
        assert sink.closed == 1
    finally:
        host.close(timeout=CLOSE_GRACE_S)


def test_close_racing_submit_never_orphans_a_work_item() -> None:
    """A Run admitted before ``close()`` began must still be executed; one
    admitted after it must be refused. The old code posted its sentinel and
    closed the FanOut immediately, so a Run could be accepted, enqueued
    behind the sentinel, and never executed by anybody."""
    reached = threading.Event()
    gate = threading.Event()
    host, conn, _capture, _model = _host(["pong"])

    def publish(item) -> None:
        reached.set()
        gate.wait(5.0)
        host._queue.put_nowait(item)

    host._publish_work = publish
    host.start()
    submitted: list = []
    try:
        submitter = threading.Thread(
            target=lambda: submitted.append(
                host.submit(SubmitRequest(message="hello"))
            )
        )
        submitter.start()
        assert reached.wait(2.0), "the submit must reach the handoff"

        # The handoff is still in flight: an honest close cannot finish yet.
        assert host.close(timeout=0.05) is False
        gate.set()
        submitter.join(5.0)
        assert not submitter.is_alive()
        assert len(submitted) == 1
        result = submitted[0]
        assert result.kind == "accepted", "the Run was admitted before close began"

        assert host.close(timeout=CLOSE_GRACE_S) is True
        # The accepted Run really ran: it reached a decided index row and the
        # submitter's waiter was notified instead of hanging forever.
        host.wait(result.run_id, timeout=CLOSE_GRACE_S)
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        assert row == ("finished", "completed"), "no Run is left unexecuted"
    finally:
        gate.set()
        host.close()


def test_submit_after_close_is_refused() -> None:
    host, conn, _capture, _model = _host(["pong"])
    host.start()
    assert host.close(timeout=CLOSE_GRACE_S) is True
    try:
        refused = host.submit(SubmitRequest(message="hello"))
        assert refused.kind == "admission_failed"
        assert refused.run_id is None
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,), (
            "a closed Host inserts no Run"
        )
    finally:
        host.close()


def test_close_before_start_still_closes_the_fanout_once() -> None:
    sink = _BlockingBarrierSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    assert host.close(timeout=CLOSE_GRACE_S) is True
    assert host.close(timeout=CLOSE_GRACE_S) is True
    assert sink.closed == 1, "an unstarted Host closes its sinks exactly once"
    assert host.closed is True
    assert host.submit(SubmitRequest(message="hello")).kind == "admission_failed"


# --- problem 4: a sink that refuses to close is an unfinished close ---------


class _FlakyCloseSink:
    """A sink whose close() refuses until released, counting every ask."""

    name = "flaky-close"
    flush_at_run_end = False

    def __init__(self) -> None:
        self.close_calls = 0
        self.release = threading.Event()

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared, event

    def flush(self, run_id: str) -> FlushResult:
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        self.close_calls += 1
        if not self.release.is_set():
            raise RuntimeError("sink close refused")


class _SpyCloseConnection(sqlite3.Connection):
    """A real connection that records whether its close was ever asked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _DrainingCloseSink(_FlakyCloseSink):
    """An SSEBroker-shaped sink: False means its writers still drain."""

    def close(self) -> bool:
        self.close_calls += 1
        return self.release.is_set()


def test_runtime_host_retries_fanout_close_before_reporting_closed() -> None:
    """A sink whose close() raises is an unfinished close, not a finished one.

    The old close() raised its ``_fanout_closed`` bit *before* calling the
    FanOut, so a sink that raised on the first attempt left the bit set:
    the next close() skipped the FanOut entirely, set ``_closed``, and
    reported the Host closed -- while the failed sink had never been closed
    by anyone, and never would be. The completion bits may only move behind
    the FanOut's own success: the failing close propagates unchanged, the
    Host keeps its closing semantics with everything it owns (its database
    included) still held, and the retry closes exactly the sink that
    failed -- only then do ``_fanout_closed`` and ``_closed`` advance. The
    entry descriptor and the process lock are not the Host's to release;
    that layer of the tail is proven at the Dashboard level.
    """
    sink = _FlakyCloseSink()
    conn = sqlite3.connect(
        ":memory:", check_same_thread=False, factory=_SpyCloseConnection
    )
    schema.migrate(conn)
    host, _conn, _capture, _model = _host(["pong"], conn=conn, extra_sinks=[sink])
    host.start()
    try:
        with pytest.raises(RuntimeError, match="sink close refused"):
            host.close(timeout=CLOSE_GRACE_S)

        assert sink.close_calls == 1
        # Neither progress bit advanced, and the Host is still closing.
        assert host._fanout_closed is False
        assert host.closed is False
        assert host._closing is True
        # The database the Host owns was never given up.
        assert conn.close_calls == 0

        sink.release.set()
        assert host.close(timeout=CLOSE_GRACE_S) is True
        # The retry asked the failed sink again, and only a real success
        # moved the completion bits.
        assert sink.close_calls == 2
        assert host._fanout_closed is True
        assert host.closed is True
        assert conn.close_calls == 0, "the Host never closes the database itself"
    finally:
        sink.release.set()
        host.close()


def test_runtime_host_retries_fanout_that_is_still_draining() -> None:
    """A non-drained stream sink keeps the Host honestly incomplete."""
    sink = _DrainingCloseSink()
    host, _conn, _capture, _model = _host(["pong"], extra_sinks=[sink])
    host.start()
    try:
        assert host.close(timeout=CLOSE_GRACE_S) is False
        assert sink.close_calls == 1
        assert host._fanout_closed is False
        assert host.closed is False

        sink.release.set()
        assert host.close(timeout=CLOSE_GRACE_S) is True
        assert sink.close_calls == 2
        assert host._fanout_closed is True
        assert host.closed is True
    finally:
        sink.release.set()
        host.close()


def test_runtime_host_keeps_trace_drain_pending_within_close_budget(
    tmp_path, monkeypatch
) -> None:
    """A live trace writer is an unfinished Host close, not a closed sink."""
    import agent_alfred.trace as trace_module

    real_write = os.write
    write_reached = threading.Event()
    release_write = threading.Event()

    def gated_trace_write(fd, data):
        if bytes(data[:6]) == b'{"seq"' and not write_reached.is_set():
            write_reached.set()
            release_write.wait(5.0)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", gated_trace_write)
    monkeypatch.setattr(trace_module, "_CLOSE_JOIN_TIMEOUT_S", 0.01)
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="proc-lifecycle",
    )
    host, _conn, _capture, _model = _host(extra_sinks=[trace])
    host.start()
    try:
        host._fanout.emit(
            RunStarted(purpose="chat", user_message=None),
            EventEnvelope(0.0, "run-close", None, None, None, None),
        )
        assert write_reached.wait(2.0), "trace drain did not reach the write"

        assert host.close(timeout=0.05) is False
        assert host.closed is False
        assert host._fanout_closed is False
        assert trace._drain.is_alive()

        release_write.set()
        assert host.close(timeout=2.0) is True
        assert host.closed is True
        assert trace._drain.is_alive() is False
    finally:
        release_write.set()
        host.close(timeout=2.0)


def test_keyboard_interrupt_produces_a_terminal_run_and_releases_the_lease() -> None:
    """Ctrl-C must leave a decided Run, not a lease held forever.

    Python delivers SIGINT to the main thread only, so a KeyboardInterrupt
    reaching the worker was raised by code, not by the user's Ctrl-C: the Run
    settles interrupted and the Host keeps serving rather than losing its
    only execution thread.
    """
    host, conn, _capture, _model = _host([KeyboardInterrupt(), "pong"])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "accepted"
        result = host.wait(submitted.run_id)
        assert result.outcome == "interrupted"
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?",
            (submitted.run_id,),
        ).fetchone()
        assert row == ("finished", "interrupted"), "the Run is decided, not abandoned"
        assert host.snapshot().coordinator_state == "idle"
        again = host.submit(SubmitRequest(message="again"))
        assert again.kind == "accepted", (
            "the lease is released, not held as run_in_progress forever"
        )
        assert host.wait(again.run_id).outcome == "completed", (
            "the Host still serves Runs after an interrupted one"
        )
    finally:
        host.close()
