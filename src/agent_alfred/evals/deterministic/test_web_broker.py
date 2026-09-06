"""SSEBroker: reconnect without holes, per-connection backpressure, overflow."""

from __future__ import annotations

import dis
import gc
import inspect
import json
import queue
import re
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import CodeType
from typing import get_args

import pytest

from agent_alfred import events as events_module
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
    interrupt_instruction_once,
)
from agent_alfred.evals.deterministic._thread_test_helpers import (
    ProbeInterruptedUnstartedThread,
)
from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    GatedThreads,
    GatedWriteConnection,
    Harness,
    ObservedConnection,
    RealThreadSpawner,
    _NoThreads,
    cursor_for,
    drain_connection,
    drain_dispatcher,
    replay_ids,
    runtime_snapshot,
    user_message_with,
)
from agent_alfred.events import (
    AttemptCommitted,
    AttemptStarted,
    BestEffortFlushResult,
    BlockDelta,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    Notice,
    ProcessFatalSinkError,
    RunFinished,
    RunStarted,
    SequencedEvent,
    StepStarted,
    UnsequencedEvent,
    event_json_default,
)
from agent_alfred.gateway.web import broker as broker_module
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.broker import (
    SSEBroker,
    _IngressKick,
    _IngressStop,
    _patch_frames,
    _PublishedEvent,
)
from agent_alfred.gateway.web.connection import (
    CloseConnection,
    ConnectionQueue,
    ConnectionWriter,
    FakeConnection,
    OfferOutcome,
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.progress import (
    AttemptTerminal,
    RunProgress,
    StepProjection,
)
from agent_alfred.gateway.web.replay import AppendResult, ReplayBatch, ReplayRing
from agent_alfred.gateway.web.state import SNAPSHOT_TEXT_LIMIT
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.session_validity import SessionValidity

INSTANCE = "inst-test"




@contextmanager
def _signal_then_gate_broker_instructions(
    code: CodeType,
    signal_offset: int,
    gate_offset: int,
) -> Iterator[
    tuple[list[bool], threading.Event, threading.Event, threading.Event]
]:
    """Signal one instruction, then park the same thread at a later one."""
    armed = [True, True]
    signaled = threading.Event()
    gated = threading.Event()
    release = threading.Event()

    with claimed_monitoring_tool(
        "broker-boundary-gate", local_codes=(code,)
    ) as tool_id:

        def monitor(actual_code: CodeType, actual_offset: int) -> None:
            if actual_code is not code:
                return
            if armed[0] and actual_offset == signal_offset:
                armed[0] = False
                signaled.set()
                return
            if armed[1] and actual_offset == gate_offset:
                armed[1] = False
                gated.set()
                release.wait()

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, monitor
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        try:
            yield armed, signaled, gated, release
        finally:
            release.set()


def _instruction_for_broker_line(
    function, needle: str, *, opname: str
) -> int:
    source, first_line = inspect.getsourcelines(function)
    line = first_line + next(
        index for index, text in enumerate(source) if needle in text
    )
    return next(
        instruction.offset
        for instruction in dis.get_instructions(function)
        if instruction.opname == opname
        and instruction.positions.lineno == line
    )


def _stream_transfer_instruction(boundary: str) -> int:
    """Locate one ownership publication edge in ``start_stream``."""
    instructions = tuple(dis.get_instructions(SSEBroker.start_stream))
    if boundary in {
        "spawn_call",
        "spawn_return",
        "registration_call",
        "registration_return",
    }:
        attribute = (
            "_spawn"
            if boundary.startswith("spawn")
            else "_finish_stream_registration"
        )
        load = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "LOAD_ATTR"
            and instruction.argval == attribute
        )
        call = next(
            index
            for index, instruction in enumerate(instructions[load:], load)
            if instruction.opname in {"CALL", "CALL_KW"}
        )
        return instructions[
            call if boundary.endswith("call") else call + 1
        ].offset
    if boundary == "return":
        transferred = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_ATTR"
            and instruction.argval == "transferred"
        )
        return next(
            instruction.offset
            for instruction in instructions[transferred + 1 :]
            if instruction.opname == "RETURN_VALUE"
        )
    attribute, side = boundary.rsplit("_", 1)
    store = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == attribute
    )
    if side == "before":
        return instructions[store].offset
    assert side == "after"
    return instructions[store + 1].offset


def _dispatcher_start_instruction(
    *, after_effect: bool
) -> tuple[CodeType, int]:
    """Locate the default dispatcher's real ``Thread.start`` boundary."""
    for function in (SSEBroker.start, broker_module._spawn_thread):
        instructions = tuple(dis.get_instructions(function))
        start_load = next(
            (
                index
                for index, instruction in enumerate(instructions)
                if instruction.opname == "LOAD_ATTR"
                and instruction.argval == "start"
            ),
            None,
        )
        if start_load is None:
            continue
        call = next(
            index
            for index, instruction in enumerate(
                instructions[start_load:], start_load
            )
            if instruction.opname in {"CALL", "CALL_KW"}
        )
        return function.__code__, instructions[
            call + int(after_effect)
        ].offset
    raise AssertionError("default dispatcher has no Thread.start boundary")


# --- connect and reconnect -------------------------------------------------


def test_transport_notice_code_is_the_decided_two_value_closed_set() -> None:
    assert set(get_args(frames.TransportNoticeCode)) == {
        "replay_gap",
        "deltas_dropped",
    }


def test_startup_replay_fetch_has_no_semantically_dead_budget_parameter() -> None:
    assert tuple(inspect.signature(SSEBroker._fetch_startup_replay).parameters) == (
        "self",
        "source",
        "progress",
        "through_seq",
    )


def test_broker_refuses_a_frame_limit_without_normal_sequence_room() -> None:
    with pytest.raises(ValueError, match="max_frame_bytes must be >= 288"):
        Harness(max_frame_bytes=287)


def test_broker_rejects_a_budget_above_the_physical_frame_limit() -> None:
    with pytest.raises(ValueError, match="max_frame_bytes must be <= 1048576"):
        SSEBroker(
            process_instance_id="inst",
            snapshot=runtime_snapshot(),
            max_frame_bytes=1_048_577,
        )


def test_a_preflight_proof_is_the_only_session_fact_used_to_open_the_stream() -> None:
    answers = iter(("valid", "unavailable"))
    queries: list[str | None] = []

    class WriteObservedConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.startup_written = threading.Event()

        def write(self, data: bytes) -> None:
            super().write(data)
            if b"event: state_patch" in data:
                self.startup_written.set()

    def validity(session_id: str | None):
        queries.append(session_id)
        return next(answers)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=validity,
    )
    proof = broker.preflight_session("s1")

    connection = WriteObservedConnection()
    try:
        handle = broker.connect(
            connection=connection,
            session_id="s1",
            admission=proof,
        )

        assert queries == ["s1"]
        assert handle.session_valid is True
        assert connection.startup_written.wait(2.0), "startup snapshot was not sent"
        assert connection.writes[0] == b"retry: 1000\n\n"
        assert connection.writes[1].startswith(b"id: inst-test:")
        assert b"event: state_patch" in connection.writes[2]
    finally:
        assert broker.close(timeout=2.0) is True
    assert handle.finished.is_set(), "the test retained its stream writer"


def test_a_full_admission_is_transferred_once_without_rechecking_session() -> None:
    queries: list[str | None] = []

    def validity(session_id: str | None):
        queries.append(session_id)
        return "valid" if len(queries) == 1 else "unavailable"

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=validity,
        spawn=_NoThreads().spawn,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection, session_id="s1")

    handle = broker.start_stream(proof)

    assert queries == ["s1"]
    assert handle in broker.connections
    with pytest.raises(RuntimeError, match="already transferred"):
        broker.start_stream(proof)


def test_connect_owns_a_prepared_stream_before_its_proof_store() -> None:
    """A lost proof cannot leave a registration that blocks close forever."""
    failure = KeyboardInterrupt("prepared proof returned before caller store")
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=_NoThreads().spawn,
    )
    connection = FakeConnection()
    code = SSEBroker.connect.__code__
    target = next(
        instruction.offset
        for instruction in reversed(tuple(dis.get_instructions(code)))
        if instruction.opname in {"STORE_FAST", "STORE_DEREF"}
        and instruction.argval == "proof"
    )

    with interrupt_instruction_once(code, target, failure) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.connect(connection=connection)

    assert armed == [False]
    assert raised.value is failure
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert broker.close(timeout=0) is True


def test_broker_retains_a_lost_prepared_acquisition_until_abort_completes(
    monkeypatch,
) -> None:
    """The registry owns cleanup when proof construction loses its return."""
    failure = SystemExit("prepared acquisition returned before proof store")
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=_NoThreads().spawn,
    )
    connection = FakeConnection()
    real_request_close = ConnectionQueue.request_close
    attempts = 0

    def fail_twice(source):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("queue abort unavailable")
        return real_request_close(source)

    monkeypatch.setattr(ConnectionQueue, "request_close", fail_twice)
    code = SSEBroker.prepare_stream.__code__
    instructions = tuple(dis.get_instructions(code))
    proof_store = next(
        instruction.offset
        for instruction in instructions
        if instruction.opname in {"STORE_FAST", "STORE_DEREF"}
        and instruction.argval == "proof"
    )

    with interrupt_instruction_once(code, proof_store, failure) as armed:
        with pytest.raises(SystemExit) as caught:
            broker.prepare_stream(connection=connection)

    assert armed == [False]
    assert caught.value is failure
    assert broker.registrations_in_flight == 1
    assert broker.close(timeout=0) is False
    assert attempts == 2
    assert broker.close(timeout=0) is True
    assert attempts == 3
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0


def test_connect_does_not_reclose_a_writer_owned_connection_at_handle_store() -> None:
    """After transfer, the registry/writer -- not ``connect`` -- owns the peer."""
    control = SystemExit("writer handle returned before caller store")
    write_entered = threading.Event()
    release_write = threading.Event()
    threads: list[threading.Thread] = []

    class GatedConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def write(self, data: bytes) -> None:
            write_entered.set()
            release_write.wait()
            super().write(data)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn,
    )
    connection = GatedConnection()
    code = SSEBroker.connect.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_FAST" and instruction.argval == "handle"
    )
    try:
        with interrupt_instruction_once(code, target, control) as armed:
            with pytest.raises(SystemExit) as caught:
                broker.connect(connection=connection)

        assert armed == [False]
        assert caught.value is control
        assert write_entered.wait(2.0), "transferred writer never reached the peer"
        assert connection.close_calls == 0
        assert len(broker.connections) == 1
    finally:
        release_write.set()
        broker.close(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)


def test_connect_exposes_an_incomplete_prepared_stream_rollback(
    monkeypatch,
) -> None:
    """A refused admission cannot masquerade as fully cleaned up."""
    from agent_alfred.gateway.web.broker import StreamAdmissionRejected
    from agent_alfred.resource_rollback import IncompleteRollback

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=_NoThreads().spawn,
    )
    connection = FakeConnection()
    handle = object()
    may_finish = False

    def reject(*args, _rollback, **kwargs):
        del args, kwargs
        token = object()
        _rollback.own(token, lambda: may_finish)
        raise StreamAdmissionRejected("injected refusal", handle)

    monkeypatch.setattr(broker, "prepare_stream", reject)
    with pytest.raises(StreamAdmissionRejected) as caught:
        broker.connect(connection=connection)

    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert cleanup.owner.errors == ()
    assert connection.closed is False
    may_finish = True
    assert cleanup.retry() is True
    connection.close()


def test_a_writer_spawn_failure_revokes_the_prepared_admission_once() -> None:
    failure = RuntimeError("injected writer handoff failure")
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=lambda _target: (_ for _ in ()).throw(failure),
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    assert handle.queue.current_cost.frames > 0

    with pytest.raises(RuntimeError) as raised:
        broker.start_stream(proof)

    assert raised.value is failure
    assert connection.closed is True
    assert handle.finished.is_set()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    broker.abort_stream(proof)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_writer_cleanup_failure_keeps_the_handle_owned_until_broker_retry(
    monkeypatch,
) -> None:
    """A failing queue cleanup cannot skip the socket or fake completion."""
    threads = _NoThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=threads.spawn,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = broker.start_stream(proof)
    handle.queue.stop()
    real_finish = handle.queue.finish
    attempts = 0

    def fail_twice():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("queue finish unavailable")
        real_finish()

    monkeypatch.setattr(handle.queue, "finish", fail_twice)
    target_errors: list[BaseException] = []
    try:
        threads.targets[0]()
    except BaseException as exc:  # noqa: BLE001 - pre-fix testimony
        target_errors.append(exc)

    assert attempts == 2
    assert connection.closed is True
    assert handle in broker.connections
    assert handle.finished.is_set() is False
    assert broker.close(timeout=0) is True
    assert attempts == 3
    assert handle.finished.is_set()
    assert broker.connections == ()
    assert target_errors == []


@pytest.mark.parametrize(
    "boundary",
    [
        "spawn_call",
        "spawn_return",
        "thread_before",
        "thread_after",
        "registration_call",
        "registration_return",
        "transferred_before",
        "transferred_after",
    ],
)
def test_start_stream_transfer_exit_revokes_the_prepared_admission(
    boundary: str,
) -> None:
    """A post-spawn exit cannot orphan the registered pre-writer owner."""
    failure = KeyboardInterrupt("writer transfer interrupted")
    threads = _NoThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=threads.spawn,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    target = _stream_transfer_instruction(boundary)

    with interrupt_instruction_once(
        SSEBroker.start_stream.__code__, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.start_stream(proof)

    assert armed == [False]
    assert raised.value is failure
    assert connection.closed is True
    assert handle.finished.is_set()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert broker.close(timeout=0) is True
    assert broker.close(timeout=0) is True


def test_start_stream_return_exit_leaves_a_closeable_transferred_writer() -> None:
    """The public return edge is after complete writer ownership transfer."""
    failure = KeyboardInterrupt("start_stream return interrupted")
    target_started = threading.Event()
    target_finished = threading.Event()

    def spawn(target):
        def run() -> None:
            target_started.set()
            try:
                target()
            finally:
                target_finished.set()

        thread = threading.Thread(target=run, daemon=True)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    target = _stream_transfer_instruction("return")

    with interrupt_instruction_once(
        SSEBroker.start_stream.__code__, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.start_stream(proof)

    assert armed == [False]
    assert raised.value is failure
    assert target_started.wait(2.0), "spawned writer never entered its target"
    assert broker.registrations_in_flight == 0
    assert broker.close(timeout=2.0) is True
    assert target_finished.wait(0), "close returned before the writer target"
    assert handle.finished.is_set()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.close(timeout=0) is True


def test_start_stream_start_after_effect_never_runs_an_aborted_writer() -> None:
    """A started target waits until the caller commits its ownership transfer."""
    failure = KeyboardInterrupt("start raised after starting writer target")
    spawned = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    thread: threading.Thread | None = None

    class StartThenFailThread:
        def __init__(self, target) -> None:
            nonlocal thread

            def run() -> None:
                spawned.set()
                release.wait()
                try:
                    target()
                finally:
                    finished.set()

            thread = threading.Thread(target=run, daemon=True)
            self.thread = thread

        def start(self) -> None:
            self.thread.start()
            assert spawned.wait(2.0), "spawned target never reached its gate"
            raise failure

        def join(self, timeout=None) -> None:
            self.thread.join(timeout)

        def is_alive(self) -> bool:
            return self.thread.is_alive()

    def spawn_unstarted(target):
        return StartThenFailThread(target)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn_unstarted,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle

    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.start_stream(proof)
        assert raised.value is failure
        assert spawned.is_set(), "thread start after-effect was not reached"
        assert thread is not None and thread.is_alive()
        assert finished.is_set() is False
        assert broker.close(timeout=0) is False
        release.set()
        assert finished.wait(0.5), "aborted writer target consumed the closed queue"
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert connection.writes == []
        assert connection.closed is True
        assert handle.finished.is_set()
        assert handle.queue.current_cost == frames.FrameCost(0, 0)
        assert broker.connections == ()
        assert broker.registrations_in_flight == 0
        assert broker.close(timeout=0) is True
        assert broker.close(timeout=0) is True
    finally:
        release.set()
        handle.queue.stop()
        if thread is not None:
            thread.join(timeout=2.0)
        assert broker.close(timeout=2.0) is True


@pytest.mark.parametrize("after_effect", [False, True], ids=["before", "native"])
def test_close_owns_a_native_writer_before_started_is_published(
    after_effect: bool,
) -> None:
    """The native creation effect is owned before the alive flag appears."""
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    spawned: list[threading.Thread] = []
    failure = KeyboardInterrupt("writer start interrupted at native boundary")

    class PausedBootstrapThread(threading.Thread):
        def _bootstrap_inner(self) -> None:
            bootstrap_entered.set()
            release_bootstrap.wait()
            super()._bootstrap_inner()

    def spawn(target):
        thread = PausedBootstrapThread(target=target, daemon=True)
        spawned.append(thread)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    if after_effect:
        code = threading.Thread.start.__code__
        native_call = _instruction_for_broker_line(
            threading.Thread.start, "_start_joinable_thread(", opname="CALL_KW"
        )
        target = next(
            instruction.offset
            for instruction in dis.get_instructions(code)
            if instruction.offset > native_call
        )
    else:
        code = SSEBroker.start_stream.__code__
        target = _instruction_for_broker_line(
            SSEBroker.start_stream, "handle.thread.start()", opname="CALL"
        )
    try:
        with interrupt_instruction_once(code, target, failure) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                broker.start_stream(proof)
        assert armed == [False]
        assert raised.value is failure
        assert len(spawned) == 1
        thread = spawned[0]
        if after_effect:
            assert bootstrap_entered.wait(2.0), "native bootstrap was not reached"
        assert bootstrap_entered.is_set() is after_effect
        assert bool(thread._os_thread_handle.ident) is after_effect
        assert thread.is_alive() is False
        assert connection.writes == []
        assert connection.closed is True
        assert broker.close(timeout=0) is (not after_effect)
    finally:
        release_bootstrap.set()
        for thread in spawned:
            if thread._os_thread_handle.ident:
                thread._os_thread_handle.join(2.0)
                assert thread._os_thread_handle.is_done()
        assert broker.close(timeout=2.0) is True
    assert broker.close(timeout=0) is True


def test_start_stream_abort_fences_a_dispatcher_that_captured_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration identity cannot decide whether a stale offer is safe."""
    failure = KeyboardInterrupt("writer transfer interrupted after registration")
    harness = Harness()
    broker = harness.broker
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    harness.emit(RunStarted(purpose="chat"))
    offer_entered = threading.Event()
    release_offer = threading.Event()
    delivery_done = threading.Event()
    outcomes: list[OfferOutcome] = []
    delivery_errors: list[BaseException] = []
    real_offer = handle.queue.offer

    def gated_offer(item, **kwargs):
        offer_entered.set()
        release_offer.wait()
        outcome = real_offer(item, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(handle.queue, "offer", gated_offer)

    def deliver() -> None:
        try:
            assert broker.deliver_next(timeout=0)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test thread
            delivery_errors.append(exc)
        finally:
            delivery_done.set()

    dispatcher = threading.Thread(target=deliver, daemon=True)
    dispatcher.start()
    assert offer_entered.wait(2.0), "dispatcher never captured the registered handle"
    target = _stream_transfer_instruction("registration_return")

    try:
        with interrupt_instruction_once(
            SSEBroker.start_stream.__code__, target, failure
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                broker.start_stream(proof)
        assert armed == [False]
        assert raised.value is failure
    finally:
        release_offer.set()

    assert delivery_done.wait(2.0), "captured dispatcher offer never settled"
    dispatcher.join(timeout=0)
    assert delivery_errors == []
    assert [outcome.kind for outcome in outcomes] == ["dropped"]
    assert handle.queue.close_requested is True
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert handle.finished.is_set()
    assert connection.closed is True
    assert broker.close(timeout=0) is True
    assert broker.close(timeout=0) is True


def test_close_waits_for_an_entered_aborted_stream_handoff() -> None:
    """An entered target remains a close owner until its aborted branch exits."""
    failure = KeyboardInterrupt("start interrupted after target entry")
    target_finished = threading.Event()
    spawned_thread: threading.Thread | None = None
    handoff_claimed: threading.Event

    class StartThenFailThread:
        def __init__(self, target) -> None:
            nonlocal spawned_thread

            def run() -> None:
                try:
                    target()
                finally:
                    target_finished.set()

            spawned_thread = threading.Thread(target=run, daemon=True)
            self.thread = spawned_thread

        def start(self) -> None:
            self.thread.start()
            assert handoff_claimed.wait(2.0), (
                "spawned handoff never registered its close ownership"
            )
            raise failure

        def join(self, timeout=None) -> None:
            self.thread.join(timeout)

        def is_alive(self) -> bool:
            return self.thread.is_alive()

    def spawn_unstarted(target):
        return StartThenFailThread(target)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn_unstarted,
    )
    connection = FakeConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    instructions = tuple(
        dis.get_instructions(SSEBroker._run_stream_handoff)  # noqa: SLF001
    )
    claim_boundary = next(
        instruction.offset
        for instruction in instructions
        if instruction.opname == "LOAD_ATTR"
        and instruction.argval == "ownership_lock"
    )
    ownership_read = next(
        instruction.offset
        for instruction in reversed(instructions)
        if instruction.opname == "LOAD_ATTR"
        and instruction.argval == "aborted"
    )

    with _signal_then_gate_broker_instructions(
        SSEBroker._run_stream_handoff.__code__,  # noqa: SLF001
        claim_boundary,
        ownership_read,
    ) as (armed, claimed, handoff_paused, release_handoff):
        handoff_claimed = claimed
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                broker.start_stream(proof)
            assert raised.value is failure
            assert handoff_paused.wait(2.0), (
                "aborted handoff never reached the post-lock ownership read"
            )
            assert armed == [False, False]
            assert target_finished.is_set() is False
            assert handle.finished.is_set() is False
            assert broker.connections == ()
            assert broker.registrations_in_flight == 0
            assert broker.close(timeout=0) is False
        finally:
            release_handoff.set()

    assert target_finished.wait(2.0), "aborted handoff target never exited"
    assert spawned_thread is not None
    spawned_thread.join(timeout=2.0)
    assert not spawned_thread.is_alive(), "aborted handoff thread never exited"
    assert handle.finished.is_set()
    assert connection.closed is True
    assert broker.close(timeout=0) is True
    assert broker.close(timeout=0) is True


def test_close_reaps_a_dead_stream_handoff_tombstone() -> None:
    """A target exit between finished and unregister cannot poison every close."""
    failure = SystemExit("handoff unregister interrupted")
    writer_entered = threading.Event()
    target_finished = threading.Event()
    target_failures: list[BaseException] = []

    class WriteObservedConnection(FakeConnection):
        def write(self, data: bytes) -> None:
            super().write(data)
            writer_entered.set()

    def spawn(target):
        def run() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - exact target testimony
                target_failures.append(exc)
            finally:
                target_finished.set()

        thread = threading.Thread(target=run, daemon=True)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn,
    )
    connection = WriteObservedConnection()
    proof = broker.prepare_stream(connection=connection)
    handle = proof._handle
    handoff_source, handoff_first_line = inspect.getsourcelines(
        SSEBroker._run_stream_handoff  # noqa: SLF001
    )
    cleanup_line = handoff_first_line + next(
        index
        for index, text in enumerate(handoff_source)
        if "_reap_stream_handoffs_locked" in text
    )
    handoff_instructions = tuple(
        dis.get_instructions(SSEBroker._run_stream_handoff)  # noqa: SLF001
    )
    writer_call = next(
        index
        for index, instruction in enumerate(handoff_instructions)
        if instruction.opname == "LOAD_ATTR"
        and instruction.argval == "_run_writer"
    )
    target = next(
        instruction.offset
        for instruction in handoff_instructions[writer_call + 1 :]
        if instruction.opname == "LOAD_ATTR"
        and instruction.argval == "_reap_stream_handoffs_locked"
        and instruction.positions.lineno == cleanup_line
    )

    with interrupt_instruction_once(
        SSEBroker._run_stream_handoff.__code__,  # noqa: SLF001
        target,
        failure,
    ) as armed:
        broker.start_stream(proof)
        assert writer_entered.wait(2.0), "writer never consumed its startup"
        closed = broker.close(timeout=2.0)

    assert armed == [False]
    assert target_finished.wait(0), "close returned before the target exited"
    assert target_failures == [failure]
    assert handle.finished.is_set()
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert broker._active_stream_handoffs == {}  # noqa: SLF001
    assert closed is True
    assert broker.close(timeout=0) is True


@pytest.mark.parametrize("pause_before_thread_exit", [False, True])
def test_close_waits_for_thread_exit_after_writer_cleanup(
    pause_before_thread_exit: bool,
) -> None:
    """A finished stream is not proof that its owning thread has exited."""
    target_returned = threading.Event()
    release_thread = threading.Event()
    if not pause_before_thread_exit:
        release_thread.set()

    class DisconnectedConnection(FakeConnection):
        def write(self, data: bytes) -> None:
            super().write(data)
            raise BrokenPipeError("test peer disconnected")

    def spawn(target):
        def run() -> None:
            target()
            target_returned.set()
            release_thread.wait()

        return threading.Thread(target=run, daemon=True)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _session_id: "valid",
        spawn=spawn,
    )
    connection = DisconnectedConnection()
    handle = broker.connect(connection=connection)
    try:
        assert target_returned.wait(2.0), "writer target never returned"
        assert connection.writes, "peer-disconnect injection was not reached"
        assert connection.closed is True
        assert handle.finished.is_set()
        assert handle.thread is not None
        if not pause_before_thread_exit:
            handle.thread.join(2.0)
        assert handle.thread.is_alive() is pause_before_thread_exit
        assert broker.close(timeout=0) is (not pause_before_thread_exit)
    finally:
        release_thread.set()
        if handle.thread is not None:
            handle.thread.join(2.0)
            assert not handle.thread.is_alive()
        assert broker.close(timeout=2.0) is True
    assert broker.close(timeout=0) is True


def test_connect_closes_the_connection_when_session_validation_raises() -> None:
    """A handed-off HTTP socket cannot escape a failed Session lookup."""
    failure = RuntimeError("injected Session lookup failure")

    def fail_session_lookup(_session_id: str | None) -> bool:
        raise failure

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=fail_session_lookup,
    )
    connection = FakeConnection()

    with pytest.raises(RuntimeError) as raised:
        broker.connect(connection=connection, session_id="s1")

    assert raised.value is failure
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0


def test_connect_cleans_the_unowned_handle_when_startup_encoding_raises(
    monkeypatch,
) -> None:
    """Startup frames and the socket die together when encoding cannot finish."""
    failure = ValueError("injected startup encoding failure")
    created = []
    real_handle = broker_module.ConnectionHandle

    def capture_handle(**kwargs):
        handle = real_handle(**kwargs)
        created.append(handle)
        return handle

    def fail_patch_encoding(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(broker_module, "ConnectionHandle", capture_handle)
    monkeypatch.setattr(broker_module, "_patch_frames", fail_patch_encoding)
    harness = Harness()
    connection = FakeConnection()

    with pytest.raises(ValueError) as raised:
        harness.connect(connection=connection, cursor=cursor_for(0))

    assert raised.value is failure
    assert len(created) == 1
    handle = created[0]
    assert connection.closed is True
    assert handle.finished.is_set()
    assert handle.thread is None
    assert handle.writer is None
    assert handle.queue.current_cost == frames.FrameCost(
        frames=0, encoded_bytes=0
    )
    assert harness.broker.connections == ()
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []

    handle_ref = weakref.ref(handle)
    created.clear()
    del handle
    del raised
    failure.__traceback__ = None
    gc.collect()
    assert handle_ref() is None


def test_the_first_connection_gets_retry_a_reseed_and_the_snapshot() -> None:
    harness = Harness()
    harness.emit_many(3)
    handle = harness.connect()
    items = drain_connection(handle)
    assert items[0].wire_bytes() == b"retry: 1000\n\n"
    assert items[1].wire_bytes() == b"id: inst-test:3\n\n"
    # No gap: a first connection is not a degradation.
    assert not any(b"replay_gap" in item.wire_bytes() for item in items)
    assert any(b"event: state_patch" in item.wire_bytes() for item in items)


def test_reconnect_replays_exactly_the_missing_tail() -> None:
    harness = Harness()
    harness.emit_many(5)
    handle = harness.connect(cursor=cursor_for(2))
    items = drain_connection(handle)
    assert replay_ids(items) == [3, 4, 5]
    # Re-seeded at the cursor so an id-less frame cannot erase it.
    assert items[1].wire_bytes() == b"id: inst-test:2\n\n"
    assert not any(b"replay_gap" in item.wire_bytes() for item in items)


def test_reconnect_never_duplicates_and_never_leaves_a_hole() -> None:
    harness = Harness()
    harness.emit_many(6)
    # Each connection resumes from *its own* cursor, so the acceptance is
    # per connection: exactly the missing tail, in order, once. Accumulating
    # across different cursors would ask for the union of three overlapping
    # tails, which is not a property any connection is promised.
    for cursor_seq in (1, 3, 5):
        handle = harness.connect(cursor=cursor_for(cursor_seq))
        ids = replay_ids(drain_connection(handle))
        assert ids == list(range(cursor_seq + 1, 7))
        assert len(set(ids)) == len(ids), f"duplicates from {cursor_seq}: {ids}"


def test_a_rolled_out_ring_answers_a_gap_not_a_silent_resume() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
        )
    )
    harness.emit_many(5)
    handle = harness.connect(cursor=cursor_for(1))
    items = drain_connection(handle)
    notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    assert b'"requested_seq":1' in notice.wire_bytes()
    # Nothing was replayed from a ring that no longer holds it.
    assert replay_ids(items) == []


class _ClassifyThenEvictRing(ReplayRing):
    """Pause one real replay read after its cursor verdict is decided."""

    def __init__(self, *, budget: frames.FrameBudget) -> None:
        super().__init__(budget=budget)
        self.classified = threading.Event()
        self.continue_read = threading.Event()
        self.armed = False

    def classify_seq(self, seq):
        verdict = super().classify_seq(seq)
        if self.armed:
            self.armed = False
            self.classified.set()
            self.continue_read.wait()
        return verdict


@pytest.mark.parametrize(
    ("ring_budget", "max_frame_bytes"),
    [
        pytest.param(
            frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
            frames.MAX_FRAME_BYTES,
            id="frame-budget",
        ),
        pytest.param(
            frames.FrameBudget(frames=64, encoded_bytes=1114),
            frames.MAX_FRAME_BYTES,
            id="byte-budget",
        ),
        pytest.param(
            frames.FrameBudget(frames=8, encoded_bytes=1 << 20),
            400,
            id="multi-frame-event",
        ),
    ],
)
def test_replay_eviction_after_cursor_classification_never_sends_a_holey_tail(
    ring_budget: frames.FrameBudget,
    max_frame_bytes: int,
) -> None:
    ring = _ClassifyThenEvictRing(budget=ring_budget)
    harness = Harness(ring=ring, max_frame_bytes=max_frame_bytes)
    harness.emit_many(2)
    handle = harness.connect(cursor=cursor_for(0))
    delivered = []
    completed: list[bool] = []

    ring.armed = True
    writer = threading.Thread(
        target=lambda: completed.append(
            handle.writer.deliver_startup(delivered.append)
        )
    )
    writer.start()
    assert ring.classified.wait(5.0), "writer never classified its cursor"

    harness.emit_many(1, start=2)
    ring.continue_read.set()
    writer.join(5.0)
    assert not writer.is_alive(), "writer did not finish the frozen replay"

    assert completed == [False]
    assert replay_ids(delivered) == []
    assert delivered[-1].wire_bytes() == frames.retry_frame(
        frames.BACKOFF_RETRY_MS
    ).wire_bytes()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)

    reconnect = harness.connect(cursor=cursor_for(0))
    opening = drain_connection(reconnect)
    gap = next(item for item in opening if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in gap.wire_bytes()
    assert replay_ids(opening) == []


def test_commit_does_not_walk_a_large_evicted_replay_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20))
    retired_refs: list[weakref.ReferenceType[PreparedFrames]] = []
    for seq in range(1, 65):
        entry = frames.measured_frames(
            seq=seq,
            frames=(b"data: small",),
            id_line=b"id: inst-test:%d\n" % seq,
            replayable=True,
        )
        ring.append(entry)
        if seq <= 48:
            retired_refs.append(weakref.ref(entry))
    del entry
    harness = Harness(ring=ring)
    payload = RunStarted(purpose="chat")
    prepared = frames.measured_frames(
        frames=tuple(b"data: large" for _ in range(48)),
        sequence_offset=0,
        sequence_field_limit=32,
        replayable=True,
    )
    harness.fanout._seq = 65
    monkeypatch.setattr(harness.broker, "prepare", lambda _event: prepared)
    cost_calls = 0
    original_ingress_cost = PreparedFrames.ingress_cost

    def counted_ingress_cost(item: PreparedFrames) -> frames.FrameCost:
        nonlocal cost_calls
        cost_calls += 1
        return original_ingress_cost(item)

    monkeypatch.setattr(PreparedFrames, "ingress_cost", counted_ingress_cost)
    original_release = ring._entries.release_retired
    released_outside_publish_lock = False

    def checked_release(start: int, count: int, through_seq: int) -> None:
        nonlocal released_outside_publish_lock
        acquired = harness.broker._lock.acquire(blocking=False)
        assert acquired, "references were released under the broker lock"
        fanout_acquired = harness.fanout._lock.acquire(blocking=False)
        assert fanout_acquired, "references were released under the FanOut lock"
        harness.fanout._lock.release()
        harness.broker._lock.release()
        released_outside_publish_lock = True
        original_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", checked_release)

    harness.emit(payload, run_id="r65")

    # One ring admission plus one ingress admission; none per displaced item.
    assert cost_calls == 2
    assert released_outside_publish_lock is True
    assert all(reference() is None for reference in retired_refs)
    retained_cells = [
        cell.entry
        for cell in ring._entries._entries
        if cell is not None and cell.entry is not None
    ]
    assert (
        frames.FrameCost(
            frames=sum(len(entry.frames) for entry in retained_cells),
            encoded_bytes=sum(entry.byte_size for entry in retained_cells),
        )
        == ring.current_cost
    )
    assert [entry.seq for entry in ring.entries_after(48) or ()] == list(range(49, 66))


def test_retirement_cleanup_failure_is_fatal_after_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "retirement-secret-must-not-be-published"
    ring = ReplayRing(budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20))
    harness = Harness(ring=ring)
    capture = CapturingSink(name="capture")
    fanout = FanOutSink(
        [harness.broker, capture], process_instance_id=INSTANCE
    )
    def fail_release(start: int, count: int, through_seq: int) -> None:
        del start, count, through_seq
        raise RuntimeError(secret)

    monkeypatch.setattr(
        ring._entries, "release_retired", fail_release
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    first = fanout.emit(RunStarted(purpose="chat"), envelope)
    committed = fanout.emit(RunStarted(purpose="chat"), envelope)
    fanout.emit(
        RunStarted(purpose="chat"),
        replace(envelope, run_id="r2"),
    )

    assert (first.seq, committed.seq) == (1, 2)
    assert any(event.seq == committed.seq for event in capture.events)
    assert harness.broker._fatal is not None  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1, "process fatal is notified once across Runs"
    assert dict(notices[0].payload.detail) == {
        "sink": harness.broker.name,
        "stage": "post_commit",
    }
    assert secret not in json.dumps(capture.events, default=event_json_default)


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(
            KeyboardInterrupt("offer interrupted before effect"),
            id="KeyboardInterrupt",
        ),
        pytest.param(
            SystemExit("offer interrupted before effect"),
            id="SystemExit",
        ),
    ),
)
def test_ingress_before_effect_control_exit_forces_ordered_replay(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """A ring-committed event cannot become a silent live-stream hole."""
    harness = Harness()
    old = harness.connect(cursor=cursor_for(0))
    drain_connection(old)
    real_offer = harness.broker._ingress.offer  # noqa: SLF001

    def interrupt_before_offer(_item) -> bool:
        raise failure

    monkeypatch.setattr(
        harness.broker._ingress, "offer", interrupt_before_offer  # noqa: SLF001
    )
    with pytest.raises(type(failure)) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure, "the process-control exception changed identity"

    monkeypatch.setattr(harness.broker._ingress, "offer", real_offer)  # noqa: SLF001
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    drain_dispatcher(harness)

    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == [], (
        "the old stream advanced past the ring-committed missing event"
    )
    reconnect = harness.connect(cursor=cursor_for(0))
    assert replay_ids(drain_connection(reconnect)) == [1, 2]
    assert harness.broker._fatal is None  # noqa: SLF001


def test_ingress_after_effect_control_exit_neither_duplicates_nor_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown offer effect is recovered by a generation, not a retry."""
    harness = Harness()
    old = harness.connect(cursor=cursor_for(0))
    drain_connection(old)
    real_offer = harness.broker._ingress.offer  # noqa: SLF001
    failure = KeyboardInterrupt("offer interrupted after effect")

    def interrupt_after_offer(item) -> bool:
        assert real_offer(item) is True
        raise failure

    monkeypatch.setattr(
        harness.broker._ingress, "offer", interrupt_after_offer  # noqa: SLF001
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure

    # Register in the committed generation before the uncertain ingress item
    # is consumed. Its opening replay owns seq 1, so the queued copy must be
    # skipped; the old generation must be closed before it can see any later id.
    reconnect = harness.connect(cursor=cursor_for(0))
    opening = drain_connection(reconnect)
    assert replay_ids(opening) == [1]
    monkeypatch.setattr(harness.broker._ingress, "offer", real_offer)  # noqa: SLF001
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    drain_dispatcher(harness)

    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == []
    assert replay_ids(opening + drain_connection(reconnect)) == [1, 2]
    assert harness.broker._fatal is None  # noqa: SLF001


@pytest.mark.parametrize("effect", ["before", "after"])
def test_ingress_control_exit_remains_authoritative_if_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
) -> None:
    """A failed disconnect fence must fail closed without replacing ingress."""
    harness = Harness()
    old = harness.connect(cursor=cursor_for(0))
    drain_connection(old)
    original = KeyboardInterrupt("ingress interrupted first")
    secondary = SystemExit("disconnect generation interrupted second")

    class InterruptingGeneration(int):
        def __add__(self, increment):
            if effect == "after":
                harness.broker._disconnect_generation = (  # noqa: SLF001
                    int(self) + int(increment)
                )
            raise secondary

    def fail_offer(_item) -> bool:
        raise original

    harness.broker._disconnect_generation = InterruptingGeneration(0)  # noqa: SLF001
    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is original
    assert harness.broker._fatal is original  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001

    with pytest.raises(ProcessFatalSinkError):
        prepared = harness.broker.prepare(
            UnsequencedEvent(
                event_id="later",
                envelope=EventEnvelope(0.0, "r2", None, None, None, None),
                payload=RunStarted(purpose="chat"),
                trace_policy="persist",
                replayable=True,
            )
        )
        harness.broker.commit(
            prepared,
            SequencedEvent(
                seq=2,
                process_instance_id=INSTANCE,
                event_id="later",
                envelope=EventEnvelope(0.0, "r2", None, None, None, None),
                payload=RunStarted(purpose="chat"),
                trace_policy="persist",
                replayable=True,
            ),
        )
    assert harness.broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == []


def test_interrupted_ingress_keeps_retired_cleanup_until_fanout_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker retains and settles ring cleanup outside publication locks."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert harness.broker.deliver_next(timeout=0) is True
    retired = ring.entries_after(0)
    assert retired is not None
    retired_entry = retired[0]
    retired_ref = weakref.ref(retired_entry)
    del retired, retired_entry

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    cleanup_lock_checks: list[tuple[bool, bool]] = []

    def gated_release(start: int, count: int, through_seq: int) -> None:
        broker_unlocked = harness.broker._lock.acquire(blocking=False)  # noqa: SLF001
        if broker_unlocked:
            harness.broker._lock.release()  # noqa: SLF001
        fanout_unlocked = harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
        if fanout_unlocked:
            harness.fanout._lock.release()  # noqa: SLF001
        cleanup_lock_checks.append((broker_unlocked, fanout_unlocked))
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release retired cleanup"
        real_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", gated_release)  # noqa: SLF001
    failure = KeyboardInterrupt("evicting offer interrupted")

    def interrupt_before_offer(_item) -> bool:
        raise failure

    monkeypatch.setattr(
        harness.broker._ingress, "offer", interrupt_before_offer  # noqa: SLF001
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r2")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="interrupted-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "ring cleanup ownership was stranded"
    assert retired_ref() is not None, "cleanup escaped before its owner ran"
    release_cleanup.set()
    publisher.join(3.0)
    assert not publisher.is_alive(), "interrupted publication never settled"

    gc.collect()
    assert caught == [failure]
    assert cleanup_lock_checks == [(True, True)]
    assert retired_ref() is None


def test_interrupted_ingress_keeps_first_control_exit_when_kick_also_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secondary wake-up interruption cannot replace the ingress cause."""
    harness = Harness()
    old = harness.connect(cursor=cursor_for(0))
    drain_connection(old)
    original = KeyboardInterrupt("ingress interrupted first")
    secondary = SystemExit("kick interrupted second")

    def fail_offer(_item) -> bool:
        raise original

    def fail_kick() -> None:
        raise secondary

    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(harness.broker._ingress, "put_kick", fail_kick)  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is original

    # The generation itself is durable wake-up debt. Even with no sentinel,
    # the dispatcher's periodic path closes the old stream on its next check.
    assert harness.broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True


def test_interrupted_ingress_retries_cleanup_after_secondary_control_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup stays broker-owned when its first lock-free attempt exits."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    gated_threads = GatedThreads()
    harness = Harness(ring=ring, spawn=gated_threads)
    harness.broker.start()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert harness.broker.deliver_next(timeout=0) is True
    retained = ring.entries_after(0)
    assert retained is not None
    retired_entry = retained[0]
    retired_ref = weakref.ref(retired_entry)
    del retained, retired_entry

    real_release = ring._entries.release_retired  # noqa: SLF001
    release_attempts = 0
    cleanup_complete = threading.Event()
    secondary = SystemExit("cleanup interrupted second")

    def interrupt_cleanup_once(start: int, count: int, through_seq: int) -> None:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise secondary
        real_release(start, count, through_seq)
        cleanup_complete.set()

    original = KeyboardInterrupt("ingress interrupted first")

    def fail_offer(_item) -> bool:
        raise original

    monkeypatch.setattr(
        ring._entries, "release_retired", interrupt_cleanup_once  # noqa: SLF001
    )
    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert raised.value is original
    assert release_attempts == 1
    assert retired_ref() is not None

    # The first claimant requeued before fatal publication. Let the real
    # dispatcher start only now: its first periodic boundary must claim the
    # same owner exactly once, without a caller retrying publication.
    gated_threads.open("_dispatch_loop")
    assert cleanup_complete.wait(3.0), "dispatcher did not repay cleanup debt"
    gated_threads.by_name["_dispatch_loop"].join(3.0)
    gc.collect()
    assert release_attempts == 2
    assert retired_ref() is None


def test_fatal_ingress_exception_still_releases_ring_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process-fatal ingress failure cannot orphan an earlier ring cell."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert harness.broker.deliver_next(timeout=0) is True
    retained = ring.entries_after(0)
    assert retained is not None
    retired_entry = retained[0]
    retired_ref = weakref.ref(retired_entry)
    del retained, retired_entry

    real_release = ring._entries.release_retired  # noqa: SLF001
    lock_checks: list[tuple[bool, bool]] = []

    def checked_release(start: int, count: int, through_seq: int) -> None:
        broker_unlocked = harness.broker._lock.acquire(blocking=False)  # noqa: SLF001
        if broker_unlocked:
            harness.broker._lock.release()  # noqa: SLF001
        fanout_unlocked = harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
        if fanout_unlocked:
            harness.fanout._lock.release()  # noqa: SLF001
        lock_checks.append((broker_unlocked, fanout_unlocked))
        real_release(start, count, through_seq)

    failure = RuntimeError("ingress failed after ring commit")
    secondary = KeyboardInterrupt("fatal kick interrupted")

    def fail_offer(_item) -> bool:
        raise failure

    def fail_fatal_kick() -> None:
        raise secondary

    monkeypatch.setattr(ring._entries, "release_retired", checked_release)  # noqa: SLF001
    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(harness.broker._ingress, "put_kick", fail_fatal_kick)  # noqa: SLF001

    # The fatal ingress cause remains authoritative even when its wake-up
    # edge encounters a later process-control exception.
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    gc.collect()

    assert harness.broker._fatal is failure  # noqa: SLF001
    assert lock_checks == [(True, True)]
    assert retired_ref() is None


def test_ingress_exception_latches_fatal_before_releasing_the_broker_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No connection can register between failed ingress and its fatal fence."""
    harness = Harness()
    broker = harness.broker
    failure = RuntimeError("ingress failed after ring commit")
    fatal_publish_entered = threading.Event()
    release_fatal_publish = threading.Event()
    publisher_done = threading.Event()
    publisher_failures: list[BaseException] = []
    real_publish_fatal = broker._publish_fatal  # noqa: SLF001

    def fail_offer(_item) -> bool:
        raise failure

    def gated_publish_fatal(
        exc: BaseException, *, wake_dispatcher: bool = True
    ):
        fatal_publish_entered.set()
        assert release_fatal_publish.wait(3.0), "test did not release fatal publish"
        return real_publish_fatal(exc, wake_dispatcher=wake_dispatcher)

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - reported below
            publisher_failures.append(exc)
        finally:
            publisher_done.set()

    monkeypatch.setattr(broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(broker, "_publish_fatal", gated_publish_fatal)
    publisher = threading.Thread(target=publish, name="fatal-ingress-publisher")
    publisher.start()
    assert fatal_publish_entered.wait(3.0), "publisher never left ingress failure"

    try:
        connection = FakeConnection()
        refused = broker.connect(connection=connection)
        assert refused.finished.is_set()
        assert connection.closed is True
        assert refused not in broker.connections
        assert broker.publish_state_patch(runtime_snapshot(state_revision=1)) is False
    finally:
        release_fatal_publish.set()
        publisher.join(3.0)
    assert publisher_done.is_set()
    assert publisher_failures == []
    assert broker._fatal is failure  # noqa: SLF001


def test_later_sibling_control_exit_cannot_skip_broker_ring_cleanup() -> None:
    """A returned broker PostCommit survives a later sink's control exit."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    failure = KeyboardInterrupt("later sibling interrupted")

    class InterruptingSibling(CapturingSink):
        armed = False

        def commit(self, prepared: object, event: SequencedEvent) -> None:
            super().commit(prepared, event)
            if self.armed:
                raise failure

    sibling = InterruptingSibling(name="sibling")
    fanout = FanOutSink(
        [harness.broker, sibling], process_instance_id=INSTANCE
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert harness.broker.deliver_next(timeout=0) is True
    retained = ring.entries_after(0)
    assert retained is not None
    retired_entry = retained[0]
    retired_ref = weakref.ref(retired_entry)
    del retained, retired_entry

    sibling.armed = True
    with pytest.raises(KeyboardInterrupt) as raised:
        fanout.emit(RunStarted(purpose="chat"), envelope)
    assert raised.value is failure
    gc.collect()
    assert retired_ref() is None
    assert harness.broker._fatal is None  # noqa: SLF001

    sibling.armed = False
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert [event.seq for event in sibling.events] == [1, 2, 3]


def test_parallel_interrupted_commits_retain_distinct_cleanup_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pending slot cannot overwrite another publisher's ring cleanup."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    first_ref = weakref.ref(seeded[0])
    del seeded

    failures = {
        "publisher-one": KeyboardInterrupt("first ingress interruption"),
        "publisher-two": SystemExit("second ingress interruption"),
    }

    def fail_offer(_item) -> bool:
        raise failures[threading.current_thread().name]

    real_settle = harness.broker.settle_interrupted_commit
    first_waiting = threading.Event()
    both_waiting = threading.Event()
    release_settlement = threading.Event()
    waiting = 0
    waiting_lock = threading.Lock()

    def gated_settle(failure: BaseException):
        nonlocal waiting
        with waiting_lock:
            waiting += 1
            if waiting == 1:
                first_waiting.set()
            if waiting == 2:
                both_waiting.set()
        assert release_settlement.wait(3.0), "test did not release settlement"
        return real_settle(failure)

    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(
        harness.broker, "settle_interrupted_commit", gated_settle
    )
    caught: dict[str, BaseException] = {}

    def publish(run_id: str) -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id=run_id)
        except BaseException as exc:  # noqa: BLE001 - exact owners asserted
            exc = exc.with_traceback(None)
            caught[threading.current_thread().name] = exc

    first = threading.Thread(
        target=publish, args=("r1",), name="publisher-one"
    )
    first.start()
    assert first_waiting.wait(3.0), "first publisher did not retain cleanup"
    second_entry = ring.entries_after(1)
    assert second_entry is not None
    second_ref = weakref.ref(second_entry[0])
    del second_entry

    second = threading.Thread(
        target=publish, args=("r2",), name="publisher-two"
    )
    second.start()
    assert both_waiting.wait(3.0), "second publisher overwrote pending cleanup"
    assert first_ref() is not None
    assert second_ref() is not None

    release_settlement.set()
    first.join(3.0)
    second.join(3.0)
    assert not first.is_alive() and not second.is_alive()
    gc.collect()

    assert caught == failures
    assert first_ref() is None
    assert second_ref() is None
    assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001


def test_the_four_illegal_cursors_each_name_their_reason() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
        )
    )
    harness.emit_many(4)
    cases = {
        "garbage": "malformed",
        "other-process:2": "instance_mismatch",
        cursor_for(1): "too_old",
        cursor_for(99): "ahead",
    }
    for cursor, reason in cases.items():
        handle = harness.connect(cursor=cursor)
        items = drain_connection(handle)
        notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
        assert f'"gap_reason":"{reason}"' in notice.wire_bytes().decode()


def test_the_gap_notice_distinguishes_no_run_from_unrecoverable_run() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
        )
    )
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit_many(3, start=10)
    # No active Run in the snapshot: absent.
    handle = harness.connect(cursor="garbage")
    assert b'"current_run_state":"absent"' in _wire_containing(handle, b"replay_gap")

    active = runtime_snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=ActiveRunSummary(
            run_id="r1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id=None,
            prompt_preview="hi",
            started_at="2026-01-01T00:00:00Z",
            recording_state=None,
        ),
    )
    harness.broker.publish_state_patch(active)
    handle = harness.connect(cursor="garbage")
    assert b'"current_run_state":"unrecoverable"' in _wire_containing(
        handle, b"replay_gap"
    )


@pytest.mark.parametrize("recording_state", ["pending", "failed"])
@pytest.mark.parametrize(
    ("ring_frames", "expected_state"), [(1, "unrecoverable"), (4, "recoverable")]
)
def test_gap_recovery_boundary_survives_the_run_saving_phase(
    recording_state, ring_frames, expected_state
) -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=ring_frames, encoded_bytes=1 << 20)
        )
    )
    active = ActiveRunSummary(
        run_id="r1",
        purpose="chat",
        gateway="web",
        phase="running",
        session_id=None,
        prompt_preview="hi",
        started_at="2026-01-01T00:00:00Z",
        recording_state=None,
    )
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=1, coordinator_state="running", active_run=active
        )
    )
    harness.emit(RunStarted(purpose="chat"))
    harness.emit(StepStarted(step_index=1))
    expected = f'"current_run_state":"{expected_state}"'.encode()
    assert expected in _wire_containing(
        harness.connect(cursor="garbage"), b"replay_gap"
    )

    harness.emit(RunFinished(outcome="completed"))
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=2,
            coordinator_state=f"recording_{recording_state}",
            unrecorded_terminal_projection=UnrecordedTerminalProjection(
                run_id="r1",
                purpose="chat",
                outcome="completed",
                reply_text="done",
                error=None,
                recording_state=recording_state,
                session_id=None,
                prompt_preview="hi",
            ),
            active_run=replace(
                active,
                phase="finished",
                outcome="completed",
                recording_state=recording_state,
            ),
        )
    )
    assert expected in _wire_containing(
        harness.connect(cursor="garbage"), b"replay_gap"
    )

    harness.broker.publish_state_patch(runtime_snapshot(state_revision=3))
    assert b'"current_run_state":"absent"' in _wire_containing(
        harness.connect(cursor="garbage"), b"replay_gap"
    )
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=4,
            coordinator_state="running",
            active_run=replace(active, run_id="r2"),
        )
    )
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert b'"current_run_state":"recoverable"' in _wire_containing(
        harness.connect(cursor="garbage"), b"replay_gap"
    )


def _wire_containing(handle, needle: bytes) -> bytes:
    for item in drain_connection(handle):
        if needle in item.wire_bytes():
            return item.wire_bytes()
    raise AssertionError(f"no frame containing {needle!r}")


def _decode_patch(wire: bytes) -> dict:
    """The state_patch document exactly as it crossed the wire."""
    body = b"".join(
        line[len(b"data: ") :]
        for line in wire.split(b"\n")
        if line.startswith(b"data: ")
    )
    return json.loads(body.decode("utf-8"))


def _patch_payload(handle) -> dict:
    return _decode_patch(_wire_containing(handle, b"event: state_patch"))


def test_the_snapshot_names_the_unrecorded_terminal_projection_bounded() -> None:
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=4,
            coordinator_state="recording_pending",
            active_run=ActiveRunSummary(
                run_id="r1",
                purpose="chat",
                gateway="web",
                phase="finished",
                outcome="completed",
                session_id="s1",
                prompt_preview="hi",
                started_at="2026-01-01T00:00:00Z",
                recording_state="pending",
            ),
            unrecorded_terminal_projection=UnrecordedTerminalProjection(
                run_id="r1",
                purpose="chat",
                outcome="completed",
                reply_text="y" * 5000,
                error=None,
                recording_state="pending",
                session_id="s1",
                prompt_preview="hi",
            ),
        )
    )
    handle = harness.connect(session_id="s1")
    wire = _wire_containing(handle, b"event: state_patch")
    payload = _decode_patch(wire)
    projection = payload["unrecorded_terminal_projection"]
    assert payload["recording_state"] == "pending"
    assert payload["session_valid"] is True
    assert projection["run_id"] == "r1"
    assert projection["outcome"] == "completed"
    # Bounded, and the cut is marked rather than silent: the snapshot is the
    # one place the full reply text could escape to a browser.
    preview = projection["reply_preview"]
    assert len(preview) == SNAPSHOT_TEXT_LIMIT
    assert preview.endswith("…")
    assert preview.startswith("y" * 100)
    assert len(wire) < frames.MAX_FRAME_BYTES


def test_session_validity_is_this_connections_own_fact() -> None:
    harness = Harness()
    harness.connect(session_id="gone")
    handle = harness.connect(session_id="gone")
    patch = _wire_containing(handle, b"event: state_patch")
    assert b'"session_valid":false' in patch


# --- the terminal Step summary survives the settlement window ---------------


def _terminal_stream(harness: Harness, run_id: str) -> None:
    """One Step's real event vocabulary, through the broker's own commit."""
    harness.emit(RunStarted(purpose="chat"), run_id=run_id)
    harness.emit(StepStarted(step_index=2), run_id=run_id)
    harness.emit(
        AttemptCommitted(attempt_id="a1", stop_reason="end_turn", duration_ms=5),
        run_id=run_id,
    )
    harness.emit(RunFinished(outcome="completed"), run_id=run_id)


def _recording_pending_snapshot(**kwargs) -> RuntimeSnapshot:
    return runtime_snapshot(
        state_revision=1,
        coordinator_state="recording_pending",
        active_run=ActiveRunSummary(
            run_id="r1",
            purpose="chat",
            gateway="web",
            phase="finished",
            session_id="s1",
            prompt_preview="hi",
            started_at="2026-01-01T00:00:00Z",
            recording_state="pending",
            current_step=2,
            outcome="completed",
        ),
        unrecorded_terminal_projection=UnrecordedTerminalProjection(
            run_id="r1",
            purpose="chat",
            outcome="completed",
            reply_text="pong",
            error=None,
            recording_state="pending",
            session_id="s1",
            prompt_preview="hi",
        ),
        **kwargs,
    )


def test_run_finished_freezes_the_last_step_until_the_next_run_started() -> None:
    """``run.finished`` freezes the view; it does not erase it.

    The admission lease is held until the recording settles (ADR-0026), and
    in that window this view is the only in-process record of what the Run
    produced -- a same-process reconnect reads exactly it. The summary stops
    when the state moves past the Run: the next ``run.started`` replaces it.
    """
    progress = RunProgress()
    progress.note_run_started("r1")
    progress.note_step_started("r1")
    progress.note_attempt_terminal(
        "r1",
        attempt_id="a1",
        outcome="committed",
        stop_reason="end_turn",
        error_code=None,
        duration_ms=5,
    )
    # run.finished: the reply exists, the recording transaction does not yet.
    progress.note_run_finished("r1")
    frozen = progress.projection(2)
    assert frozen == StepProjection(
        step_index=2,
        attempts=(
            AttemptTerminal(
                attempt_id="a1",
                outcome="committed",
                stop_reason="end_turn",
                error_code=None,
                duration_ms=5,
            ),
        ),
    )
    # The next Run's run.started replaces the frozen summary, wholesale.
    progress.note_run_started("r2")
    assert progress.projection(None) is None
    progress.note_step_started("r2")
    assert progress.projection(0) == StepProjection(step_index=0, attempts=())


def test_the_startup_patch_keeps_the_frozen_summary_while_authoritative() -> None:
    """Same-process reconnect during the settlement window.

    The pending patch is authoritative, its active Run is the finished one,
    so the opening stream must carry the frozen terminal summary -- the last
    StepProjection and its Attempt terminals, exactly as the event stream
    produced them. After recorded→idle (or a new active Run) the old Run's
    summary is cleaned: it may not masquerade as the new state's progress.
    """
    harness = Harness()
    _terminal_stream(harness, "r1")
    harness.broker.publish_state_patch(_recording_pending_snapshot())
    handle = harness.connect(session_id="s1")
    step = _patch_payload(handle)["step"]
    assert step == {
        "step_index": 2,
        "attempts": [
            {
                "attempt_id": "a1",
                "outcome": "committed",
                "stop_reason": "end_turn",
                "error_code": None,
                "duration_ms": 5,
            }
        ],
        "attempts_truncated": False,
    }

    # The lease released: idle, and the old Run's summary goes with it.
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=2))
    idle = _patch_payload(harness.connect(session_id="s1"))
    assert idle["active_run"] is None
    assert idle["step"] is None

    # A new Run admitted: the old Run's frozen summary must not show under it.
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=3,
            coordinator_state="running",
            active_run=ActiveRunSummary(
                run_id="r2",
                purpose="chat",
                gateway="web",
                phase="running",
                session_id="s1",
                prompt_preview="next",
                started_at="2026-01-01T00:00:00Z",
                recording_state=None,
            ),
        )
    )
    next_run = _patch_payload(harness.connect(session_id="s1"))
    assert next_run["active_run"]["run_id"] == "r2"
    assert next_run["step"] is None


# --- backpressure ----------------------------------------------------------


def test_a_connection_that_never_consumes_does_not_block_the_run_or_others() -> None:
    harness = Harness()
    # One tab that never reads a single frame from its queue, with a queue
    # small enough to actually fill; the other one keeps up. Backpressure is
    # per connection, so only the first is allowed to suffer for it.
    slow = harness.connect(budget=frames.FrameBudget(8, 8 * 1024 * 1024))
    other = harness.connect(budget=frames.FrameBudget(512, 8 * 1024 * 1024))
    started = time.monotonic()
    for _ in range(80):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    elapsed = time.monotonic() - started
    # Emitting is O(1) in connections: a wedged tab costs it nothing.
    assert elapsed < 2.0
    other_ids = replay_ids(drain_connection(other))
    assert other_ids == list(range(1, 81))
    # The slow one was closed rather than allowed to stall the process.
    assert slow.queue.close_requested is True


def test_dropped_transients_are_reported_once_the_connection_recovers() -> None:
    harness = Harness()
    # The real fixed prefix needs three frame slots before it drains. The live
    # queue then fills those same three slots and drops the fourth transient.
    handle = harness.connect(budget=frames.FrameBudget(3, 8 * 1024 * 1024))
    drain_connection(handle)
    for _ in range(4):
        harness.emit(BlockDelta(text="x"), run_id="r1")
        harness.deliver()
    # Three were accepted, one was dropped. Drain past them until the queue
    # has room again.
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    items = drain_connection(handle)
    notice = next(
        (item for item in items if b"deltas_dropped" in item.wire_bytes()),
        None,
    )
    assert notice is not None
    assert b'"count":1' in notice.wire_bytes()
    # Reported once: the next delivery does not repeat the count.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    assert not any(
        b"deltas_dropped" in item.wire_bytes() for item in drain_connection(handle)
    )


def test_drop_recovery_keeps_the_new_attempt_and_its_following_delta() -> None:
    harness = Harness()
    handle = harness.connect(budget=frames.FrameBudget(3, 8 * 1024 * 1024))
    drain_connection(handle)

    for text in (
        "queued-1",
        "queued-2",
        "queued-3",
        "missed-1",
        "missed-2",
    ):
        harness.emit(BlockDelta(attempt_id="old", text=text), run_id="r1")
        harness.deliver()
    drain_connection(handle)

    harness.emit(
        AttemptStarted(attempt_id="new", streamed=True),
        run_id="r1",
    )
    harness.deliver()
    recovery = drain_connection(handle)
    assert len(recovery) == 2

    state: dict[str, str | None] = {"attempt_id": "old", "text": "stale"}

    def consume(item: PreparedFrames) -> None:
        payload = _decode_patch(item.wire_bytes())
        if payload.get("code") == "deltas_dropped":
            state.update(attempt_id=None, text="")
            return
        event = payload.get("event")
        event_payload = payload.get("payload", {}).get("payload", {})
        if event == "attempt.started":
            state.update(attempt_id=event_payload["attempt_id"], text="")
        elif (
            event == "block.delta"
            and event_payload.get("attempt_id") == state["attempt_id"]
        ):
            state["text"] = str(state["text"]) + event_payload["text"]

    for item in recovery:
        consume(item)
    assert state == {"attempt_id": "new", "text": ""}

    harness.emit(BlockDelta(attempt_id="new", text="kept"), run_id="r1")
    harness.deliver()
    for item in drain_connection(handle):
        consume(item)
    assert state == {"attempt_id": "new", "text": "kept"}


def test_replayable_recovery_that_cannot_fit_closes_and_remains_replayable() -> None:
    harness = Harness(connection_budget=frames.FrameBudget(3, 1 << 20))
    handle = harness.connect()
    drain_connection(handle)

    for text in ("queued-1", "queued-2", "queued-3", "missed"):
        harness.emit(BlockDelta(attempt_id="old", text=text), run_id="r1")
        harness.deliver()
    # Keep two queued physical frames, leaving room for either the notice or
    # the replayable candidate, but not the atomic pair.
    handle.queue.take(timeout=0)

    event = harness.emit(
        AttemptStarted(attempt_id="new", streamed=True),
        run_id="r1",
    )
    harness.deliver()

    assert handle.queue.close_requested is True
    remaining = drain_connection(handle)
    wire = b"".join(
        item.wire_bytes()
        for item in remaining
        if isinstance(item, PreparedFrames)
    )
    assert b"attempt.started" not in wire
    assert b"deltas_dropped" not in wire

    fresh = harness.connect(
        cursor=cursor_for(0),
        budget=frames.FrameBudget(4, 1 << 20),
    )
    assert replay_ids(drain_connection(fresh)) == [event.seq]


def test_ingress_drop_recovery_admits_notice_and_transient_as_one_pair() -> None:
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)

    # This is a complete, measured physical domain-event frame. Its trailing
    # JSON whitespace makes the worked example exactly 101 encoded bytes.
    prefix = b'event: domain_event\ndata: {"event":"attempt.started"}'
    candidate = frames.measured_frames(
        frames=(prefix + b" " * 46,), replayable=False
    )
    candidate_cost = candidate.ingress_cost()
    assert candidate_cost.encoded_bytes == 101

    ingress_dropped = int("9" * 51)
    notice_cost = frames.deltas_dropped_notice(
        ingress_dropped
    ).ingress_cost()
    assert notice_cost.encoded_bytes == 117
    assert notice_cost.encoded_bytes > candidate_cost.encoded_bytes

    # A replay slice already owns part of this connection's central budget.
    # What remains can hold the candidate exactly, but not notice + candidate.
    startup_entry = frames.measured_frames(frames=(b"s",), replayable=True)
    startup_cost = startup_entry.ingress_cost()
    batch = ReplayBatch(
        kind="batch", entries=(startup_entry,), cost=startup_cost
    )
    assert handle.queue.build_and_reserve_startup(lambda _remaining: batch) is batch
    handle.queue.budget = frames.FrameBudget(
        frames=(startup_cost + candidate_cost).frames,
        encoded_bytes=(startup_cost + candidate_cost).encoded_bytes,
    )

    harness.broker._deliver_event(
        handle, _PublishedEvent(1, candidate), ingress_dropped
    )

    # Neither half may appear alone and the ingress debt is not acknowledged.
    with pytest.raises(queue.Empty):
        handle.queue.take(timeout=0)
    assert handle.ingress_seen == 0
    assert handle.queue.current_cost == startup_cost

    # The refused transient is now local debt. Once both debts and the next
    # candidate fit, they collapse into one notice before that candidate.
    handle.queue.release_startup(startup_cost)
    merged_notice = frames.deltas_dropped_notice(ingress_dropped + 1)
    combined = merged_notice.ingress_cost() + candidate.ingress_cost()
    handle.queue.budget = frames.FrameBudget(
        frames=combined.frames,
        encoded_bytes=combined.encoded_bytes,
    )
    harness.broker._deliver_event(
        handle, _PublishedEvent(2, candidate), ingress_dropped
    )

    notice = handle.queue.take(timeout=0)
    recovered = handle.queue.take(timeout=0)
    assert b'"code":"deltas_dropped"' in notice.wire_bytes()
    assert f'"count":{ingress_dropped + 1}'.encode() in notice.wire_bytes()
    assert recovered is candidate
    assert handle.ingress_seen == ingress_dropped


def test_ingress_drop_recovery_closes_before_admitting_replayable_alone() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    drain_connection(handle)

    # Establish one unit of ingress debt, then retire the older admitted
    # transient. The replayable candidate published afterwards is the first
    # item whose boundary may carry that debt.
    harness.emit(BlockDelta(attempt_id="old", text="queued"), run_id="r1")
    harness.emit(BlockDelta(attempt_id="missed", text="x"), run_id="r1")
    assert harness.broker._ingress_dropped == 1
    harness.deliver()
    drain_connection(handle)

    event = harness.emit(
        AttemptStarted(attempt_id="new", streamed=True), run_id="r1"
    )
    candidate = harness.broker.prepare(event)
    assert isinstance(candidate, PreparedFrames)
    checkpointed = candidate.with_checkpoint(event.seq, INSTANCE)
    candidate_cost = checkpointed.ingress_cost()
    handle.queue.budget = frames.FrameBudget(
        frames=candidate_cost.frames,
        encoded_bytes=candidate_cost.encoded_bytes,
    )

    harness.deliver()

    assert handle.queue.close_requested is True
    assert handle.ingress_seen == 0
    remaining = drain_connection(handle)
    wire = b"".join(
        item.wire_bytes()
        for item in remaining
        if isinstance(item, PreparedFrames)
    )
    assert b"attempt.started" not in wire
    assert b"deltas_dropped" not in wire

    fresh = harness.connect(
        cursor=cursor_for(0), budget=frames.FrameBudget(4, 1 << 20)
    )
    assert replay_ids(drain_connection(fresh)) == [event.seq]


def test_a_transient_missed_by_the_ingress_costs_liveness_only() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    # The first transient occupies the ingress; the second finds it full.
    harness.emit(BlockDelta(text="x"), run_id="r1")
    harness.emit(BlockDelta(text="y"), run_id="r1")
    # Not queued, and nothing was closed: transients are presentation.
    assert handle.queue.close_requested is False
    assert harness.broker._ingress_dropped == 1
    # The next replayable event carries the notice for the missed transient.
    harness.deliver()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    assert any(
        b"deltas_dropped" in item.wire_bytes() for item in drain_connection(handle)
    )


def test_ingress_drop_debt_stays_behind_older_admitted_events() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    drain_connection(handle)

    # The older replayable event is already admitted when this later
    # transient overflows. Its debt must not travel backwards across that
    # publication boundary: doing so would make a client discard an attempt
    # that was never part of the loss interval.
    harness.emit(
        AttemptStarted(attempt_id="old", streamed=True),
        run_id="r1",
    )
    harness.emit(
        BlockDelta(attempt_id="old", text="missed"),
        run_id="r1",
    )
    assert harness.broker._ingress_dropped == 1

    harness.deliver()
    harness.emit(
        AttemptStarted(attempt_id="new", streamed=True),
        run_id="r1",
    )
    harness.deliver()

    payloads = [
        _decode_patch(item.wire_bytes()) for item in drain_connection(handle)
    ]
    assert [payload.get("event") or payload.get("code") for payload in payloads] == [
        "attempt.started",
        "deltas_dropped",
        "attempt.started",
    ]
    assert payloads[0]["payload"]["payload"]["attempt_id"] == "old"
    assert payloads[1]["count"] == 1
    assert payloads[2]["payload"]["payload"]["attempt_id"] == "new"


def test_a_replayable_that_misses_the_ingress_closes_live_connections() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    # One replayable event occupies the ingress; the next cannot fit.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    event = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    # The fact survived in the ring, which is the whole point of ordering
    # ring-before-ingress.
    assert harness.broker._ring.high_water_seq() == event.seq
    # The overflow raised the disconnect generation; the dispatcher does the
    # closing, outside the publish path.
    harness.deliver()
    assert handle.queue.close_requested is True
    closing = drain_connection(handle)
    assert closing[-1] == CloseConnection(retry_ms=frames.BACKOFF_RETRY_MS)
    # A reconnect with the cursor recovers it exactly.
    fresh = harness.connect(cursor=cursor_for(event.seq - 1))
    assert replay_ids(drain_connection(fresh)) == [event.seq]


def test_shared_ingress_overflow_writes_backoff_before_disconnect() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    connection = FakeConnection()
    handle = harness.connect(connection=connection)

    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    harness.deliver()

    # Synchronously drive the production writer target: no scheduler timing is
    # evidence, while the exact bytes and final close are.
    harness.spawn.targets[-1]()

    assert connection.written.endswith(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
    )
    assert connection.closed is True
    assert handle.finished.is_set()


def test_a_transport_notice_owns_no_seq_and_never_enters_the_ring() -> None:
    harness = Harness()
    harness.emit_many(2)
    before = harness.broker._ring.high_water_seq()
    handle = harness.connect(cursor="garbage")
    items = drain_connection(handle)
    notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
    assert b"\nid: " not in notice.wire_bytes()
    assert harness.broker._ring.high_water_seq() == before


# --- the opening stream is the writer's, never the queue's ------------------


def test_a_replay_tail_over_the_connection_budget_still_arrives_whole() -> None:
    """The opening sequence must not buy its way past the queue's budgets.

    A reconnecting client's exact replay tail can be larger than the
    connection's own frame budget; priming it into the queue made the queue
    hold more than it was ever allowed to the moment it existed. A bounded
    writer cursor still delivers the tail whole -- in order, with no hole --
    while every individual batch stays within the connection budget.
    """
    harness = Harness(
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    # The writer-owned fixed prefix is held by reference and fully charged
    # before the writer begins; the replay tail itself remains cursor-bounded.
    assert handle.queue.current_frames == 3
    assert handle.queue.current_bytes > 0
    assert handle.queue.budget.fits(handle.queue.current_cost)
    # retry -> re-seed -> snapshot -> the exact tail, in order.
    opening = drain_connection(handle)
    assert opening[0].wire_bytes() == b"retry: 1000\n\n"
    assert opening[1].wire_bytes() == b"id: inst-test:0\n\n"
    assert any(b"event: state_patch" in frame for frame in opening[2].frames)
    assert replay_ids(opening) == list(range(1, 21))
    # The batches use the ring's immutable frames by reference -- not a
    # second, re-encoded copy of them. The deterministic writer seam has
    # already released each batch before returning this observation.
    stored = harness.broker._ring.entries_after(0)
    assert len(opening) == 3 + len(stored)
    assert all(a is b for a, b in zip(opening[3:], stored))


def test_replay_guard_must_fit_before_connection_registration() -> None:
    """A frozen record without priority capacity refuses the whole stream."""
    harness = Harness()
    event = harness.emit(RunStarted(purpose="chat"), run_id="frozen")
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored) == 1
    record = stored[0]
    assert record.seq == event.seq and len(record.frames) == 1
    record_cost = record.ingress_cost()
    prefix = (
        frames.retry_frame(frames.DEFAULT_RETRY_MS),
        frames.reseed_frame(INSTANCE, 0),
        _patch_frames(runtime_snapshot(), None, True),
    )
    prefix_cost = frames.FrameCost(
        frames=sum(item.ingress_cost().frames for item in prefix),
        encoded_bytes=sum(item.ingress_cost().encoded_bytes for item in prefix),
    )
    refusing_budget = frames.FrameBudget(
        frames=prefix_cost.frames + record_cost.frames,
        encoded_bytes=prefix_cost.encoded_bytes + record_cost.encoded_bytes - 1,
    )
    connection = FakeConnection()

    refused = harness.connect(
        connection=connection,
        cursor=cursor_for(0),
        budget=refusing_budget,
    )

    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused.writer is None
    assert refused not in harness.broker.connections
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []
    assert connection.closed is True
    assert connection.written == b""
    assert refused.queue.current_cost == frames.FrameCost(0, 0)
    assert refused.queue.close_requested is False

    # Filling the entire queue proves abort left neither usage nor a hidden
    # startup priority guard behind on the returned, unregistered handle.
    full_budget_probe = frames.measured_frames(
        frames=(b"x" * (refusing_budget.encoded_bytes - 2),)
    )
    assert full_budget_probe.ingress_cost() == frames.FrameCost(
        frames=1,
        encoded_bytes=refusing_budget.encoded_bytes,
    )
    assert refused.queue.offer(full_budget_probe).kind == "accepted"
    assert refused.queue.take(0) is full_budget_probe
    assert refused.queue.current_cost == frames.FrameCost(0, 0)

    healthy = harness.connect(
        cursor=cursor_for(0),
        budget=frames.FrameBudget(
            frames=prefix_cost.frames + record_cost.frames,
            encoded_bytes=prefix_cost.encoded_bytes + record_cost.encoded_bytes,
        ),
    )
    assert healthy in harness.broker.connections
    assert healthy.writer is not None
    assert replay_ids(drain_connection(healthy)) == [event.seq]


def test_fixed_startup_prefix_must_fit_before_connection_registration() -> None:
    """Retry, re-seed and snapshot are all admitted before registration."""
    harness = Harness()
    connection = FakeConnection()

    refused = harness.connect(
        connection=connection,
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
    )

    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused.writer is None
    assert refused not in harness.broker.connections
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []
    assert connection.closed is True
    assert connection.written == b""
    assert refused.queue.current_cost == frames.FrameCost(0, 0)
    assert refused.queue.close_requested is False


def test_startup_replay_uses_the_capacity_left_by_queued_live_traffic() -> None:
    """A frozen event makes progress in smaller slices instead of backing off."""
    harness = Harness(
        connection_budget=frames.FrameBudget(5, 1 << 20),
        max_frame_bytes=384,
    )
    event = harness.emit(RunStarted(purpose="x" * 500), run_id="r1")
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) > 4

    handle = harness.connect(cursor=cursor_for(0))
    live = frames.measured_frames(
        seq=event.seq + 1,
        frames=(b"data: live",),
    )
    assert handle.queue.offer(live).kind == "accepted"

    opening = drain_connection(handle)
    replay = [
        item
        for item in opening
        if isinstance(item, PreparedFrames) and item.seq == event.seq
    ]
    assert len(replay) >= 2
    assert all(1 <= len(item.frames) <= 4 for item in replay)
    assert all(item.id_line == b"" for item in replay[:-1])
    assert replay[-1].id_line == stored[0].id_line
    assert b"".join(item.wire_bytes() for item in replay) == stored[0].wire_bytes()
    assert opening[-1] is live
    assert all(item.wire_bytes() != b"retry: 3000\n\n" for item in opening)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_live_offers_cannot_steal_capacity_between_startup_slices() -> None:
    """Release-to-fetch handoff keeps one next record protected by bytes."""
    fixed_prefix_bytes = sum(
        item.ingress_cost().encoded_bytes
        for item in (
            frames.retry_frame(frames.DEFAULT_RETRY_MS),
            frames.reseed_frame(INSTANCE, 0),
            _patch_frames(runtime_snapshot(), None, True),
        )
    )
    budget = frames.FrameBudget(7, fixed_prefix_bytes + 650)
    harness = Harness(connection_budget=budget, max_frame_bytes=448)
    event = harness.emit(RunStarted(purpose="x" * 80), run_id="r1")
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) == 3
    handle = harness.connect(cursor=cursor_for(0))

    first_live = frames.measured_frames(frames=(b"a" * 118,))
    inserted = tuple(
        frames.measured_frames(frames=(bytes([98 + index]) * 48,))
        for index in range(2)
    )
    assert handle.queue.offer(first_live).kind == "accepted"

    original_release = handle.queue.release_startup
    releases = 0
    outcomes: list[str] = []

    def release_then_offer(cost: frames.FrameCost) -> None:
        nonlocal releases
        original_release(cost)
        if releases < len(inserted):
            # This is the exact old race window: the current slice is gone,
            # but the next fetch has not started. No sleep or scheduler luck.
            outcomes.append(handle.queue.offer(inserted[releases]).kind)
        releases += 1

    handle.queue.release_startup = release_then_offer  # type: ignore[method-assign]
    replay: list[PreparedFrames] = []
    peak = frames.FrameCost(0, 0)

    def observe(item: PreparedFrames) -> None:
        nonlocal peak
        if item.seq == event.seq:
            replay.append(item)
        current = handle.queue.current_cost
        assert budget.fits(current)
        peak = frames.FrameCost(
            max(peak.frames, current.frames),
            max(peak.encoded_bytes, current.encoded_bytes),
        )

    assert handle.writer is not None
    assert handle.writer.deliver_startup(observe) is True
    assert outcomes == ["accepted", "accepted"]
    assert [len(item.frames) for item in replay] == [2, 1]
    assert [item.id_line for item in replay] == [b"", stored[0].id_line]
    assert budget.fits(peak)
    assert [handle.queue.take(0) for _ in range(3)] == [
        first_live,
        *inserted,
    ]
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_startup_finishes_its_partial_checkpoint_before_overflow_backoff() -> None:
    """A live overflow cannot strand a frozen logical event mid-checkpoint."""
    harness = Harness(
        connection_budget=frames.FrameBudget(5, 1 << 20),
        max_frame_bytes=448,
    )
    frozen = harness.emit(RunStarted(purpose="x" * 75), run_id="frozen")
    harness.deliver()
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) == 3

    class OverflowOnFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.handle = None
            self.live = None
            self.triggered = False

        def write(self, data: bytes) -> None:
            if b'"chunk_index":0' in data and not self.triggered:
                self.triggered = True
                self.live = harness.emit(
                    RunStarted(purpose="after-overflow"), run_id="live"
                )
                harness.deliver()
                assert self.handle is not None
                assert self.handle.queue.close_requested is True
            super().write(data)

    connection = OverflowOnFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))
    connection.handle = handle
    queued_live = frames.measured_frames(frames=(b"data: queued-live",))
    assert handle.queue.offer(queued_live).kind == "accepted"

    # Drive the production _run_writer target synchronously: deterministic
    # socket callback, real unregister/final cleanup, no scheduling race.
    harness.spawn.targets[-1]()

    assert connection.triggered is True
    assert connection.live is not None
    frozen_wire = stored[0].wire_bytes()
    assert frozen_wire in connection.written
    assert connection.written.count(stored[0].id_line) == 1
    assert connection.written.endswith(b"retry: 3000\n\n")
    assert queued_live.wire_bytes() not in connection.written
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert handle.finished.is_set()

    fresh = harness.connect(cursor=cursor_for(frozen.seq))
    assert replay_ids(drain_connection(fresh)) == [connection.live.seq]


def test_the_no_thread_writer_releases_each_observed_startup_batch() -> None:
    """An observer never turns the writer back into a full-tail owner."""

    source = ConnectionQueue(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )

    class TwoBatchReplay:
        def __init__(self) -> None:
            self.next_seq = 1
            self.first_batch_refs: list = []
            self.released: list[frames.FrameCost] = []

        def take(self) -> ReplayBatch:
            if self.next_seq > 4:
                return ReplayBatch(kind="complete")
            if self.next_seq == 3:
                gc.collect()
                assert all(ref() is None for ref in self.first_batch_refs)
            entries = tuple(
                frames.measured_frames(
                    seq=seq,
                    frames=(f"data: {seq}".encode(),),
                    id_line=f"id: inst-test:{seq}\n".encode(),
                    replayable=True,
                )
                for seq in range(self.next_seq, self.next_seq + 2)
            )
            if self.next_seq == 1:
                self.first_batch_refs = [weakref.ref(item) for item in entries]
            self.next_seq += 2
            cost = frames.FrameCost(
                frames=sum(item.ingress_cost().frames for item in entries),
                encoded_bytes=sum(
                    item.ingress_cost().encoded_bytes for item in entries
                ),
            )
            batch = ReplayBatch(kind="batch", entries=entries, cost=cost)
            return source.build_and_reserve_startup(lambda _remaining: batch)

        def release(self, batch: ReplayBatch) -> None:
            source.release_startup(batch.cost)
            self.released.append(batch.cost)

        def cancel(self, batch: ReplayBatch) -> None:
            source.release_startup(batch.cost)

    replay = TwoBatchReplay()
    writer = ConnectionWriter(
        connection=FakeConnection(),
        source=source,
        clock=FakeClock(),
        startup_replay=replay,
    )
    observed: list[int] = []
    peak_cost = frames.FrameCost(frames=0, encoded_bytes=0)

    def observe(item: PreparedFrames) -> None:
        nonlocal peak_cost
        assert item.seq is not None
        observed.append(item.seq)
        cost = source.current_cost
        peak_cost = frames.FrameCost(
            frames=max(peak_cost.frames, cost.frames),
            encoded_bytes=max(peak_cost.encoded_bytes, cost.encoded_bytes),
        )

    assert writer.deliver_startup(observe) is True
    assert observed == [1, 2, 3, 4]
    assert len(replay.released) == 2
    assert source.budget.fits(peak_cost)
    assert source.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)


def test_a_blocked_startup_replay_never_holds_more_than_its_frame_budget() -> None:
    """A slow writer owns only one bounded replay batch at a time."""

    class BlockFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release replay write"
            super().write(data)

    budget = frames.FrameBudget(frames=4, encoded_bytes=2_000)
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)
        ),
        connection_budget=budget,
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(20)
    assert harness.broker._ring.current_cost.frames > budget.frames
    assert harness.broker._ring.current_cost.encoded_bytes > budget.encoded_bytes
    connection = BlockFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))

    try:
        assert connection.blocked.wait(2.0), "writer never reached replay"
        assert budget.fits(handle.queue.current_cost)
        assert 0 < handle.queue.current_cost.frames <= budget.frames
        assert not hasattr(handle, "startup")
    finally:
        connection.release.set()
        handle.queue.stop()
        assert handle.finished.wait(2.0)
        assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_startup_replay_survives_retired_cell_cleanup_after_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight ring reader owns the cell it already observed.

    Publication may retire that cell and finish the linear cleanup after the
    reader has loaded it but before the reader inspects its entry.  The stale
    generation must become an ordinary unavailable replay, never an assertion
    that poisons the broker.
    """
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit_many(2)
    handle = harness.connect(cursor=cursor_for(0))
    assert handle.writer is not None

    captured = threading.Event()
    resume = threading.Event()
    cleanup_complete = threading.Event()
    reader: threading.Thread | None = None
    retired_slot = ring._entries._head

    class PauseAfterCellCapture(list):
        def __getitem__(self, index):
            cell = super().__getitem__(index)
            if (
                threading.current_thread() is reader
                and index == retired_slot
                and not captured.is_set()
            ):
                captured.set()
                assert resume.wait(2.0), "test did not resume the replay reader"
            return cell

    ring._entries._entries = PauseAfterCellCapture(ring._entries._entries)
    original_release = ring._entries.release_retired

    def observed_release(start: int, count: int, through_seq: int) -> None:
        original_release(start, count, through_seq)
        cleanup_complete.set()

    monkeypatch.setattr(ring._entries, "release_retired", observed_release)
    outcomes: list[bool] = []
    failures: list[BaseException] = []

    def read_startup() -> None:
        try:
            outcomes.append(handle.writer.deliver_startup(lambda _item: None))
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read_startup, name="startup-replay-reader")
    reader.start()
    try:
        assert captured.wait(2.0), "reader never captured the soon-retired cell"
        harness.emit(RunStarted(purpose="chat"), run_id="evicting-run")
        assert cleanup_complete.is_set(), "publication did not finish cell cleanup"
    finally:
        resume.set()
        reader.join(2.0)

    assert not reader.is_alive()
    assert failures == []
    assert outcomes == [False]
    assert harness.broker._fatal is None
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_startup_replay_treats_size_drift_during_index_lookup_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary-search index computed from an old ring size is not fatal."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=2048)
    )
    harness = Harness(ring=ring)
    harness.emit_many(2)
    assert len(ring) == 2
    handle = harness.connect(cursor=cursor_for(0))
    assert handle.writer is not None

    indexed = threading.Event()
    resume = threading.Event()
    reader: threading.Thread | None = None
    entries_type = type(ring._entries)
    original_getitem = entries_type.__getitem__

    def pause_before_revalidating_size(entries, index):
        if threading.current_thread() is reader and not indexed.is_set():
            indexed.set()
            assert resume.wait(2.0), "test did not resume the replay reader"
        return original_getitem(entries, index)

    monkeypatch.setattr(
        entries_type,
        "__getitem__",
        pause_before_revalidating_size,
    )
    outcomes: list[bool] = []
    failures: list[BaseException] = []
    delivered: list[PreparedFrames] = []

    def read_startup() -> None:
        try:
            outcomes.append(handle.writer.deliver_startup(delivered.append))
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read_startup, name="startup-replay-reader")
    reader.start()
    try:
        assert indexed.wait(2.0), (
            "reader never reached its old-size index: "
            f"outcomes={outcomes!r}, failures={failures!r}"
        )
        harness.emit(
            RunStarted(
                purpose="chat", user_message=user_message_with("x" * 4000)
            ),
            run_id="oversized-run",
        )
        assert len(ring) == 0, "oversized replayable event did not clear the ring"
    finally:
        resume.set()
        reader.join(2.0)

    assert not reader.is_alive()
    assert failures == []
    assert outcomes == [False]
    assert delivered[-1].wire_bytes() == frames.retry_frame(
        frames.BACKOFF_RETRY_MS
    ).wire_bytes()
    assert harness.broker._fatal is None
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_same_generation_replay_structure_failure_remains_process_fatal() -> None:
    """Only a generation move can turn a structural fault into reconnect."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit_many(2)
    handle = harness.connect(cursor=cursor_for(0))
    assert handle.writer is not None

    ring._entries._entries[ring._entries._head] = None

    with pytest.raises(RuntimeError, match="replay slot was retired") as raised:
        handle.writer.deliver_startup(lambda _item: None)

    assert harness.broker._fatal is raised.value


def test_blocked_fixed_prefix_preserves_replay_guard_and_live_budget() -> None:
    """The real broker charges its prefix before the first socket write."""

    class BlockFirstPrefix(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes) -> None:
            if not self.blocked.is_set():
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release prefix write"
            super().write(data)

    budget = frames.FrameBudget(frames=5, encoded_bytes=1 << 20)
    harness = Harness(connection_budget=budget, spawn=RealThreadSpawner())
    harness.emit(RunStarted(purpose="chat"), run_id="frozen")
    connection = BlockFirstPrefix()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))

    try:
        assert connection.blocked.wait(2.0), "writer never reached fixed prefix"
        assert handle.queue.current_frames == 3
        live = frames.measured_frames(frames=(b"data: live",))
        assert handle.queue.offer(live).kind == "accepted"
        # Prefix 3 + live 1 are actual references; the final frame remains
        # protected for frozen replay, so another live frame is refused.
        assert handle.queue.offer(frames.measured_frames(frames=(b"x",))).kind == (
            "dropped"
        )
        assert handle.queue.current_frames == 4
        assert budget.fits(handle.queue.current_cost)
    finally:
        connection.release.set()
        handle.queue.stop()
        assert handle.finished.wait(2.0)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_a_failed_startup_write_releases_its_reserved_batch() -> None:
    """A dead peer cannot strand startup capacity on the closed handle."""

    class FailFirstReplay(FakeConnection):
        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                raise BrokenPipeError("injected replay write failure")
            super().write(data)

    harness = Harness(
        connection_budget=frames.FrameBudget(frames=5, encoded_bytes=1 << 20),
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(4)
    connection = FailFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))

    assert handle.finished.wait(2.0)
    assert handle.writer is not None
    assert isinstance(handle.writer.failure, BrokenPipeError)
    assert connection.closed is True
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_slow_startup_generations_release_written_and_unfetched_history() -> None:
    """Eviction cannot leave slow writers pinning complete old windows."""

    class BlockFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release replay write"
            super().write(data)

    ring = ReplayRing(
        budget=frames.FrameBudget(frames=6, encoded_bytes=1 << 20)
    )
    harness = Harness(
        ring=ring,
        connection_budget=frames.FrameBudget(frames=5, encoded_bytes=1 << 20),
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(6)
    drain_dispatcher(harness)
    retained = ring.entries_after(0)
    assert retained is not None
    first_ref = weakref.ref(retained[0])
    unfetched_ref = weakref.ref(retained[2])
    del retained

    connections = [BlockFirstReplay() for _ in range(3)]
    handles = [
        harness.connect(connection=connection, cursor=cursor_for(0))
        for connection in connections
    ]
    assert all(connection.blocked.wait(2.0) for connection in connections)

    # Replace the complete old ring while three generations are parked in
    # their first two-entry batch. Entry 3 was never fetched by any writer,
    # so eviction must release it even though all connections remain slow.
    harness.emit_many(6, start=6)
    gc.collect()
    assert first_ref() is not None
    assert unfetched_ref() is None

    for connection in connections:
        connection.release.set()
    for handle in handles:
        assert handle.finished.wait(2.0)
        assert handle.queue.current_cost == frames.FrameCost(0, 0)
    gc.collect()
    assert first_ref() is None
    assert all(connection.closed for connection in connections)
    assert all(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
        in connection.written
        for connection in connections
    )

    # A new connection from the last complete batch boundary observes the
    # gap explicitly and receives a fresh snapshot instead of a silent tail.
    fresh = ObservedConnection()
    reconnected = harness.connect(
        connection=fresh,
        cursor=cursor_for(2),
    )
    fresh.wait_for_bytes(b"replay_gap")
    reconnected.queue.stop()
    assert reconnected.finished.wait(2.0)
    assert b"replay_gap" in fresh.written
    assert b"event: state_patch" in fresh.written


def test_startup_batches_keep_a_chunked_logical_event_whole() -> None:
    """The budget boundary is between logical events, never physical frames."""

    class BlockFirstChunk(FakeConnection):
        def __init__(self, marker: bytes) -> None:
            super().__init__()
            self._marker = marker
            self.blocked = threading.Event()
            self.release = threading.Event()
            self.complete = threading.Event()

        def write(self, data: bytes) -> None:
            if self._marker in data and not self.blocked.is_set():
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release chunk write"
            super().write(data)
            if b"id: inst-test:2\n" in data:
                self.complete.set()

    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)
        ),
        max_frame_bytes=512,
        spawn=RealThreadSpawner(),
    )
    first = harness.emit(RunStarted(purpose="chat"), run_id="first")
    chunky = harness.emit(
        RunStarted(
            purpose="chat",
            user_message=user_message_with("x" * 2_850),
        ),
        run_id="chunky",
    )
    drain_dispatcher(harness)
    stored = harness.broker._ring.entries_after(first.seq)
    assert stored is not None and len(stored) == 1
    event = stored[0]
    assert event.seq == chunky.seq and len(event.frames) > 1
    budget = frames.FrameBudget(
        frames=event.ingress_cost().frames,
        encoded_bytes=event.ingress_cost().encoded_bytes,
    )
    marker = b'"run_id":"chunky"'
    connection = BlockFirstChunk(marker)
    handle = harness.connect(
        connection=connection,
        cursor=cursor_for(first.seq),
        budget=budget,
    )
    try:
        assert connection.blocked.wait(2.0), "writer never reached chunked event"
        assert handle.queue.current_cost == event.ingress_cost()
        connection.release.set()
        assert connection.complete.wait(2.0), "logical event never completed"
        handle.queue.stop()
        assert handle.finished.wait(2.0)
    finally:
        connection.release.set()
    expected = list(event.wire_frames())
    start = connection.writes.index(expected[0])
    assert connection.writes[start : start + len(expected)] == expected


def test_startup_replay_slices_one_large_event_without_advancing_its_id() -> None:
    """A ring-sized event may cross several connection-sized batches."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    previous = frames.measured_frames(
        seq=1,
        frames=(b"data: previous",),
        id_line=b"id: inst-test:1\n",
        replayable=True,
    )
    large = frames.measured_frames(
        seq=2,
        frames=tuple(f"data: part-{index}".encode() for index in range(6)),
        id_line=b"id: inst-test:2\n",
        replayable=True,
    )
    ring.append(previous)
    ring.append(large)
    budget = frames.FrameBudget(frames=5, encoded_bytes=1 << 20)
    harness = Harness(ring=ring, connection_budget=budget)
    handle = harness.connect(cursor=cursor_for(1))
    observed: list[PreparedFrames] = []
    reservation_costs: list[frames.FrameCost] = []

    def observe(item: PreparedFrames) -> None:
        if item.seq == 2:
            observed.append(item)
            reservation_costs.append(handle.queue.current_cost)
            assert budget.fits(handle.queue.current_cost)
            assert len(item.frames) <= budget.frames

    assert handle.writer is not None
    assert handle.writer.deliver_startup(observe) is True

    assert [item.frames for item in observed] == [large.frames[:5], large.frames[5:]]
    assert observed[0].id_line == b""
    assert observed[1].id_line == large.id_line
    assert b"".join(item.wire_bytes() for item in observed) == large.wire_bytes()
    assert all(budget.fits(cost) and cost.frames > 0 for cost in reservation_costs)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert not any(
        item.wire_bytes() == frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
        for item in observed
    )


def test_live_large_event_overflow_reconnects_and_advances_once() -> None:
    """A live overflow is recovered in bounded slices, then stays checkpointed."""
    budget = frames.FrameBudget(frames=5, encoded_bytes=1 << 20)
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
        ),
        connection_budget=budget,
        max_frame_bytes=512,
        spawn=RealThreadSpawner(),
    )
    old_connection = ObservedConnection()
    old = harness.connect(connection=old_connection, cursor=cursor_for(0))
    old_connection.wait_for_bytes(b"event: state_patch")
    event = harness.emit(
        RunStarted(
            purpose="chat",
            user_message=user_message_with("x" * 1_200),
        ),
        run_id="large",
    )
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored) == 1
    assert len(stored[0].frames) > budget.frames
    harness.deliver()

    assert old.finished.wait(2.0)
    assert old_connection.written.endswith(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
    )
    assert old_connection.closed is True

    recovered_connection = ObservedConnection()
    recovered = harness.connect(
        connection=recovered_connection, cursor=cursor_for(0)
    )
    recovered_connection.wait_for_bytes(stored[0].id_line)
    expected = stored[0].wire_bytes()
    start = recovered_connection.written.index(stored[0].wire_frames()[0])
    assert recovered_connection.written[start : start + len(expected)] == expected
    assert recovered_connection.written.count(stored[0].id_line) == 1
    assert not recovered_connection.written.endswith(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
    )
    recovered.queue.stop()
    assert recovered.finished.wait(2.0)

    caught_up_connection = ObservedConnection()
    caught_up = harness.connect(
        connection=caught_up_connection, cursor=cursor_for(event.seq)
    )
    caught_up_connection.wait_for_bytes(b"event: state_patch")
    assert b"event: domain_event" not in caught_up_connection.written
    caught_up.queue.stop()
    assert caught_up.finished.wait(2.0)


def test_eviction_between_large_event_slices_never_issues_its_checkpoint() -> None:
    """Losing a partial event closes now and reports a gap on reconnect."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=7, encoded_bytes=1 << 20)
    )
    previous = frames.measured_frames(
        seq=1,
        frames=(b"data: previous",),
        id_line=b"id: inst-test:1\n",
        replayable=True,
    )
    large = frames.measured_frames(
        seq=2,
        frames=tuple(f"data: part-{index}".encode() for index in range(6)),
        id_line=b"id: inst-test:2\n",
        replayable=True,
    )
    replacement = frames.measured_frames(
        seq=3,
        frames=(b"data: replacement-a", b"data: replacement-b"),
        id_line=b"id: inst-test:3\n",
        replayable=True,
    )
    ring.append(previous)
    ring.append(large)
    harness = Harness(
        ring=ring,
        connection_budget=frames.FrameBudget(frames=5, encoded_bytes=1 << 20),
    )
    handle = harness.connect(cursor=cursor_for(1))
    opening: list[PreparedFrames] = []

    def evict_after_first_slice(item: PreparedFrames) -> None:
        opening.append(item)
        if item.seq == 2 and not item.id_line:
            ring.append(replacement)

    assert handle.writer is not None
    assert handle.writer.deliver_startup(evict_after_first_slice) is False
    assert not any(item.id_line == large.id_line for item in opening)
    assert opening[-1].wire_bytes() == frames.retry_frame(
        frames.BACKOFF_RETRY_MS
    ).wire_bytes()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)

    reconnected = harness.connect(cursor=cursor_for(1))
    next_opening = drain_connection(reconnected)
    gap = next(
        item for item in next_opening if b"replay_gap" in item.wire_bytes()
    )
    assert b'"gap_reason":"too_old"' in gap.wire_bytes()
    assert not any(item.id_line == large.id_line for item in next_opening)


def test_the_queue_budget_holds_during_startup_and_live() -> None:
    """A full ring behind it does not spend a small connection's budget.

    With the tail outside the queue, the first eight live events fit and the
    ninth -- a replayable that cannot fit -- closes the connection under the
    existing overflow rule. Priming the tail into the queue would have
    closed it on the very first live event instead.
    """
    harness = Harness(
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    drain_connection(handle)
    # The tail the opening stream carries makes the ingress backlog redundant
    # for this connection: everything committed before it registered is
    # skipped, so the dispatcher flushes it without the queue noticing.
    drain_dispatcher(harness)
    assert handle.queue.current_frames == 0
    for i in range(8):
        harness.emit(RunStarted(purpose="chat"), run_id=f"live{i}")
        harness.deliver()
    assert handle.queue.close_requested is False
    # The ninth live replayable does not fit: the connection is closed, and
    # a silent hole is never produced in its place.
    harness.emit(RunStarted(purpose="chat"), run_id="live8")
    harness.deliver()
    assert handle.queue.close_requested is True


def test_startup_then_live_has_no_hole_and_reconnect_recovers_exactly() -> None:
    """Line order, integrity, budget and recovery, on a tail over budget."""
    harness = Harness(
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    opening = replay_ids(drain_connection(handle))
    # Flush the pre-registration backlog: the opening tail already covers
    # it, so the dispatcher skips every one of those items.
    drain_dispatcher(harness)
    for i in range(9):
        harness.emit(RunStarted(purpose="chat"), run_id=f"live{i}")
        harness.deliver()
    # What the client receives: the whole tail, then the live events that
    # fit -- every seq exactly once, no gap between the two.
    seen = opening + replay_ids(drain_connection(handle))
    assert seen == list(range(1, 29))
    # Reconnecting from the last delivered cursor recovers exactly the rest.
    fresh = harness.connect(cursor=cursor_for(28))
    assert replay_ids(drain_connection(fresh)) == [29]


# --- chunking and mid-event disconnection ----------------------------------


def test_prepare_is_deterministic_for_one_immutable_logical_event() -> None:
    harness = Harness(max_frame_bytes=16 * 1024)
    unsequenced = _unsequenced_chunky("é" * (200 * 1024), event_id="logical-event-1")

    first = harness.broker.prepare(unsequenced)
    second = harness.broker.prepare(unsequenced)

    assert first == second
    assert first.wire_bytes() == second.wire_bytes()
    assert len(first.frames) > 1
    assert {_chunk_meta(frame)["event_id"] for frame in first.frames} == {
        "logical-event-1"
    }


def test_a_chunked_event_is_replayed_whole_from_its_first_frame() -> None:
    # 16 KiB frames: a 400 KiB UTF-8 body cannot be one physical frame.
    harness = Harness(max_frame_bytes=16 * 1024)
    big = "é" * (200 * 1024)
    # One event ahead of it, so the chunked event has a checkpoint to resume
    # from that this process actually issued.
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    event = harness.emit(
        RunStarted(purpose="chat", user_message=user_message_with(big)),
        run_id="chunky",
    )
    # Preparing an event of the same size really does chunk it: this is the
    # transport fact the replay assertion below depends on.
    assert len(harness.broker.prepare(_unsequenced_chunky(big)).frames) > 1
    # What the broker actually committed is the replay source -- not a
    # re-serialization of it, which would carry a different event id and
    # prove nothing about the bytes a reconnect is sent.
    stored = harness.broker._ring.entries_after(first.seq)
    assert len(stored) == 1
    assert stored[0].seq == event.seq
    assert len(stored[0].frames) > 1
    wire = stored[0].wire_frames()
    # Only the last frame of a logical event advances the cursor.
    assert all(b"\nid: " not in frame for frame in wire[:-1])
    assert wire[-1].endswith(b"id: %s:%d\n\n" % (INSTANCE.encode(), event.seq))
    # Reconnecting from before the event re-sends every chunk, not a tail.
    handle = harness.connect(cursor=cursor_for(first.seq))
    replayed = [item for item in drain_connection(handle) if item.id_line]
    assert len(replayed) == 1
    assert replayed[0].seq == event.seq
    assert replayed[0].frames == stored[0].frames


def _unsequenced_chunky(text: str, *, event_id: str = "chunky-event"):
    """The same logical event the test just published, unsequenced.

    Prepared independently of the emit so the frames under test are the ones
    a real reconnect would be re-sent, not a re-serialization of them.
    """
    from agent_alfred.events import UnsequencedEvent

    return UnsequencedEvent(
        event_id=event_id,
        envelope=EventEnvelope(
            ts=0.0,
            run_id="chunky",
            session_id=None,
            step_index=None,
            attempt_id=None,
            node_id=None,
        ),
        payload=RunStarted(purpose="chat", user_message=user_message_with(text)),
        trace_policy="persist",
        replayable=True,
    )


def _chunk_meta(frame: bytes) -> dict:
    raw = frame.split(b"data: ", 1)[1].decode("utf-8")
    head, _separator, _payload = raw.partition('"payload":')
    return json.loads(head.rstrip().rstrip(",") + "}")


# --- the concurrency race --------------------------------------------------


class _CaptureRegistrationGate:
    """Pause one connect after capture and before registration."""

    def __init__(self) -> None:
        self._entries = 0
        self._lock = threading.Lock()
        self.registration_entered = threading.Event()
        self.release_registration = threading.Event()

    def __enter__(self) -> None:
        with self._lock:
            self._entries += 1
            entry = self._entries
        if entry == 2:
            self.registration_entered.set()
            assert self.release_registration.wait(timeout=2), (
                "test did not release registration"
            )

    def __exit__(self, *_exc: object) -> None:
        return None


def test_snapshots_and_live_events_never_duplicate_or_gap_under_concurrency() -> None:
    spawn = _NoThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=spawn.spawn,
    )
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    gate = _CaptureRegistrationGate()
    connections = [broker.connect(connection=FakeConnection(), cursor=cursor_for(0))]
    broker.bind_projection_boundary(gate)
    connected: list[object] = []
    connect_errors: list[BaseException] = []
    connect_finished = threading.Event()

    def connect_across_publication() -> None:
        try:
            connected.append(
                broker.connect(connection=FakeConnection(), cursor=cursor_for(0))
            )
        except BaseException as exc:
            connect_errors.append(exc)
        finally:
            connect_finished.set()

    worker = threading.Thread(target=connect_across_publication, daemon=True)
    try:
        worker.start()
        assert gate.registration_entered.wait(timeout=2), (
            "connect never reached the capture-to-registration boundary"
        )
        crossed = fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=1.0,
                run_id="r1",
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )
        gate.release_registration.set()
        assert connect_finished.wait(timeout=2), "connect did not finish"
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert not connect_errors
        assert len(connected) == 1
        connections.extend(connected)

        after_registration = fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=2.0,
                run_id="r2",
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )
        assert [crossed.seq, after_registration.seq] == [1, 2]
        assert broker.deliver_next(timeout=0)
        assert broker.deliver_next(timeout=0)

        for handle in connections:
            ids = replay_ids(drain_connection(handle))
            target_ids = [seq for seq in ids if seq in (1, 2)]
            assert target_ids == [1, 2]
    finally:
        gate.release_registration.set()
        worker.join(timeout=2)
        broker.close(timeout=2.0)


def _ids_from_wire(wire: bytes) -> list[int]:
    """Parse ``id: <process_instance_id>:<seq>`` back into the bare seq."""
    ids = []
    for line in wire.split(b"\n"):
        if not line.startswith(b"id: "):
            continue
        _prefix, _, seq = line.decode().rpartition(":")
        ids.append(int(seq))
    return ids


# --- flush, close, and failure isolation -----------------------------------


def test_flush_cannot_claim_to_have_flushed() -> None:
    harness = Harness()
    result = harness.broker.flush("r1")
    assert isinstance(result, BestEffortFlushResult)
    assert result.outcome == "best_effort"
    assert harness.broker.flush_at_run_end is False


def test_capacity_defaults_match_the_decided_table() -> None:
    broker = SSEBroker(process_instance_id=INSTANCE, snapshot=runtime_snapshot())
    assert broker._ingress.budget == frames.FrameBudget(4096, 32 * 1024 * 1024)
    assert broker._ring.budget == frames.FrameBudget(2048, 32 * 1024 * 1024)


# --- the named cost of the ingress's accounting ------------------------------


def test_ingress_offer_keeps_queue_and_cost_atomic_across_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before-effect offers charge nothing; after-effect offers charge once."""
    before = Harness()
    before_queue = before.broker._ingress._items  # noqa: SLF001

    class InterruptBeforeOffer:
        def put(self, item) -> None:
            if isinstance(item, _PublishedEvent):
                raise KeyboardInterrupt("offer interrupted before enqueue")
            before_queue.put(item)

        def offer(self, item) -> bool:
            raise KeyboardInterrupt("offer interrupted before enqueue")

        def __getattr__(self, name):
            return getattr(before_queue, name)

    monkeypatch.setattr(  # noqa: SLF001
        before.broker._ingress,
        "_items",
        InterruptBeforeOffer(),
    )
    with pytest.raises(KeyboardInterrupt, match="before enqueue"):
        before.emit(RunStarted(purpose="chat"))
    assert before.broker._ingress.current_cost == frames.FrameCost(0, 0)
    # The data offer had no effect. The one free item is the recovery kick
    # owed by the already-committed ring generation, not phantom data/cost.
    assert before.broker._ingress._items.qsize() == 1  # noqa: SLF001
    assert before.broker.deliver_next(timeout=0) is True
    assert before.broker._ingress._items.qsize() == 0  # noqa: SLF001

    after = Harness()
    after_queue = after.broker._ingress._items  # noqa: SLF001

    class InterruptAfterOffer:
        def put(self, item) -> None:
            after_queue.put(item)
            if isinstance(item, _PublishedEvent):
                raise KeyboardInterrupt("offer interrupted after enqueue")

        def offer(self, item) -> bool:
            accepted = after_queue.offer(item)
            if accepted:
                raise KeyboardInterrupt("offer interrupted after enqueue")
            return accepted

        def __getattr__(self, name):
            return getattr(after_queue, name)

    monkeypatch.setattr(  # noqa: SLF001
        after.broker._ingress,
        "_items",
        InterruptAfterOffer(),
    )
    with pytest.raises(KeyboardInterrupt, match="after enqueue"):
        after.emit(RunStarted(purpose="chat"))
    # One billed event plus the coalesced recovery kick: membership and cost
    # committed together exactly once despite the lost return edge.
    assert after.broker._ingress._items.qsize() == 2  # noqa: SLF001
    assert after.broker._ingress.current_cost.frames == 1
    assert after.broker.deliver_next(timeout=0) is True
    assert after.broker._ingress.current_cost == frames.FrameCost(0, 0)
    assert after.broker.deliver_next(timeout=0) is True
    assert after.broker._ingress._items.qsize() == 0  # noqa: SLF001


def test_ingress_take_keeps_cost_atomic_across_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before-effect takes retain both facts; after-effect takes remove both."""
    before = Harness()
    before.emit(RunStarted(purpose="chat"))
    before_queue = before.broker._ingress._items  # noqa: SLF001

    class InterruptBeforeTake:
        def get(self, *_args, **_kwargs):
            raise KeyboardInterrupt("take interrupted before dequeue")

        def __getattr__(self, name):
            return getattr(before_queue, name)

    monkeypatch.setattr(  # noqa: SLF001
        before.broker._ingress,
        "_items",
        InterruptBeforeTake(),
    )
    with pytest.raises(KeyboardInterrupt, match="before dequeue"):
        before.broker.deliver_next(timeout=0)
    assert before.broker._ingress._items.qsize() == 1  # noqa: SLF001
    assert before.broker._ingress.current_cost.frames == 1

    after = Harness()
    after.emit(RunStarted(purpose="chat"))
    owned_queue = after.broker._ingress._items  # noqa: SLF001
    interrupted = False

    class TakeThenInterruptQueue:
        def get(self, *args, **kwargs):
            nonlocal interrupted
            item = owned_queue.get(*args, **kwargs)
            if isinstance(item, _PublishedEvent) and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("take interrupted after dequeue")
            return item

        def __getattr__(self, name):
            return getattr(owned_queue, name)

    proxy = TakeThenInterruptQueue()
    monkeypatch.setattr(after.broker._ingress, "_items", proxy)  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt, match="after dequeue"):
        after.broker.deliver_next(timeout=0)
    assert proxy.qsize() == 0
    assert after.broker._ingress.current_cost == frames.FrameCost(0, 0)


def test_ingress_take_clears_kick_ownership_after_the_dequeue_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed kick can be recreated when its sweep never ran."""
    harness = Harness(
        ingress_budget=frames.FrameBudget(frames=1, encoded_bytes=1)
    )
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    snapshot = runtime_snapshot(state_revision=1)
    assert broker.publish_state_patch(snapshot) is False
    owned_queue = broker._ingress._items  # noqa: SLF001
    interrupted = False

    class TakeThenInterruptQueue:
        def get(self, *args, **kwargs):
            nonlocal interrupted
            item = owned_queue.get(*args, **kwargs)
            if isinstance(item, _IngressKick) and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("kick take interrupted after dequeue")
            return item

        def __getattr__(self, name):
            return getattr(owned_queue, name)

    proxy = TakeThenInterruptQueue()
    monkeypatch.setattr(broker._ingress, "_items", proxy)  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt, match="after dequeue"):
        broker.deliver_next(timeout=0)
    assert proxy.qsize() == 0
    assert broker.publish_state_patch(replace(snapshot)) is False
    assert proxy.qsize() == 1
    assert broker.deliver_next(timeout=0) is True
    assert old.queue.close_requested is True


def test_ingress_rotation_never_holds_the_producer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backlog reversal cannot make a concurrent commit cost O(backlog)."""
    harness = Harness()
    backlog = 32
    for index in range(backlog):
        harness.emit(RunStarted(purpose="chat"), run_id=f"r{index}")

    entered_rotation = threading.Event()
    release_rotation = threading.Event()
    consumer_done = threading.Event()
    producer_done = threading.Event()
    failures: list[BaseException] = []
    taken: list[_PublishedEvent] = []
    real_reverse = broker_module._reverse_ingress_nodes

    def gated_reverse(node):
        entered_rotation.set()
        assert release_rotation.wait(5.0), "test never released ingress rotation"
        return real_reverse(node)

    def consume_one() -> None:
        try:
            item = harness.broker._ingress.take(timeout=1.0)  # noqa: SLF001
            assert isinstance(item, _PublishedEvent)
            taken.append(item)
        except BaseException as exc:
            failures.append(exc)
        finally:
            consumer_done.set()

    def publish_suffix() -> None:
        try:
            harness.emit(RunStarted(purpose="suffix"), run_id="suffix")
        except BaseException as exc:
            failures.append(exc)
        finally:
            producer_done.set()

    monkeypatch.setattr(broker_module, "_reverse_ingress_nodes", gated_reverse)
    consumer = threading.Thread(target=consume_one, daemon=True)
    producer = threading.Thread(target=publish_suffix, daemon=True)
    consumer.start()
    try:
        assert entered_rotation.wait(5.0), "consumer never began backlog rotation"
        producer.start()
        assert producer_done.wait(1.0), (
            "producer waited for the consumer's O(N) backlog rotation"
        )
    finally:
        release_rotation.set()
    assert consumer_done.wait(5.0), "consumer never completed its rotation"
    consumer.join()
    producer.join()

    while True:
        try:
            item = harness.broker._ingress.take(timeout=0)  # noqa: SLF001
        except queue.Empty:
            break
        assert isinstance(item, _PublishedEvent)
        taken.append(item)
    assert failures == []
    assert [item.published_seq for item in taken] == list(
        range(1, backlog + 2)
    )
    assert harness.broker._ingress.current_cost == frames.FrameCost(0, 0)


def test_retired_ingress_rotation_is_released_outside_the_producer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the old persistent prefix cannot make enqueue wait O(N)."""

    class ObservableIngressNode:
        __slots__ = ("__weakref__", "cost", "item", "next")

        def __init__(self, *, item, cost, next) -> None:
            self.item = item
            self.cost = cost
            self.next = next

    monkeypatch.setattr(broker_module, "FifoNode", ObservableIngressNode)
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"))
    ingress = harness.broker._ingress  # noqa: SLF001
    owned_queue = ingress._items  # noqa: SLF001
    retired = owned_queue._state.back  # noqa: SLF001
    assert retired is not None

    released = threading.Event()
    producer_lock_was_free: list[bool] = []

    def observe_release(_reference) -> None:
        acquired = owned_queue._condition.acquire(blocking=False)  # noqa: SLF001
        producer_lock_was_free.append(acquired)
        if acquired:
            owned_queue._condition.release()  # noqa: SLF001
        released.set()

    retired_ref = weakref.ref(retired, observe_release)
    del retired

    item = ingress.take(timeout=0)

    assert isinstance(item, _PublishedEvent)
    assert released.wait(1.0), "retired ingress prefix remained referenced"
    assert retired_ref() is None
    assert producer_lock_was_free == [True]


def test_ingress_rotation_interruption_retains_fifo_and_control_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed local rotation leaves its complete source available to retry."""
    harness = Harness()
    ingress = harness.broker._ingress  # noqa: SLF001
    published = harness.emit(RunStarted(purpose="chat"))
    ingress.put_kick()
    ingress.put_stop()
    real_reverse = broker_module._reverse_ingress_nodes
    first = True

    def interrupt_first_rotation(node):
        nonlocal first
        if first:
            first = False
            raise KeyboardInterrupt("rotation interrupted before installation")
        return real_reverse(node)

    monkeypatch.setattr(
        broker_module,
        "_reverse_ingress_nodes",
        interrupt_first_rotation,
    )
    with pytest.raises(KeyboardInterrupt, match="before installation"):
        ingress.take(timeout=0)

    assert ingress._items.qsize() == 3  # noqa: SLF001
    assert ingress.current_cost.frames == 1
    # Both controls are still owned while the prefix waits in ``rotation``.
    ingress.put_kick()
    ingress.put_stop()
    assert ingress._items.qsize() == 3  # noqa: SLF001

    data = ingress.take(timeout=0)
    kick = ingress.take(timeout=0)
    stop = ingress.take(timeout=0)
    assert isinstance(data, _PublishedEvent)
    assert data.published_seq == published.seq
    assert isinstance(kick, _IngressKick)
    assert isinstance(stop, _IngressStop)
    assert ingress.current_cost == frames.FrameCost(0, 0)
    assert ingress._items.qsize() == 0  # noqa: SLF001


def test_the_ingress_count_returns_exactly_to_zero() -> None:
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    cost = harness.broker._ingress.current_cost
    assert cost.frames == 2
    assert cost.encoded_bytes > 0
    drain_dispatcher(harness)
    assert harness.broker._ingress.current_cost == frames.FrameCost(0, 0)


def test_a_state_patch_pays_a_named_one_frame_cost() -> None:
    harness = Harness()
    snapshot = runtime_snapshot(state_revision=1)
    assert harness.broker.publish_state_patch(snapshot) is True
    expected = broker_module._patch_cost(
        snapshot, harness.broker._progress.projection(None)
    )
    assert harness.broker._ingress.current_cost == frames.FrameCost(
        frames=1, encoded_bytes=expected
    )


def test_close_is_idempotent_and_leaves_no_thread_running() -> None:
    threads: list[threading.Thread] = []

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=spawn,
    )
    broker.start()
    connection = FakeConnection()
    broker.connect(connection=connection)
    assert broker.close(timeout=2.0) is True
    assert broker.close(timeout=2.0) is True
    for thread in threads:
        assert not thread.is_alive()
    assert connection.closed is True


def test_dispatcher_factory_return_exit_retires_unstarted_thread() -> None:
    """An interrupted factory return cannot publish a never-started thread."""
    entered = threading.Event()
    threads: list[threading.Thread] = []

    def spawn(target):
        def observed_target() -> None:
            entered.set()
            target()

        thread = threading.Thread(target=observed_target, daemon=True)
        threads.append(thread)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=spawn,
    )
    code = SSEBroker.start.__code__
    instructions = tuple(dis.get_instructions(code))
    owner_store = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_dispatcher"
    )
    target = instructions[owner_store + 1].offset
    control = SystemExit("dispatcher factory returned before start")
    try:
        with interrupt_instruction_once(code, target, control) as armed:
            with pytest.raises(SystemExit) as caught:
                broker.start()

        assert armed == [False]
        assert caught.value is control
        assert entered.is_set() is False
        assert broker.close(timeout=0) is True
    finally:
        broker._ingress.put_stop()  # noqa: SLF001
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2.0)
        broker.close(timeout=2.0)


def test_custom_dispatcher_start_after_effect_keeps_thread_owned() -> None:
    """The broker owns an injected thread before asking it to start."""
    failure = KeyboardInterrupt("start raised after starting dispatcher")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    threads: list[threading.Thread] = []

    class StartThenFailThread:
        def __init__(self, target) -> None:
            self.thread = threading.Thread(target=target, daemon=True)
            threads.append(self.thread)

        def start(self) -> None:
            self.thread.start()
            assert entered.wait(2.0), "dispatcher never entered its target"
            raise failure

        def join(self, timeout=None) -> None:
            self.thread.join(timeout)

        def is_alive(self) -> bool:
            return self.thread.is_alive()

    def spawn_unstarted(target):
        return StartThenFailThread(target)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=spawn_unstarted,
    )
    real_dispatch = broker._dispatch_loop  # noqa: SLF001

    def gated_dispatch() -> None:
        entered.set()
        release.wait()
        try:
            real_dispatch()
        finally:
            exited.set()

    broker._dispatch_loop = gated_dispatch  # type: ignore[method-assign]  # noqa: SLF001
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.start()

        assert raised.value is failure
        assert broker.close(timeout=0) is False
        assert exited.is_set() is False
        release.set()
        assert exited.wait(2.0), "owned dispatcher never exited"
        assert broker.close(timeout=2.0) is True
    finally:
        release.set()
        broker._ingress.put_stop()  # noqa: SLF001
        for thread in threads:
            thread.join(timeout=2.0)
        broker.close(timeout=2.0)


def test_broker_close_retries_an_interrupted_unstarted_dispatcher_probe() -> None:
    """An unresolved start refusal cannot strand broker close forever."""
    dispatcher = ProbeInterruptedUnstartedThread(
        start_failure=RuntimeError("dispatcher did not start"),
        probe_failure=KeyboardInterrupt("dispatcher start probe interrupted"),
        name="unstarted-sse-dispatch",
    )
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=lambda _target: dispatcher,
    )

    with pytest.raises(RuntimeError) as raised:
        broker.start()
    assert raised.value is dispatcher.start_failure
    assert dispatcher.probe_calls == 1

    assert broker.close(timeout=0) is True
    assert dispatcher.probe_calls == 2


@pytest.mark.parametrize("after_effect", (False, True), ids=("before", "after"))
def test_default_dispatcher_start_exit_keeps_its_real_thread_owned(
    after_effect: bool,
) -> None:
    """A start-effect dispatcher must exit before broker close reports True."""
    failure = KeyboardInterrupt(f"dispatcher start {after_effect=}")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    real_dispatch = broker._dispatch_loop  # noqa: SLF001

    def gated_dispatch() -> None:
        entered.set()
        release.wait()
        try:
            real_dispatch()
        finally:
            exited.set()

    broker._dispatch_loop = gated_dispatch  # type: ignore[method-assign]  # noqa: SLF001
    code, target = _dispatcher_start_instruction(after_effect=after_effect)

    try:
        with interrupt_instruction_once(code, target, failure) as armed:
            with pytest.raises(KeyboardInterrupt) as raised:
                broker.start()
        assert armed == [False]
        assert raised.value is failure
        if not after_effect:
            assert entered.is_set() is False
            assert broker.close(timeout=0) is True
            return

        assert entered.wait(2.0), "real dispatcher target never entered"
        assert broker.close(timeout=0) is False
        assert exited.is_set() is False
        release.set()
        assert exited.wait(2.0), "owned dispatcher never exited"
        assert broker.close(timeout=2.0) is True
    finally:
        release.set()
        # Old code loses the Thread identity and therefore never queues stop.
        broker._ingress.put_stop()  # noqa: SLF001
        exited.wait(2.0)
        broker.close(timeout=2.0)


def test_close_claims_registered_cleanup_while_postcommit_is_delayed() -> None:
    """Registration, not a later callback, makes retirement drainable."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    after_commit_entered = threading.Event()
    release_after_commit = threading.Event()
    publish_done = threading.Event()

    def gated_after_commit(_event: SequencedEvent) -> None:
        after_commit_entered.set()
        assert release_after_commit.wait(3.0), "test did not release after_commit"

    def publish() -> None:
        try:
            harness.fanout.emit_linearized(
                RunStarted(purpose="chat"),
                EventEnvelope(0.0, "r1", None, None, None, None),
                boundary=nullcontext(),
                after_commit=gated_after_commit,
            )
        finally:
            publish_done.set()

    publisher = threading.Thread(target=publish, name="normal-cleanup-publisher")
    publisher.start()
    assert after_commit_entered.wait(3.0), "publisher never returned cleanup"
    assert harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
    harness.fanout._lock.release()  # noqa: SLF001

    close_result = harness.broker.close(timeout=0)
    try:
        with harness.broker._commit_cleanup_lock:  # noqa: SLF001
            debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
            assert debts == ()
        assert close_result is True
        assert harness.broker._closed is True  # noqa: SLF001
        assert publish_done.is_set() is False
    finally:
        release_after_commit.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert publish_done.is_set()

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    assert retired_ref() is None
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
        assert harness.broker._returned_commit_cleanup == {}  # noqa: SLF001


def test_close_recovers_a_normal_owner_whose_boundary_was_skipped() -> None:
    """Shutdown can claim a registered owner on its first attempt."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(0.0, "orphan", None, None, None, None)
    prepared = harness.broker.prepare(
        UnsequencedEvent(
            event_id="orphan",
            envelope=envelope,
            payload=payload,
            trace_policy="persist",
            replayable=True,
        )
    )
    post_commit = harness.broker.commit(
        prepared,
        SequencedEvent(
            seq=2,
            process_instance_id=INSTANCE,
            event_id="orphan",
            envelope=envelope,
            payload=payload,
            trace_policy="persist",
            replayable=True,
        ),
    )
    assert post_commit is not None

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    assert retired_ref() is None
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001


def test_close_recovers_ring_owner_if_settlement_was_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned ring stays visible until shutdown repays its own owner."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded
    failure = KeyboardInterrupt("ring failed before settlement")
    real_drop = ring._entries.drop_prefix  # noqa: SLF001

    def fail_after_drop(count: int):
        real_drop(count)
        raise failure

    monkeypatch.setattr(ring._entries, "drop_prefix", fail_after_drop)  # noqa: SLF001
    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(0.0, "orphan", None, None, None, None)
    prepared = harness.broker.prepare(
        UnsequencedEvent(
            event_id="orphan",
            envelope=envelope,
            payload=payload,
            trace_policy="persist",
            replayable=True,
        )
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        harness.broker.commit(
            prepared,
            SequencedEvent(
                seq=2,
                process_instance_id=INSTANCE,
                event_id="orphan",
                envelope=envelope,
                payload=payload,
                trace_policy="persist",
                replayable=True,
            ),
        )
    assert raised.value is failure

    assert harness.broker.close(timeout=0) is True
    failure.__traceback__ = None
    del raised
    gc.collect()
    assert retired_ref() is None
    assert harness.broker._ring_cleanup_failure is None  # noqa: SLF001


def test_close_waits_for_normal_ring_cleanup_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal retirement remains ledger-owned throughout its lock-free call."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    publish_done = threading.Event()
    release_calls = 0

    def gated_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls
        release_calls += 1
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release cleanup"
        real_release(start, count, through_seq)

    monkeypatch.setattr(
        ring._entries, "release_retired", gated_release  # noqa: SLF001
    )

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        finally:
            publish_done.set()

    publisher = threading.Thread(target=publish, name="normal-cleanup-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "publisher never began ring cleanup"

    close_result = harness.broker.close(timeout=0)
    try:
        with harness.broker._commit_cleanup_lock:  # noqa: SLF001
            debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
            assert len(debts) == 1 and debts[0].in_progress is True
        assert close_result is False
        assert harness.broker._closed is False  # noqa: SLF001
        assert publish_done.is_set() is False
    finally:
        release_cleanup.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert publish_done.is_set()

    assert harness.broker.close(timeout=0) is True
    assert release_calls == 1
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
        assert harness.broker._returned_commit_cleanup == {}  # noqa: SLF001


def test_close_waits_for_unsettled_and_requeued_commit_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """True means every ring cleanup owner has completed, not merely stopped."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    real_release = ring._entries.release_retired  # noqa: SLF001
    release_attempts = 0
    cleanup_failure = SystemExit("cleanup interrupted once")

    def fail_cleanup_once(start: int, count: int, through_seq: int) -> None:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise cleanup_failure
        real_release(start, count, through_seq)

    ingress_failure = KeyboardInterrupt("ingress interrupted")

    def fail_offer(_item) -> bool:
        raise ingress_failure

    real_settle = harness.broker.settle_interrupted_commit
    settlement_entered = threading.Event()
    release_settlement = threading.Event()

    def gated_settlement(failure: BaseException):
        settlement_entered.set()
        assert release_settlement.wait(3.0), "test did not release settlement"
        return real_settle(failure)

    monkeypatch.setattr(
        ring._entries, "release_retired", fail_cleanup_once  # noqa: SLF001
    )
    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(
        harness.broker, "settle_interrupted_commit", gated_settlement
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="cleanup-publisher")
    publisher.start()
    assert settlement_entered.wait(3.0), "publisher never retained cleanup"
    fanout_unlocked = harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
    assert fanout_unlocked, "settlement gate still held the FanOut lock"
    harness.fanout._lock.release()  # noqa: SLF001
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
    assert len(debts) == 1 and debts[0].ready is True

    try:
        assert harness.broker.close(timeout=0) is False
        assert harness.broker._closed is False  # noqa: SLF001
    finally:
        release_settlement.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert caught == [ingress_failure]
    assert release_attempts == 1
    assert retired_ref() is not None

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    debt = debts[0]
    assert release_attempts == 2
    assert retired_ref() is None
    assert debt.completed is True
    assert debt.in_progress is False
    assert debt.ready is False
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
        assert harness.broker._returned_commit_cleanup == {}  # noqa: SLF001


def test_close_waits_for_commit_cleanup_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup claimed by FanOut still owns close completion until success."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    release_calls = 0
    lock_checks: list[tuple[bool, bool]] = []

    def gated_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls
        release_calls += 1
        broker_unlocked = harness.broker._lock.acquire(blocking=False)  # noqa: SLF001
        if broker_unlocked:
            harness.broker._lock.release()  # noqa: SLF001
        fanout_unlocked = harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
        if fanout_unlocked:
            harness.fanout._lock.release()  # noqa: SLF001
        lock_checks.append((broker_unlocked, fanout_unlocked))
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release cleanup"
        real_release(start, count, through_seq)

    ingress_failure = KeyboardInterrupt("ingress interrupted")

    def fail_offer(_item) -> bool:
        raise ingress_failure

    monkeypatch.setattr(
        ring._entries, "release_retired", gated_release  # noqa: SLF001
    )
    monkeypatch.setattr(harness.broker._ingress, "offer", fail_offer)  # noqa: SLF001
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="cleanup-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "publisher never claimed cleanup"
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
        assert len(debts) == 1 and debts[0].in_progress is True

    try:
        assert harness.broker.close(timeout=0) is False
        assert harness.broker._closed is False  # noqa: SLF001
    finally:
        release_cleanup.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert caught == [ingress_failure]

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    assert release_calls == 1
    assert lock_checks == [(True, True)]
    assert retired_ref() is None
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
        assert harness.broker._returned_commit_cleanup == {}  # noqa: SLF001


def test_commit_cannot_register_new_cleanup_after_close_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping and ring publication are ordered by the same broker lock."""
    harness = Harness()
    commit_entered = threading.Event()
    release_commit = threading.Event()
    publish_done = threading.Event()
    real_commit = harness.broker.commit

    def gated_commit(prepared: object, event: SequencedEvent):
        commit_entered.set()
        assert release_commit.wait(3.0), "test did not release publisher"
        return real_commit(prepared, event)

    monkeypatch.setattr(harness.broker, "commit", gated_commit)

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="too-late")
        finally:
            publish_done.set()

    publisher = threading.Thread(target=publish, name="late-publisher")
    publisher.start()
    assert commit_entered.wait(3.0), "publisher never reached commit"

    try:
        assert harness.broker.close(timeout=0) is True
        assert publish_done.is_set() is False
    finally:
        release_commit.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert publish_done.is_set()
    try:
        assert harness.broker._ring.published_high_water_seq() == 0  # noqa: SLF001
        assert harness.broker._ingress.current_cost == frames.FrameCost(0, 0)  # noqa: SLF001
        with harness.broker._commit_cleanup_lock:  # noqa: SLF001
            assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
    finally:
        assert harness.broker.close(timeout=0) is True


def test_concurrent_close_posts_one_owned_stop() -> None:
    """Every closer may race at the call edge; the ingress owns one stop."""
    harness = Harness()
    broker = harness.broker
    broker.start()
    closers = 8
    at_handoff = threading.Barrier(closers)
    real_put_stop = broker._ingress.put_stop  # noqa: SLF001
    outcomes: list[bool] = []
    failures: list[BaseException] = []
    finished = [threading.Event() for _ in range(closers)]

    def gated_put_stop() -> None:
        at_handoff.wait(timeout=5.0)
        real_put_stop()

    def close(index: int) -> None:
        try:
            outcomes.append(broker.close(timeout=1.0))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished[index].set()

    broker._ingress.put_stop = gated_put_stop  # noqa: SLF001
    threads = [
        threading.Thread(target=close, args=(index,), daemon=True)
        for index in range(closers)
    ]
    for thread in threads:
        thread.start()
    for done in finished:
        assert done.wait(5.0), "a concurrent close never completed"
    for thread in threads:
        thread.join()

    assert failures == []
    assert outcomes == [True] * closers
    assert broker._ingress._items.qsize() == 1  # noqa: SLF001
    assert broker.deliver_next(timeout=0) is False
    assert broker._ingress._items.qsize() == 0  # noqa: SLF001


def test_stop_handoff_recovers_before_and_after_effect_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close retry neither loses nor duplicates its one-shot stop."""
    harness = Harness()
    broker = harness.broker
    broker.start()
    owned_queue = broker._ingress._items  # noqa: SLF001
    real_put_stop = broker._ingress.put_stop  # noqa: SLF001
    before = True

    def interrupt_before_effect() -> None:
        nonlocal before
        if before:
            before = False
            raise KeyboardInterrupt("stop interrupted before enqueue")
        real_put_stop()

    monkeypatch.setattr(
        broker._ingress,  # noqa: SLF001
        "put_stop",
        interrupt_before_effect,
    )
    with pytest.raises(KeyboardInterrupt, match="before enqueue"):
        broker.close(timeout=1.0)
    assert owned_queue.qsize() == 0

    class PutThenInterruptQueue:
        interrupted = False

        def put(self, item) -> None:
            owned_queue.put(item)
            if isinstance(item, _IngressStop) and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt("stop interrupted after enqueue")

        def __getattr__(self, name):
            return getattr(owned_queue, name)

    proxy = PutThenInterruptQueue()
    monkeypatch.setattr(broker._ingress, "_items", proxy)  # noqa: SLF001
    with pytest.raises(KeyboardInterrupt, match="after enqueue"):
        broker.close(timeout=1.0)
    assert proxy.qsize() == 1
    assert broker.close(timeout=1.0) is True
    assert proxy.qsize() == 1
    assert broker.deliver_next(timeout=0) is False
    assert proxy.qsize() == 0


def test_a_wedged_connection_does_not_hold_close_open() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Stuck(FakeConnection):
        def write(self, data: bytes) -> None:
            del data
            entered.set()
            release.wait()

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    broker.start()
    connection = Stuck()
    handle = broker.connect(connection=connection)
    try:
        assert entered.wait(2.0), "writer did not enter its wedged write"
        broker.publish_state_patch(runtime_snapshot(state_revision=1))
        started = time.monotonic()
        assert broker.close(timeout=0.3) is False
        # Bounded, and the descriptor is released rather than left behind.
        assert time.monotonic() - started < 2.0
        assert connection.closed is True
    finally:
        release.set()
        assert handle.finished.wait(2.0), "released writer did not exit"
        assert broker.close(timeout=2.0) is True


def test_a_blocking_connection_close_cannot_escape_the_broker_deadline() -> None:
    """Socket interruption runs under a retained owner, never on the caller."""
    write_entered = threading.Event()
    release_write = threading.Event()
    close_entered = threading.Event()
    release_close = threading.Event()
    close_returned = threading.Event()
    outcomes: list[bool] = []

    class BlockingClose(FakeConnection):
        def write(self, data: bytes) -> None:
            write_entered.set()
            release_write.wait()
            super().write(data)

        def close(self) -> None:
            close_entered.set()
            release_write.set()
            release_close.wait()
            super().close()

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    broker.start()
    handle = broker.connect(connection=BlockingClose())
    assert write_entered.wait(2.0), "writer never reached the blocked connection"

    def close_broker() -> None:
        try:
            outcomes.append(broker.close(timeout=0))
        finally:
            close_returned.set()

    closer = threading.Thread(target=close_broker, daemon=True)
    closer.start()
    try:
        assert close_returned.wait(2.0), (
            "broker close blocked past its zero-second deadline"
        )
        assert outcomes == [False]
        assert close_entered.wait(2.0), "connection close owner never ran"
        assert handle.finished.is_set() is False
    finally:
        release_write.set()
        release_close.set()
        closer.join(timeout=2.0)
        broker._ingress.put_stop()  # noqa: SLF001
        broker.close(timeout=2.0)


# --- close() is a completion report, not an intention -----------------------


def test_close_budget_covers_an_unstarted_stream_connection_cleanup() -> None:
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    class HeldCloseConnection(FakeConnection):
        def close(self) -> None:
            entered.set()
            assert release.wait(5.0), "the test must release connection cleanup"
            super().close()

    harness = Harness()
    connection = HeldCloseConnection()
    harness.broker.prepare_stream(connection=connection)
    assert harness.broker.registrations_in_flight == 1

    def close() -> None:
        try:
            outcomes.append(harness.broker.close(timeout=0))
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert entered.wait(2.0), "connection cleanup never began"
        assert returned.wait(2.0), "zero-budget close waited for the blocked socket"
        closer.join(2.0)
        assert errors == []
        assert outcomes == [False]
        assert not connection.closed
        assert harness.broker.registrations_in_flight == 1

        release.set()
        assert harness.broker.close(timeout=2.0) is True
        assert connection.closed
        assert harness.broker.registrations_in_flight == 0
        assert harness.broker.connections == ()
    finally:
        release.set()
        closer.join(2.0)
        harness.broker.close(timeout=2.0)


def test_close_reports_false_until_every_thread_has_really_exited() -> None:
    """close() answers a question about threads, not about bookkeeping.

    While the dispatcher or any writer is still alive, close() must answer
    False -- on *every* attempt, not only the first. A True that comes from
    the first attempt's own state rather than from the threads having
    exited tells the caller to release what the broker is still draining:
    the database those frames were built from and the process lock the
    stream lives under.
    """
    gates = GatedThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=gates.spawn,
    )
    broker.start()
    broker.connect(connection=FakeConnection())
    dispatcher = gates.by_name["_dispatch_loop"]
    writer = gates.by_name["<lambda>"]

    # First attempt: both threads alive. Stopping has begun; closing has not.
    assert broker.close(timeout=0.05) is False
    assert dispatcher.is_alive()
    assert writer.is_alive()

    # The dispatcher drains as soon as it is allowed to run, but the writer
    # is still alive. A repeated close must keep joining and keep saying
    # False rather than flip to True on the strength of the first attempt.
    gates.open("_dispatch_loop")
    dispatcher.join(timeout=5.0)
    assert not dispatcher.is_alive()
    assert broker.close(timeout=0.05) is False
    assert writer.is_alive()

    # The last thread exits; the next close completes, and stays complete.
    gates.open("<lambda>")
    writer.join(timeout=5.0)
    assert not writer.is_alive()
    assert broker.close(timeout=0.05) is True
    assert broker.close(timeout=0.05) is True


class _GatedSpawn:
    """Holds one spawned thread at the registration's last step.

    The connect path registers a handle and then installs its writer; a
    close that runs while the writer is not installed yet must refuse to
    complete. The gate parks the spawn itself -- the step between "the
    handle is in the registry" and "the writer thread exists".
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []

    def spawn(self, target):
        self.entered.set()
        self.release.wait()
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        return thread


@pytest.mark.parametrize("failure", [RuntimeError("spawn failed"), KeyboardInterrupt()])
def test_failed_writer_handoff_revokes_the_whole_registration(failure) -> None:
    """A failed spawn propagates unchanged and leaves no pre-writer owner."""
    target_ref = None

    def fail_spawn(target):
        nonlocal target_ref
        target_ref = weakref.ref(target)
        raise failure

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=fail_spawn,
    )
    connection = FakeConnection()

    with pytest.raises(type(failure)) as raised:
        broker.connect(connection=connection)

    assert raised.value is failure
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert target_ref is not None
    del raised
    failure.__traceback__ = None
    gc.collect()
    assert target_ref() is None


def test_close_cannot_complete_behind_an_in_flight_registration() -> None:
    """A close must not answer True while a connection is mid-registration.

    A handle that is registered but whose writer is not installed yet is a
    writer this close has not seen. Answering True here sends the caller
    off to close the database and release the process lock, and the writer
    starts afterwards -- a live writer on a broker the caller believes is
    closed. The registration must finish (or be revoked) before ``closed``
    is published, and when the close does answer True there must be no
    registered handle, no registration in flight, and no live thread.
    """
    gated = _GatedSpawn()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=gated.spawn,
    )
    connect_done = threading.Event()

    def do_connect() -> None:
        broker.connect(connection=FakeConnection())
        connect_done.set()

    connector = threading.Thread(target=do_connect, daemon=True)
    connector.start()
    try:
        assert gated.entered.wait(2.0), "connect never reached the writer step"
        # The handle is registered; the writer is not installed. This close
        # must not claim completion.
        assert broker.close(timeout=0.1) is False
        assert broker.registrations_in_flight == 1
        gated.release.set()
        assert connect_done.wait(5.0), "connect never finished its registration"
        assert broker.close(timeout=2.0) is True
        # A True answer is a fact about the registry and the threads.
        assert broker.connections == ()
        assert broker.registrations_in_flight == 0
    finally:
        gated.release.set()
        connector.join(timeout=5.0)


def test_a_closing_broker_refuses_new_connections_without_a_writer() -> None:
    """Once closing has begun, a connect registers nothing and starts none.

    The refused connection is closed by the broker -- outside the broker
    lock -- and its ``finished`` is observable, because the HTTP handler
    holding the stream open waits on exactly that event. Nothing is written:
    a startup sequence describes a stream the broker is taking away.
    """
    harness = Harness(spawn=RealThreadSpawner())
    held = GatedWriteConnection()
    harness.broker.connect(connection=held)
    assert held.entered_write.wait(2.0), "writer never reached its first write"
    assert harness.broker.close(timeout=0.2) is False  # stopping, not closed
    connection = FakeConnection()
    refused = harness.broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in harness.broker.connections
    assert connection.written == b""
    assert connection.closed is True
    held.release.set()
    assert harness.broker.close(timeout=2.0) is True


def test_a_closed_broker_refuses_new_connections_idempotently() -> None:
    """After close() has answered True, a connect gets the same refusal.

    Idempotence is the point: the first refusal and the one after a fully
    completed close are the same answer, for the same reason -- nothing may
    register, nothing may write, and the caller is told by ``finished``
    rather than left waiting on a stream that will never carry a frame.
    """
    harness = Harness(spawn=RealThreadSpawner())
    harness.connect()
    assert harness.broker.close(timeout=2.0) is True
    connection = FakeConnection()
    refused = harness.broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in harness.broker.connections
    assert connection.written == b""
    assert connection.closed is True
    # Asking again changes nothing.
    again = harness.broker.connect(connection=FakeConnection())
    assert again.finished.is_set()
    assert harness.broker.connections == ()


def test_one_connections_failure_does_not_touch_the_others() -> None:
    class Exploding(FakeConnection):
        def write(self, data: bytes) -> None:
            raise BrokenPipeError("gone")

    broken = Exploding()
    healthy = ObservedConnection()
    threads: list[threading.Thread] = []

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=spawn,
    )
    broker.start()
    broker.connect(connection=broken)
    broker.connect(connection=healthy)
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    for i in range(5):
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=float(i),
                run_id=f"r{i}",
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )
    healthy.wait_for_bytes(b"id: inst-test:5\n")
    broker.close(timeout=2.0)
    # The healthy tab saw every event even though its neighbour died. The 0
    # is the re-seed: both connections connected to an empty ring, so both
    # were planted the reserved startup boundary before any data.
    assert sorted(set(_ids_from_wire(healthy.written))) == [0, 1, 2, 3, 4, 5]
    assert broken.closed is True


def test_a_patch_is_broadcast_after_the_authoritative_snapshot_moves() -> None:
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=7))
    harness.deliver()
    patch = _wire_containing(handle, b"event: state_patch")
    assert b'"state_revision":7' in patch
    # A connection that arrives afterwards is primed with the same revision.
    late = harness.connect()
    assert b'"state_revision":7' in _wire_containing(late, b"event: state_patch")


def test_same_revision_identical_patch_recovery_does_not_offer_twice(
    monkeypatch,
) -> None:
    harness = Harness()
    offered = []
    offer = harness.broker._ingress.offer  # noqa: SLF001

    def count_offer(item):
        offered.append(item)
        return offer(item)

    monkeypatch.setattr(harness.broker._ingress, "offer", count_offer)  # noqa: SLF001
    snapshot = runtime_snapshot(state_revision=1)
    recovered = replace(snapshot)
    assert recovered == snapshot and recovered is not snapshot

    harness.broker.publish_state_patch(snapshot)
    harness.broker.publish_state_patch(recovered)

    assert len(offered) == 1, "an identical recovery patch entered ingress twice"
    assert harness.broker._latest == snapshot  # noqa: SLF001
    assert harness.broker._fatal is None  # noqa: SLF001


def test_same_revision_conflicting_patch_is_process_fatal(monkeypatch) -> None:
    from agent_alfred.events import ProcessFatalSinkError

    harness = Harness()
    offered = []
    reported: list[BaseException] = []
    harness.broker.bind_fatal_handler(reported.append)
    offer = harness.broker._ingress.offer  # noqa: SLF001

    def count_offer(item):
        offered.append(item)
        return offer(item)

    monkeypatch.setattr(harness.broker._ingress, "offer", count_offer)  # noqa: SLF001
    original = runtime_snapshot(state_revision=1)
    conflicting = runtime_snapshot(
        state_revision=1,
        coordinator_state="accepted",
        active_run=ActiveRunSummary(
            run_id="r-conflict",
            purpose="chat",
            gateway="web",
            phase="accepted",
            session_id="s1",
            prompt_preview="conflict",
            started_at=None,
            recording_state=None,
        ),
    )
    assert conflicting.state_revision == original.state_revision
    assert conflicting != original
    harness.broker.publish_state_patch(original)

    with pytest.raises(ProcessFatalSinkError) as raised:
        harness.broker.publish_state_patch(conflicting)

    assert harness.broker._fatal is raised.value  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001
    assert harness.broker._latest == original  # noqa: SLF001
    assert len(offered) == 1, "the conflicting patch must not enter ingress"
    assert reported == [raised.value]

    assert harness.broker.publish_state_patch(conflicting) is False
    assert reported == [raised.value], "one fatal transition must report once"
    assert harness.broker._latest == original  # noqa: SLF001
    assert len(offered) == 1


def test_an_undeliverable_patch_closes_the_connection() -> None:
    harness = Harness()
    handle = harness.connect()
    # Fill the queue to the frame limit with events.
    for _ in range(600):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=9))
    # The refused patch raised the disconnect generation; the dispatcher
    # closes the connection that predates it.
    harness.deliver()
    assert handle.queue.close_requested is True
    assert any(isinstance(item, CloseConnection) for item in drain_connection(handle))


def test_a_broker_without_a_session_source_refuses_to_guess() -> None:
    """The snapshot's Session-validity field is a fact, not a default.

    The broker is built before the Host it asks, so there is a window in
    which it has no source. Answering "valid" there would publish a snapshot
    claiming a Session exists when nothing has been consulted.
    """
    broker = SSEBroker(process_instance_id=INSTANCE, snapshot=runtime_snapshot())
    try:
        with pytest.raises(RuntimeError):
            broker.connect(connection=FakeConnection())
        broker.bind_session_check(
            lambda session_id: "valid" if session_id is None else "invalid"
        )
        handle = broker.connect(connection=FakeConnection())
        assert handle is not None
    finally:
        assert broker.close(timeout=2.0) is True
    assert handle.finished.is_set(), "the test retained its stream writer"


def test_the_dispatcher_stops_on_the_sentinel() -> None:
    harness = Harness()
    harness.broker._ingress.put_stop()
    assert harness.broker.deliver_next(timeout=0.1) is False


def test_only_the_stop_sentinel_may_bypass_the_budget() -> None:
    """``put_stop`` takes no argument, so nothing else can ride it."""
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1))
    # The event cannot fit a one-byte ingress, so the overflow queues the
    # free kick -- and a full ingress still accepts the end sentinel: the
    # dispatcher's own end cannot be refused for want of room.
    harness.emit_many(1)
    harness.broker._ingress.put_stop()
    assert isinstance(harness.broker._ingress.take(timeout=0.1), _IngressKick)
    assert isinstance(harness.broker._ingress.take(timeout=0.1), _IngressStop)
    with pytest.raises(TypeError):
        harness.broker._ingress.put_stop(_IngressStop())
    # The kick wakes the dispatcher through its own free door -- also with
    # no argument, so nothing can ride past the budgets on it either.
    harness.broker._ingress.put_kick()
    with pytest.raises(TypeError):
        harness.broker._ingress.put_kick(_IngressKick())
    # A kick is not an end: the dispatcher sweeps and keeps going.
    assert harness.broker.deliver_next(timeout=0.1) is True
    # ...and the stop sentinel still ends it.
    assert harness.broker.deliver_next(timeout=0.1) is False


def test_commit_is_quantitative_and_never_writes_io() -> None:
    """The commit half must not touch a socket: closing a connection does IO
    and is therefore the writer thread's job, never the emitting thread's."""
    harness = Harness()
    handle = harness.connect()
    for _ in range(600):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    connection = handle.connection
    assert isinstance(connection, FakeConnection)
    before = len(connection.writes)
    # The queue is over its frame budget from here on.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    # Still nothing written by the emitting thread: the close is a flag the
    # writer thread acts on.
    assert len(connection.writes) == before
    assert handle.queue.close_requested is True


@pytest.mark.parametrize("seq", [1, 2, 3])
def test_a_sequenced_event_enters_the_ring_before_the_ingress(seq: int) -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    event = harness.emit(RunStarted(purpose="chat"), run_id=f"r{seq}")
    harness.deliver()
    # The very next event cannot fit the ingress, yet the ring already has
    # the previous one: recovery never depends on liveness.
    assert harness.broker._ring.latest_complete_seq() == event.seq
    harness.emit(RunStarted(purpose="chat"), run_id=f"r{seq + 10}")
    assert harness.broker._ring.latest_complete_seq() == event.seq + 1


def test_sequenced_events_pass_through_unchanged() -> None:
    harness = Harness()
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert isinstance(first, SequencedEvent)
    assert first.process_instance_id == INSTANCE
    second = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    # Zero is the reserved boundary before the first event, not an event
    # position: on a ring that has lost nothing it is a real starting point,
    # and resuming from it replays everything.
    handle = harness.connect(cursor=cursor_for(0))
    assert replay_ids(drain_connection(handle)) == [first.seq, second.seq]
    assert not any(
        b"replay_gap" in item.wire_bytes() for item in drain_connection(handle)
    )
    # A checkpoint this process did issue replays exactly its own tail.
    handle = harness.connect(cursor=cursor_for(first.seq))
    assert replay_ids(drain_connection(handle)) == [second.seq]


def _clock() -> FakeClock:
    return FakeClock()


# --- failure escalation ----------------------------------------------------


class _ExplodingQueue:
    """A connection queue that cannot take anything."""

    def __init__(self) -> None:
        self.close_requests = 0

    def offer(self, item):
        del item
        raise RuntimeError("dispatcher boom")

    def request_close(self) -> None:
        # The real queue marks itself and hands the writer a sentinel; the
        # stub records the ask, which is the part the fatal path exercises.
        self.close_requests += 1


def test_a_dispatcher_that_dies_is_reported_not_swallowed() -> None:
    """#23 §9: only the dispatcher or the ring escalates to sink_disabled.

    A dead dispatcher is not one connection's problem -- from that moment
    nothing published reaches any browser and the ring stops being the
    recovery source it claims to be. Swallowing it here would leave every
    client looking at a stream that died minutes ago and still says it is
    live.
    """
    harness = Harness()
    reported: list[BaseException] = []
    harness.broker.bind_fatal_handler(reported.append)
    handle = harness.connect()
    handle.queue = _ExplodingQueue()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    with pytest.raises(RuntimeError):
        harness.broker._dispatch_loop()  # noqa: SLF001 - the loop under test
    # Reported upward -- the broker does not decide what a process-level
    # fact means, it only refuses to keep quiet about one.
    assert [str(exc) for exc in reported] == ["dispatcher boom"]
    # And the broker stopped taking new work rather than limping on.
    assert harness.broker._stopping is True  # noqa: SLF001


def test_a_connection_registered_while_closing_is_stopped_at_once() -> None:
    """The HTTP handler waits on ``finished``.

    A connection registered after ``close()`` used to get a writer whose
    opening stream was written before any stop could reach it. The refusal
    is now earlier and stronger: no registry entry, no writer, no opening
    stream -- the broker closes the connection itself and ``finished`` is
    set, so the handler thread holding the stream open returns instead of
    waiting on a sentinel that is never coming.
    """
    harness = Harness()
    harness.broker.close(timeout=0.1)
    connection = FakeConnection()
    handle = harness.broker.connect(connection=connection)
    assert handle.finished.is_set()
    assert handle.thread is None
    assert connection.written == b""
    assert connection.closed is True
    assert handle not in harness.broker.connections


# --- a dead dispatcher is a process fact, not one connection's --------------


class _ExplodingConnectionQueue(ConnectionQueue):
    """A per-connection queue whose offer fails every time, for real.

    Unlike the plain ``_ExplodingQueue`` above, this one stands in for the
    queue a *connect* builds, so its constructor takes the budgets the
    broker passes -- and the dispatcher's fan-out dies on its first offer,
    deterministically, with no timing.
    """

    def offer(self, item):
        raise RuntimeError("fan-out is broken")


def _capture_fanout(broker):
    """A FanOut whose second sink records what the broker refuses to say."""
    capture = CapturingSink(name="capture")
    return capture, FanOutSink([broker, capture], process_instance_id=INSTANCE)


def test_a_fatal_dispatcher_closes_connections_and_refuses_the_rest(
    monkeypatch,
) -> None:
    """When the dispatcher dies, everything behind it stops honestly.

    A dispatcher that dies mid-fan-out leaves every existing connection
    open and every later publish pouring into an ingress nobody drains.
    The death is a process-level fact: the existing connections are asked
    to hang up, new connections are refused, later commits fail loudly (so
    the FanOut can disable this sink and tell every other sink why), and
    nothing accumulates behind a dead consumer.
    """
    # The re-raise after the fatal report is the dispatcher's own testimony;
    # it is silenced here because the report, not the traceback, is what
    # this test reads.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    fatal_calls: list[BaseException] = []
    fatal_done = threading.Event()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    broker.bind_fatal_handler(lambda exc: (fatal_calls.append(exc), fatal_done.set()))
    broker.start()
    capture, fanout = _capture_fanout(broker)
    handle = broker.connect(connection=FakeConnection())

    def emit(run_id: str) -> None:
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=0.0,
                run_id=run_id,
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )

    emit("r1")
    assert fatal_done.wait(5.0), "fatal handler never ran"
    assert len(fatal_calls) == 1
    assert isinstance(fatal_calls[0], RuntimeError)

    # Every existing connection is asked to hang up, and its writer leaves.
    assert handle.queue.close_requested is True
    assert handle.finished.wait(5.0), "connection was never asked to close"

    # No new connections behind a dead dispatcher.
    connection = FakeConnection()
    refused = broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in broker.connections
    assert connection.written == b""
    assert connection.closed is True

    # Later events neither accumulate nor pretend to succeed: the commit
    # fails so the FanOut disables this sink, the ingress stays empty, and
    # the failure is told to the other sinks exactly once -- which is the
    # no-recursion property too, because publishing that notice must not
    # kill the fan-out again.
    stalled = broker._ingress._items.qsize()  # noqa: SLF001
    assert stalled == 0  # the item that killed the fan-out was already taken
    for _ in range(3):
        emit("r1")
    assert broker._ingress._items.qsize() == stalled  # noqa: SLF001
    disabled = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(disabled) == 1


def test_a_fatal_commit_fails_instead_of_delivering_to_no_one(monkeypatch) -> None:
    """The sink says it cannot deliver; it does not silently swallow.

    After the dispatcher has died, a direct commit must raise -- that is
    what lets the FanOutSink disable this sink and record ``sink_disabled``
    for the Run -- and the refusal is stable: it does not depend on which
    thread asks or how many times.
    """
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    broker.connect(connection=FakeConnection())
    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r1",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id="fatal-event",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    prepared = broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=1,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    # Queue the item first, so the hand-driven dispatch loop has something
    # to take: the failing fan-out is what publishes the fatal state,
    # exactly as a live dispatcher's death would.
    broker.commit(prepared, sequenced)
    with pytest.raises(RuntimeError):
        broker._dispatch_loop()  # noqa: SLF001
    # The refusal is stable across repeats, so the later commits use their
    # own seqs: what must fail is the fatal check, not the ring's
    # monotonicity.
    for later_seq in (2, 3):
        with pytest.raises(RuntimeError):
            broker.commit(
                prepared,
                replace(sequenced, seq=later_seq),
            )


def test_sequence_binding_exhaustion_is_a_process_fatal_refusal() -> None:
    from agent_alfred.events import ProcessFatalSinkError

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r1",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id="sequence-exhaustion",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    prepared = broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=10**25,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )

    with pytest.raises(ProcessFatalSinkError, match="publication machinery"):
        broker.commit(prepared, sequenced)

    assert isinstance(broker._fatal, ValueError)  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001


def test_the_fatal_refusal_is_typed_as_process_fatal(monkeypatch) -> None:
    """The refusal behind a dead dispatcher carries the process-fatal type.

    A bare ``RuntimeError`` was enough to make the FanOut disable the sink
    -- but only for the Run that happened to be publishing, which is how a
    dead dispatcher got rediscovered and re-announced by every Run that
    followed. The refusal the FanOut needs in order to disable the sink for
    every Run and notify once must be recognizably the process-fatal class,
    on both paths a publish can touch.
    """
    from agent_alfred.events import ProcessFatalSinkError

    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    harness = Harness()
    harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    with pytest.raises(RuntimeError):
        harness.broker._dispatch_loop()  # noqa: SLF001 - the loop under test

    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r2",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id="fatal-refusal-event",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    # Prepare refuses too: framing an event nobody can deliver is work
    # wasted, and the refusal is the same typed signal.
    with pytest.raises(ProcessFatalSinkError):
        harness.broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=99,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    with pytest.raises(ProcessFatalSinkError):
        harness.broker.commit(None, sequenced)


def test_process_level_sink_disabled_notification_is_persistent_and_once(
    monkeypatch,
) -> None:
    """The process-level ``sink_disabled`` notice is one envelope, ever.

    A dispatcher that died in r1 used to be rediscovered by every later
    Run: r2's first publish failed the fatal check, was booked as that
    Run's sink failure, and published its own ``sink_disabled`` notice --
    and so did r3, and r4. The notice a process-level fact owes is one
    envelope, and the Runs after it must find the sink already gone instead
    of re-announcing it.
    """
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    dead = threading.Event()
    broker.bind_fatal_handler(lambda exc: dead.set())
    broker.start()
    handle = broker.connect(connection=FakeConnection())
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([broker, capture], process_instance_id=INSTANCE)

    def emit(run_id: str) -> None:
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=0.0,
                run_id=run_id,
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )

    emit("r1")
    assert dead.wait(5.0), "fatal handler never ran"
    assert handle.finished.wait(5.0), "connection was never asked to close"
    fanout.flush_barrier("r1")
    emit("r2")
    fanout.flush_barrier("r2")
    emit("r3")

    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1, "one process, one process-level notice"
    # The refusal persists: the broker stopped taking work and stays stopped
    # across every Run that followed the death.
    assert broker._stopping is True  # noqa: SLF001
    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert refused not in broker.connections
    # The healthy sink lost nothing: every Run's event still reached it.
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2", "r3"]


class _BrokenRing(ReplayRing):
    """A ring that cannot record anything.

    The ring is the process's only replay source for an active Run; one
    that raises is the dispatcher-dying class of failure, not one Run's
    transient. Subclassing the real ring keeps every other ring behaviour
    intact -- only the recording step is broken.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failure = RuntimeError("ring is broken")

    def observe_published(
        self, seq, entry, *, defer_retired_release: bool = False
    ):
        del seq, entry, defer_retired_release
        raise self.failure


class _BrokenStartupGuardRing(ReplayRing):
    """A real ring whose pre-registration replay read fails."""

    def startup_guard_cost(self, progress, through_seq):
        del progress, through_seq
        raise RuntimeError("startup replay guard is broken")


class _BrokenOpenReadRing(ReplayRing):
    """A real ring with one selectable pre-registration read fault."""

    def __init__(self) -> None:
        super().__init__()
        self._broken_read: str | None = None
        self.failure = RuntimeError("opening replay read is broken")

    def break_read(self, name: str) -> None:
        self._broken_read = name

    def _raise_if_broken(self, name: str) -> None:
        if self._broken_read == name:
            raise self.failure

    def reseed_boundary_seq(self):
        self._raise_if_broken("reseed_boundary_seq")
        return super().reseed_boundary_seq()

    def classify_seq(self, seq):
        self._raise_if_broken("classify_seq")
        return super().classify_seq(seq)

    def latest_complete_seq(self):
        self._raise_if_broken("latest_complete_seq")
        return super().latest_complete_seq()

    def oldest_seq(self):
        self._raise_if_broken("oldest_seq")
        return super().oldest_seq()

    def published_high_water_seq(self):
        self._raise_if_broken("published_high_water_seq")
        return super().published_high_water_seq()

    def replay_floor_seq(self):
        self._raise_if_broken("replay_floor_seq")
        return super().replay_floor_seq()


class _RecordingRealThreadSpawner(RealThreadSpawner):
    """Runs writers while retaining proof that admission spawned no extra one."""

    def __init__(self) -> None:
        self.targets: list = []

    def spawn(self, target):
        self.targets.append(target)
        return super().spawn(target)


@pytest.mark.parametrize(
    ("broken_read", "cursor"),
    [
        pytest.param("reseed_boundary_seq", None, id="classify-cursor-reseed"),
        pytest.param("classify_seq", cursor_for(0), id="classify-cursor-seq"),
        pytest.param("latest_complete_seq", None, id="latest-complete"),
        pytest.param("oldest_seq", "malformed", id="gap-oldest"),
        pytest.param(
            "published_high_water_seq", "malformed", id="gap-published-high-water"
        ),
        pytest.param("replay_floor_seq", "malformed", id="gap-replay-floor"),
        pytest.param(
            "published_high_water_seq", None, id="registration-published-high-water"
        ),
    ],
)
def test_every_opening_ring_read_failure_is_process_fatal_before_admission(
    broken_read: str,
    cursor: str | None,
) -> None:
    """Every ring read before HTTP 200 shares the process-fatal boundary."""
    ring = _BrokenOpenReadRing()
    spawn = _RecordingRealThreadSpawner()
    harness = Harness(ring=ring, spawn=spawn)
    broker = harness.broker
    capture, fanout = _capture_fanout(broker)
    broker.bind_fatal_handler(
        lambda _exc: fanout.emit(
            Notice(
                level="error",
                code="sink_disabled",
                detail=(("sink", broker.name), ("stage", "dispatch")),
            )
        )
    )
    existing = harness.connect()
    if broken_read == "replay_floor_seq":
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        broker.publish_state_patch(
            runtime_snapshot(
                state_revision=1,
                coordinator_state="running",
                active_run=ActiveRunSummary(
                    run_id="r1",
                    purpose="chat",
                    gateway="web",
                    phase="running",
                    session_id=None,
                    prompt_preview="hi",
                    started_at="2026-01-01T00:00:00Z",
                    recording_state=None,
                ),
            )
        )
    ring.break_read(broken_read)
    connection = FakeConnection()

    with pytest.raises(RuntimeError, match="opening replay read is broken") as raised:
        broker.prepare_stream(connection=connection, cursor=cursor)

    assert raised.value is ring.failure
    assert broker._fatal is ring.failure  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    assert existing.queue.close_requested is True
    assert existing.finished.wait(5.0), "existing stream was not closed"
    assert existing.thread is not None
    existing.thread.join(timeout=2.0)
    assert not existing.thread.is_alive(), "existing writer never exited"
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert len(spawn.targets) == 1, "failed admission leaked writer ownership"
    assert connection.writes == []

    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert refused not in broker.connections
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1


def test_a_non_gap_open_does_not_read_the_replay_floor() -> None:
    """Current Run recoverability is a gap-only opening fact."""
    ring = _BrokenOpenReadRing()
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker.publish_state_patch(
        runtime_snapshot(
            state_revision=1,
            coordinator_state="running",
            active_run=ActiveRunSummary(
                run_id="r1",
                purpose="chat",
                gateway="web",
                phase="running",
                session_id=None,
                prompt_preview="hi",
                started_at="2026-01-01T00:00:00Z",
                recording_state=None,
            ),
        )
    )
    ring.break_read("replay_floor_seq")

    handle = harness.connect()

    assert handle in harness.broker.connections


def test_a_startup_guard_read_failure_is_process_fatal_before_admission() -> None:
    """A broken replay source cannot leave HTTP free to send SSE 200."""
    ring = _BrokenStartupGuardRing()
    harness = Harness(ring=ring, spawn=RealThreadSpawner())
    broker = harness.broker
    capture, fanout = _capture_fanout(broker)
    broker.bind_fatal_handler(
        lambda _exc: fanout.emit(
            Notice(
                level="error",
                code="sink_disabled",
                detail=(("sink", broker.name), ("stage", "dispatch")),
            )
        )
    )
    existing = harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    connection = FakeConnection()

    with pytest.raises(RuntimeError, match="startup replay guard is broken"):
        broker.prepare_stream(connection=connection, cursor=cursor_for(0))

    assert broker._fatal is not None  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    assert existing.queue.close_requested is True
    assert existing.finished.wait(5.0), "existing stream was not closed"
    assert existing.thread is not None
    existing.thread.join(timeout=2.0)
    assert not existing.thread.is_alive(), "existing writer never exited"
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0

    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert refused not in broker.connections
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1


class _BrokenReplayFetchRing(ReplayRing):
    """A real ring whose writer-owned replay read fails."""

    def bounded_entries_after(self, progress, through_seq, budget):
        del progress, through_seq, budget
        raise RuntimeError("startup replay fetch is broken")


def test_a_writer_replay_read_failure_disables_the_process_sink(
    monkeypatch,
) -> None:
    """A writer cannot downgrade the active Run's only replay-source loss."""
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    ring = _BrokenReplayFetchRing()
    harness = Harness(ring=ring, spawn=RealThreadSpawner())
    broker = harness.broker
    capture, fanout = _capture_fanout(broker)
    broker.bind_fatal_handler(
        lambda _exc: fanout.emit(
            Notice(
                level="error",
                code="sink_disabled",
                detail=(("sink", broker.name), ("stage", "dispatch")),
            )
        )
    )
    existing = harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")

    replaying = harness.connect(cursor=cursor_for(0))
    assert replaying.finished.wait(5.0), "replay writer never observed the failure"

    assert broker._fatal is not None  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    assert existing.queue.close_requested is True
    assert existing.finished.wait(5.0), "existing stream was not closed"
    for handle in (existing, replaying):
        assert handle.thread is not None
        handle.thread.join(timeout=2.0)
        assert not handle.thread.is_alive(), "fatal writer never exited"
    assert broker.connections == ()

    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert refused not in broker.connections
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1


def test_a_broken_ring_is_a_process_fatal_not_a_run_local_error() -> None:
    """A ring failure inside commit escalates to the fatal state.

    Before the typed signal, a ring exception was just another sink error:
    the FanOut disabled the broker for the Run, the broker stayed up, kept
    accepting connections and kept handing out cursors a ring that cannot
    record could never honour -- and the next Run drew the same exception
    and published the same notice again. The ring failing is the dispatcher
    failing (#23 §9): the broker publishes the fatal state, refuses
    everything that would pour work into a broken recovery source, and the
    notice is said once.
    """
    ring = _BrokenRing()
    harness = Harness(ring=ring)
    broker = harness.broker
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([broker, capture], process_instance_id=INSTANCE)

    def emit(run_id: str) -> None:
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=0.0,
                run_id=run_id,
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )

    emit("r1")
    # The broker published the fatal state instead of limping on.
    assert broker._fatal is ring.failure  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    # New connections are refused, and so are state patches: both would
    # describe a world whose recovery source is gone.
    connection = FakeConnection()
    refused = broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert connection.closed is True
    assert broker.publish_state_patch(runtime_snapshot(state_revision=1)) is False
    # Said once: the next Run must not rediscover the broken ring and
    # publish the same notice again.
    fanout.flush_barrier("r1")
    emit("r2")
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2"], "the healthy sink keeps receiving events"


def test_ring_cost_control_exit_poison_closes_old_stream_before_a_higher_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-written recovery source is fatal before another seq can pass."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    old = harness.connect()
    drain_connection(old)
    cost_entered = threading.Event()
    release_cost = threading.Event()
    failure = KeyboardInterrupt("ring cost interrupted")
    real_cost = PreparedFrames.ingress_cost

    def gated_cost(item: PreparedFrames) -> frames.FrameCost:
        if item.seq == 1:
            cost_entered.set()
            assert release_cost.wait(3.0), "test did not release ring cost"
            raise failure
        return real_cost(item)

    monkeypatch.setattr(PreparedFrames, "ingress_cost", gated_cost)
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="ring-cost-publisher")
    publisher.start()
    assert cost_entered.wait(3.0), "publisher never entered ring cost"
    release_cost.set()
    publisher.join(3.0)
    assert not publisher.is_alive()

    assert caught == [failure]
    assert ring._generation % 2 == 1  # noqa: SLF001
    assert harness.broker._fatal is failure  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001

    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert ring._generation % 2 == 1  # noqa: SLF001
    assert harness.broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == [], (
        "the old stream must close before any id:2 can pass"
    )


@pytest.mark.parametrize("binding", ["with_sequence", "with_checkpoint"])
def test_binding_control_exit_fails_broker_closed_before_the_ring(
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    """Every broker-specific binding after FanOut seq allocation is fatal."""
    harness = Harness()
    old = harness.connect()
    drain_connection(old)
    failure = SystemExit(f"{binding} interrupted")

    def interrupt_binding(_item: PreparedFrames, *_args) -> PreparedFrames:
        raise failure

    monkeypatch.setattr(PreparedFrames, binding, interrupt_binding)

    with pytest.raises(SystemExit) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure
    assert harness.broker._fatal is failure  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001
    assert harness.broker._ring.published_high_water_seq() == 0  # noqa: SLF001

    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert harness.broker._ring.published_high_water_seq() == 0  # noqa: SLF001
    assert harness.broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == []


@pytest.mark.parametrize(
    "boundary", ["progress", "state_epoch", "run_event"],
)
@pytest.mark.parametrize(
    "failure_type", [KeyboardInterrupt, RuntimeError],
)
def test_pre_ring_failure_fences_every_later_publication(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failure_type: type[BaseException],
) -> None:
    """Every post-seq broker mutation belongs to one fatal commit domain."""
    harness = Harness()
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    failure = failure_type(f"{boundary} failed before ring publication")
    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r1",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id="pre-ring-failure",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    prepared = broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=1,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )

    if boundary == "progress":
        real_observe = broker_module.progress_module.observe

        def observe_then_fail(*args, **kwargs) -> None:
            real_observe(*args, **kwargs)
            raise failure

        monkeypatch.setattr(
            broker_module.progress_module, "observe", observe_then_fail
        )
    elif boundary == "state_epoch":

        class InterruptingEpoch(int):
            def __new__(cls):
                return super().__new__(cls, 0)

            def __add__(self, other):
                del other
                raise failure

        broker._state_epoch = InterruptingEpoch()  # noqa: SLF001
    else:
        real_note = broker._note_run_event  # noqa: SLF001

        def note_then_fail(event: SequencedEvent) -> None:
            real_note(event)
            raise failure

        monkeypatch.setattr(broker, "_note_run_event", note_then_fail)

    if isinstance(failure, Exception):
        with pytest.raises(ProcessFatalSinkError) as raised:
            broker.commit(prepared, sequenced)
        assert raised.value.__cause__ is failure
    else:
        with pytest.raises(KeyboardInterrupt) as raised:
            broker.commit(prepared, sequenced)
        assert raised.value is failure

    assert broker._fatal is failure  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    assert broker._ring.published_high_water_seq() == 0  # noqa: SLF001
    with pytest.raises(ProcessFatalSinkError):
        broker.commit(prepared, replace(sequenced, seq=2))
    assert broker._ring.published_high_water_seq() == 0  # noqa: SLF001

    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True
    assert replay_ids(drain_connection(old)) == []


@pytest.mark.parametrize("retirement", ["evict", "clear"])
def test_poisoned_eviction_cleanup_remains_owned_until_close_can_finish(
    monkeypatch: pytest.MonkeyPatch,
    retirement: str,
) -> None:
    """A control exit after logical eviction cannot orphan retired frames."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    if retirement == "clear":
        ring.budget = frames.FrameBudget(frames=1, encoded_bytes=1)
    failure = KeyboardInterrupt(f"{retirement} interrupted after retirement")
    real_drop = ring._entries.drop_prefix  # noqa: SLF001

    def interrupt_after_drop(count: int):
        retired = real_drop(count)
        assert retired is not None
        raise failure

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    release_calls = 0
    poisoned_lock_checks: list[tuple[bool, bool]] = []

    def gated_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls
        release_calls += 1
        broker_unlocked = harness.broker._lock.acquire(blocking=False)  # noqa: SLF001
        if broker_unlocked:
            harness.broker._lock.release()  # noqa: SLF001
        fanout_unlocked = harness.fanout._lock.acquire(blocking=False)  # noqa: SLF001
        if fanout_unlocked:
            harness.fanout._lock.release()  # noqa: SLF001
        poisoned_lock_checks.append((broker_unlocked, fanout_unlocked))
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release poisoned cleanup"
        real_release(start, count, through_seq)

    monkeypatch.setattr(
        ring._entries, "drop_prefix", interrupt_after_drop  # noqa: SLF001
    )
    monkeypatch.setattr(
        ring._entries, "release_retired", gated_release  # noqa: SLF001
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="poison-cleanup-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "poisoned retirement had no cleanup owner"

    try:
        assert harness.broker.close(timeout=0) is False
        with harness.broker._lock:  # noqa: SLF001
            assert harness.broker._ring_cleanup_failure is failure  # noqa: SLF001
            assert harness.broker._ring_cleanup_in_progress is True  # noqa: SLF001
    finally:
        release_cleanup.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert caught == [failure]

    assert harness.broker.close(timeout=0) is True
    # The asserted original control object owns the failing stack, whose
    # `_evict_while_over_budget` frame legitimately held the dropped entry as
    # a local. Clear only that diagnostic traceback before measuring whether
    # the ring/cleanup ownership itself released the frame.
    failure.__traceback__ = None
    caught.clear()
    gc.collect()
    assert release_calls == 1
    assert poisoned_lock_checks == [(True, True)]
    assert retired_ref() is None
    with harness.broker._lock:  # noqa: SLF001
        assert harness.broker._ring_cleanup_failure is None  # noqa: SLF001
        assert harness.broker._ring_cleanup_in_progress is False  # noqa: SLF001


@pytest.mark.parametrize("owner", ["commit", "ring"])
def test_cleanup_return_boundary_requeues_without_an_immortal_claim(
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
) -> None:
    """Control after callback return leaves an identity-claimable retry."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    release_calls = 0
    real_release = ring._entries.release_retired  # noqa: SLF001

    def count_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls
        release_calls += 1
        real_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", count_release)  # noqa: SLF001
    original = KeyboardInterrupt(f"{owner} publication interrupted")
    secondary = SystemExit(f"{owner} cleanup return interrupted")
    boundary_armed = True

    def interrupt_return(actual_owner: str) -> None:
        nonlocal boundary_armed
        if actual_owner == owner and boundary_armed:
            boundary_armed = False
            raise secondary

    monkeypatch.setattr(
        harness.broker, "_cleanup_callback_returned", interrupt_return
    )
    if owner == "ring":
        real_drop = ring._entries.drop_prefix  # noqa: SLF001

        def fail_after_drop(count: int):
            real_drop(count)
            raise original

        monkeypatch.setattr(ring._entries, "drop_prefix", fail_after_drop)  # noqa: SLF001
    else:
        original = secondary

    with pytest.raises(type(original)) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is original

    if owner == "commit":
        with harness.broker._commit_cleanup_lock:  # noqa: SLF001
            debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
            assert len(debts) == 1
            assert debts[0].ready is True
            assert debts[0].claim is None
            assert debts[0].in_progress is False
    else:
        with harness.broker._lock:  # noqa: SLF001
            assert harness.broker._ring_cleanup_failure is original  # noqa: SLF001
            assert harness.broker._ring_cleanup_ready is True  # noqa: SLF001
            assert harness.broker._ring_cleanup_claim is None  # noqa: SLF001
            assert harness.broker._ring_cleanup_in_progress is False  # noqa: SLF001

    assert harness.broker.close(timeout=0) is True
    original.__traceback__ = None
    secondary.__traceback__ = None
    del raised
    gc.collect()
    assert release_calls == 1
    assert retired_ref() is None


def test_ring_return_after_effect_keeps_retirement_until_broker_owns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ring-side backup closes return-to-ledger ownership transfer."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    failure = SystemExit("observe returned then interrupted")
    real_observe = ring.observe_published

    def interrupt_after_return(
        seq: int,
        entry: PreparedFrames | None,
        *,
        defer_retired_release: bool = False,
    ):
        result = real_observe(
            seq,
            entry,
            defer_retired_release=defer_retired_release,
        )
        if seq == 2:
            raise failure
        return result

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()

    def gated_release(start: int, count: int, through_seq: int) -> None:
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release backup cleanup"
        real_release(start, count, through_seq)

    monkeypatch.setattr(ring, "observe_published", interrupt_after_return)
    monkeypatch.setattr(
        ring._entries, "release_retired", gated_release  # noqa: SLF001
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="ring-return-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "ring return gap lost its backup owner"

    try:
        assert ring._generation % 2 == 0  # noqa: SLF001
        assert harness.broker._fatal is failure  # noqa: SLF001
        assert harness.broker.close(timeout=0) is False
    finally:
        release_cleanup.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert caught == [failure]

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    assert retired_ref() is None
    assert ring._deferred_retired == {}  # noqa: SLF001


def test_ring_ack_after_effect_keeps_the_broker_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker ledger registration precedes the ring backup acknowledgement."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    failure = KeyboardInterrupt("retirement ack interrupted after effect")
    real_ack = AppendResult.acknowledge_retired_transfer
    interrupt_ack = True

    def ack_then_interrupt(result: AppendResult) -> None:
        nonlocal interrupt_ack
        real_ack(result)
        if result._retired is not None and interrupt_ack:  # noqa: SLF001
            interrupt_ack = False
            raise failure

    real_release = ring._entries.release_retired  # noqa: SLF001
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()

    def gated_release(start: int, count: int, through_seq: int) -> None:
        cleanup_entered.set()
        assert release_cleanup.wait(3.0), "test did not release ack cleanup"
        real_release(start, count, through_seq)

    monkeypatch.setattr(
        AppendResult, "acknowledge_retired_transfer", ack_then_interrupt
    )
    monkeypatch.setattr(
        ring._entries, "release_retired", gated_release  # noqa: SLF001
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="ring-ack-publisher")
    publisher.start()
    assert cleanup_entered.wait(3.0), "ack interruption lost broker cleanup"

    try:
        assert harness.broker._fatal is failure  # noqa: SLF001
        assert ring._deferred_retired == {}  # noqa: SLF001
        assert harness.broker.close(timeout=0) is False
    finally:
        release_cleanup.set()
        publisher.join(3.0)
    assert not publisher.is_alive()
    assert caught == [failure]

    assert harness.broker.close(timeout=0) is True
    gc.collect()
    assert retired_ref() is None


def test_ring_backup_and_broker_debt_share_one_retirement_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two durable references may not execute one physical release concurrently."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True

    failure = KeyboardInterrupt("ring acknowledgement interrupted before effect")
    real_ack = AppendResult.acknowledge_retired_transfer
    ack_armed = True

    def interrupt_ack_once(result: AppendResult) -> None:
        nonlocal ack_armed
        if ack_armed and result._retired is not None:  # noqa: SLF001
            ack_armed = False
            raise failure
        real_ack(result)

    monkeypatch.setattr(
        AppendResult, "acknowledge_retired_transfer", interrupt_ack_once
    )
    real_release = ring._entries.release_retired  # noqa: SLF001
    first_release_entered = threading.Event()
    second_release_entered = threading.Event()
    allow_release = threading.Event()
    release_calls = 0
    active_releases = 0
    max_active_releases = 0
    count_lock = threading.Lock()

    def gated_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls, active_releases, max_active_releases
        with count_lock:
            release_calls += 1
            active_releases += 1
            max_active_releases = max(max_active_releases, active_releases)
            if release_calls == 1:
                first_release_entered.set()
            else:
                second_release_entered.set()
        try:
            assert allow_release.wait(3.0), "test did not release retirement"
            real_release(start, count, through_seq)
        finally:
            with count_lock:
                active_releases -= 1

    monkeypatch.setattr(ring._entries, "release_retired", gated_release)  # noqa: SLF001
    publish_errors: list[BaseException] = []

    def publish() -> None:
        try:
            harness.emit(RunStarted(purpose="chat"), run_id="r1")
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            publish_errors.append(exc)

    publisher = threading.Thread(target=publish, name="ring-backup-release")
    publisher.start()
    assert first_release_entered.wait(3.0), "settlement never claimed ring backup"

    close_results: list[bool] = []
    close_done = threading.Event()

    def close() -> None:
        close_results.append(harness.broker.close(timeout=0))
        close_done.set()

    closer = threading.Thread(target=close, name="broker-debt-release")
    closer.start()
    try:
        assert close_done.wait(1.0), (
            "close entered the same physical release instead of reporting busy"
        )
        assert second_release_entered.is_set() is False
        assert close_results == [False]
    finally:
        allow_release.set()
        publisher.join(3.0)
        closer.join(3.0)

    assert not publisher.is_alive()
    assert not closer.is_alive()
    assert publish_errors == [failure]
    assert release_calls == 1
    assert max_active_releases == 1
    assert harness.broker.close(timeout=0) is True


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize("boundary", ["identity", "registration"])
def test_cleanup_ledger_handoff_control_exit_keeps_a_claimable_owner(
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
    boundary: str,
) -> None:
    """Ring backup and broker ledger overlap across the registration return."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    broker = harness.broker
    failure = SystemExit(
        f"cleanup {boundary} interrupted {effect} effect"
    )
    if boundary == "identity":
        real_get_ident = broker_module.threading.get_ident
        interrupt_identity = True

        def interrupt_get_ident() -> int:
            nonlocal interrupt_identity
            if interrupt_identity:
                interrupt_identity = False
                raise failure
            return real_get_ident()

        monkeypatch.setattr(
            broker_module.threading, "get_ident", interrupt_get_ident
        )
    else:
        real_retain = broker._retain_returned_commit_cleanup  # noqa: SLF001

        def interrupt_registration(release_retired):
            if effect == "before":
                raise failure
            real_retain(release_retired)
            raise failure

        monkeypatch.setattr(
            broker, "_retain_returned_commit_cleanup", interrupt_registration
        )

    with pytest.raises(SystemExit) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure
    assert broker._fatal is failure  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001

    failure.__traceback__ = None
    del raised
    gc.collect()
    assert retired_ref() is None
    assert ring._deferred_retired == {}  # noqa: SLF001
    with broker._commit_cleanup_lock:  # noqa: SLF001
        assert broker._pending_commit_cleanup == {}  # noqa: SLF001
    assert broker.close(timeout=0) is True


@pytest.mark.parametrize("effect", ["before", "after"])
def test_post_commit_construction_control_exit_keeps_a_claimable_owner(
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
) -> None:
    """A broker owner becomes settleable before PostCommit crosses its return."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    failure = KeyboardInterrupt(f"PostCommit interrupted {effect} effect")
    real_post_commit = broker_module.PostCommit
    interrupt_once = True

    def interrupt_construction(*args, **kwargs):
        nonlocal interrupt_once
        if interrupt_once:
            interrupt_once = False
            if effect == "before":
                raise failure
            real_post_commit(*args, **kwargs)
            raise failure
        return real_post_commit(*args, **kwargs)

    monkeypatch.setattr(broker_module, "PostCommit", interrupt_construction)

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure
    assert harness.broker._fatal is failure  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001

    failure.__traceback__ = None
    del raised
    gc.collect()
    assert retired_ref() is None
    assert ring._deferred_retired == {}  # noqa: SLF001
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
    assert harness.broker.close(timeout=0) is True


def test_commit_return_control_exit_claims_the_latest_normal_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FanOut can settle a PostCommit lost after the sink returned it."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    harness.emit(RunStarted(purpose="chat"), run_id="seed")
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    failure = KeyboardInterrupt("commit returned before caller received PostCommit")
    real_commit = harness.broker.commit

    def return_then_interrupt(prepared: object, event: SequencedEvent):
        post_commit = real_commit(prepared, event)
        if event.seq == 2:
            raise failure
        return post_commit

    monkeypatch.setattr(harness.broker, "commit", return_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert raised.value is failure

    failure.__traceback__ = None
    del raised
    gc.collect()
    assert retired_ref() is None
    assert ring._deferred_retired == {}  # noqa: SLF001
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
    assert harness.broker.close(timeout=0) is True


@pytest.mark.parametrize("boundary", ["store", "loop"])
def test_fanout_async_exit_after_commit_effect_still_settles_owner(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Caller handoff exits cannot strand owners or skip a prepared sibling."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [harness.broker, tail], process_instance_id=INSTANCE
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded

    failure = KeyboardInterrupt(f"FanOut {boundary} boundary interrupted")
    after_commit_seqs: list[int] = []
    boundary_entered = threading.Event()
    release_boundary = threading.Event()
    armed = True
    real_record = events_module._record_post_commit  # noqa: SLF001

    def interrupt_record(actions, sink, action) -> None:
        nonlocal armed
        real_record(actions, sink, action)
        if armed and sink is harness.broker:
            armed = False
            boundary_entered.set()
            assert release_boundary.wait(3.0), "test did not release FanOut"
            raise failure

    def interrupt_loop() -> None:
        nonlocal armed
        if armed:
            armed = False
            boundary_entered.set()
            assert release_boundary.wait(3.0), "test did not release FanOut"
            raise failure

    monkeypatch.setattr(
        events_module,
        "_record_post_commit" if boundary == "store" else "_finish_commit_iteration",
        interrupt_record if boundary == "store" else interrupt_loop,
    )
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            fanout.emit_linearized(
                RunStarted(purpose="chat"),
                envelope,
                boundary=nullcontext(),
                after_commit=lambda event: after_commit_seqs.append(event.seq),
            )
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="fanout-boundary-publisher")
    publisher.start()
    assert boundary_entered.wait(3.0), "publisher never reached FanOut boundary"
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        debts = tuple(harness.broker._pending_commit_cleanup.values())  # noqa: SLF001
        assert len(debts) == 1 and debts[0].ready is True
    release_boundary.set()
    publisher.join(3.0)

    assert not publisher.is_alive()
    assert caught == [failure]
    assert armed is False
    assert after_commit_seqs == [2]
    assert [event.seq for event in tail.events] == [1, 2]
    failure.__traceback__ = None
    caught.clear()
    gc.collect()
    assert retired_ref() is None
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
    assert harness.broker.close(timeout=0) is True


def test_fanout_boundary_exit_cannot_skip_post_commit_settlement() -> None:
    """An outer publication boundary exits before cleanup, but cannot strand it."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    harness = Harness(ring=ring)
    tail = CapturingSink(name="tail")
    fanout = FanOutSink(
        [harness.broker, tail], process_instance_id=INSTANCE
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)
    fanout.emit(RunStarted(purpose="chat"), envelope)
    assert harness.broker.deliver_next(timeout=0) is True
    seeded = ring.entries_after(0)
    assert seeded is not None
    retired_ref = weakref.ref(seeded[0])
    del seeded
    failure = SystemExit("publication boundary exit interrupted")
    after_commit_seqs: list[int] = []

    class InterruptingBoundary:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            raise failure

    with pytest.raises(SystemExit) as raised:
        fanout.emit_linearized(
            RunStarted(purpose="chat"),
            envelope,
            boundary=InterruptingBoundary(),
            after_commit=lambda event: after_commit_seqs.append(event.seq),
        )

    assert raised.value is failure
    assert after_commit_seqs == [2]
    assert [event.seq for event in tail.events] == [1, 2]
    failure.__traceback__ = None
    del raised
    gc.collect()
    assert retired_ref() is None
    with harness.broker._commit_cleanup_lock:  # noqa: SLF001
        assert harness.broker._pending_commit_cleanup == {}  # noqa: SLF001
    assert harness.broker.close(timeout=0) is True


# --- overflow wakes the dispatcher once, not once per overflow --------------


def test_overflow_kicks_merge_and_stay_bounded() -> None:
    """A pending kick is a bit, not a queue item per overflow.

    Every must-deliver overflow that cannot fit the ingress raises the
    disconnect generation and needs the dispatcher woken -- once. One
    unwoken kick is a liveness bug; one kick *per* overflow is the one
    item that grows without bound behind a stalled dispatcher, and it is
    the control item that would delay the close's own stop sentinel.
    The generation may grow without limit; the number of pending kicks
    may not.
    """
    harness = Harness(
        ingress_budget=frames.FrameBudget(2, 32 * 1024 * 1024)
    )
    broker = harness.broker
    for _ in range(2):
        harness.emit(RunStarted(purpose="chat"))
    assert broker._ingress._items.qsize() == 2  # noqa: SLF001 - billed data
    for _ in range(1000):
        harness.emit(RunStarted(purpose="chat"))
    assert broker._disconnect_generation == 1000
    # Two billed data items and ONE pending kick, whatever the overflow
    # count.
    assert broker._ingress._items.qsize() == 3  # noqa: SLF001
    # Consuming the kick re-arms it: the next overflow may produce the next
    # kick, still one at a time.
    for _ in range(2):
        assert broker.deliver_next(timeout=0.2) is True  # the data
    assert broker.deliver_next(timeout=0.2) is True  # the kick
    for _ in range(2):
        harness.emit(RunStarted(purpose="chat"))  # the two frames fit again
    for _ in range(10):
        harness.emit(RunStarted(purpose="chat"))  # overflows again
    assert broker._disconnect_generation == 1010
    assert broker._ingress._items.qsize() == 3  # noqa: SLF001 - 2 data + 1 kick
    # And the close's stop sentinel is never stuck behind unbounded control
    # items: the whole queue is bounded by billed data plus the two
    # sentinels.
    assert broker.close(timeout=1.0) is True
    assert broker._ingress._items.qsize() <= 3  # noqa: SLF001


def test_event_overflow_survives_a_before_effect_kick_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher rechecks committed generation debt without a retry owner."""
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        ingress_budget=frames.FrameBudget(frames=1, encoded_bytes=1),
        spawn=RealThreadSpawner().spawn,
    )
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    broker.start()
    handle = broker.connect(connection=FakeConnection())
    real_put_kick = broker._ingress.put_kick  # noqa: SLF001
    attempts = 0

    def interrupt_first_kick() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt("event kick interrupted before enqueue")
        real_put_kick()

    monkeypatch.setattr(broker._ingress, "put_kick", interrupt_first_kick)  # noqa: SLF001
    try:
        with pytest.raises(KeyboardInterrupt, match="before enqueue"):
            fanout.emit(RunStarted(purpose="chat"))
        assert handle.finished.wait(2.0), (
            "the dispatcher slept through committed disconnect generation debt"
        )
        assert handle.queue.close_requested is True
    finally:
        broker.close(timeout=2.0)


def test_event_overflow_kicks_coalesce_after_effect_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent event publishers cannot accumulate after-effect kicks."""
    harness = Harness(
        ingress_budget=frames.FrameBudget(frames=1, encoded_bytes=1)
    )
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    owned_queue = broker._ingress._items  # noqa: SLF001

    class PutThenInterruptQueue:
        def __init__(self, failures: int) -> None:
            self.failures = failures

        def put(self, item) -> None:
            owned_queue.put(item)
            if isinstance(item, _IngressKick) and self.failures:
                self.failures -= 1
                raise KeyboardInterrupt("event kick interrupted after enqueue")

        def __getattr__(self, name):
            return getattr(owned_queue, name)

    proxy = PutThenInterruptQueue(failures=4)
    monkeypatch.setattr(broker._ingress, "_items", proxy)  # noqa: SLF001

    for index in range(4):
        with pytest.raises(KeyboardInterrupt, match="after enqueue"):
            harness.emit(RunStarted(purpose="chat"), run_id=f"r{index}")

    assert proxy.qsize() == 1, "after-effect event kicks grew without bound"
    assert broker.deliver_next(timeout=0) is True
    assert old.queue.close_requested is True
    assert broker.deliver_next(timeout=0) is False


# --- a transient's seq names its publication, not its delivery --------------


def test_a_transient_published_before_a_connection_registers_is_not_delivered() -> None:
    """Reconnect does not resurrect a half-finished attempt.

    A transient published before a connection registers is exactly the
    in-flight delta ADR-0013 says a reconnect must not receive: the client
    dropped it on purpose and will be handed the attempt's terminal
    snapshot instead. The registration boundary is the newest *published*
    seq -- transients included -- so a preregistration transient is skipped
    like any other event the replay already covers, while a transient
    published after registration is delivered live, still without an
    ``id:`` on the wire.
    """
    harness = Harness()
    # The delta is in the ingress, but the dispatcher has not run: the
    # registration below happens after the publication.
    harness.emit(BlockDelta(attempt_id="a1", index=0, text="half an attempt"))
    handle = harness.connect()
    drain_dispatcher(harness)
    wire = b"".join(item.wire_bytes() for item in drain_connection(handle))
    assert b'"event":"block.delta"' not in wire
    # A transient published after the registration is live delivery.
    harness.emit(BlockDelta(attempt_id="a1", index=0, text="still going"))
    drain_dispatcher(harness)
    items = drain_connection(handle)
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' in wire
    # And a transient never carries a checkpoint: no ``id:``, then or now.
    assert all(not item.id_line for item in items)


def test_mixed_backlogs_respect_the_registration_boundary() -> None:
    """First connection: backlog events are the snapshot's job, not live.

    A first connection primed with the replay ring holds nothing, so every
    backlog event -- replayable or transient -- is behind its published
    boundary and none is delivered live. What arrives afterwards is
    delivered, replayables with their checkpoints, transients without.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"))  # seq 1, replayable
    harness.emit(RunStarted(purpose="chat"))  # seq 2, replayable
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 3, transient
    handle = harness.connect()
    drain_dispatcher(harness)
    items = drain_connection(handle)
    assert replay_ids(items) == []
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 4, transient
    harness.emit(RunStarted(purpose="chat"))  # seq 5, replayable
    drain_dispatcher(harness)
    items = drain_connection(handle)
    # Only the replayable event carries a checkpoint.
    assert replay_ids(items) == [5]
    wire = b"".join(item.wire_bytes() for item in items)
    assert wire.count(b'"event":"block.delta"') == 1


def test_a_reconnect_skips_preregistration_transients_too() -> None:
    """The boundary is the same on a reconnect from a valid checkpoint.

    The replay tail in the opening stream covers the replayable backlog;
    the published boundary covers the transient sitting behind it in the
    ingress. Delivering the ingress after the connection registered must
    therefore produce neither a duplicate replayable nor a resurrected
    delta.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"))  # seq 1
    harness.emit(RunStarted(purpose="chat"))  # seq 2
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 3, still in ingress
    handle = harness.connect(cursor=cursor_for(1))
    drain_dispatcher(harness)
    items = drain_connection(handle)
    # The replay tail, exactly once, and no preregistration transient.
    assert replay_ids(items) == [2]
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire


# --- fan-out asks for Session truth only when a patch needs it --------------


def test_a_domain_event_fan_out_never_queries_session_validity() -> None:
    """A domain event has one wire view, independent of its Session.

    Session validity is database-backed in production. Asking it here would
    put one SQL query per connection on the sole dispatcher even though the
    answer cannot change the domain event that any connection receives.
    """
    harness = Harness()
    handle = harness.connect(session_id="s1")
    drain_connection(handle)

    def unexpected_query(_session_id: str | None) -> bool:
        raise AssertionError("domain event fan-out queried Session validity")

    harness.broker.bind_session_check(unexpected_query)
    harness.emit(RunStarted(purpose="chat"))
    harness.deliver()

    wire = b"".join(item.wire_bytes() for item in drain_connection(handle))
    assert b'"event":"run.started"' in wire


def test_a_transient_domain_event_carries_its_publication_seq_on_the_wire() -> None:
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)

    event = harness.emit(BlockDelta(attempt_id="a1", text="hello"))
    harness.deliver()

    [item] = drain_connection(handle)
    wire = item.wire_bytes()
    payload = json.loads(wire.split(b"data: ", 1)[1].split(b"\n", 1)[0])
    assert payload["seq"] == event.seq
    assert b"\nid: " not in wire


def test_a_replayable_domain_event_carries_the_same_seq_as_its_checkpoint() -> None:
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)

    event = harness.emit(RunStarted(purpose="chat"))
    harness.deliver()

    [item] = drain_connection(handle)
    [wire] = item.wire_frames()
    payload = json.loads(wire.split(b"data: ", 1)[1].split(b"\n", 1)[0])
    assert payload["seq"] == event.seq
    assert wire.endswith(b"id: %s:%d\n\n" % (INSTANCE.encode(), event.seq))


def test_every_chunk_carries_one_seq_and_only_the_last_advances_the_cursor() -> None:
    harness = Harness(max_frame_bytes=512)
    handle = harness.connect()
    drain_connection(handle)

    event = harness.emit(
        RunStarted(purpose="chat", user_message=user_message_with("x" * 4000))
    )
    harness.deliver()

    [item] = drain_connection(handle)
    wire = item.wire_frames()
    assert len(wire) > 1
    assert {_chunk_meta(record)["seq"] for record in wire} == {event.seq}
    assert all(b"\nid: " not in record for record in wire[:-1])
    assert wire[-1].endswith(b"id: %s:%d\n\n" % (INSTANCE.encode(), event.seq))


def test_a_patch_fan_out_keeps_each_sessions_validity_view() -> None:
    """A patch still carries the database-backed fact for its connection."""
    harness = Harness()
    valid = harness.connect(session_id="s1")
    invalid = harness.connect(session_id="gone")
    drain_connection(valid)
    drain_connection(invalid)

    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _patch_payload(valid)["session_valid"] is True
    assert _patch_payload(invalid)["session_valid"] is False


def test_patch_dispatch_uses_only_each_connections_preflight_validity() -> None:
    """The dispatcher neither queries storage nor revises a proven verdict."""
    harness = Harness()
    first = harness.connect(session_id="s1")
    second = harness.connect(session_id="s1")
    other = harness.connect(session_id="gone")
    for handle in (first, second, other):
        drain_connection(handle)

    def unexpected_query(_session_id: str | None) -> SessionValidity:
        raise AssertionError("state-patch dispatch queried Session storage")

    harness.broker.bind_session_check(unexpected_query)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _patch_payload(first)["session_valid"] is True
    assert _patch_payload(second)["session_valid"] is True
    assert _patch_payload(other)["session_valid"] is False


def test_a_patch_keeps_each_connections_last_proven_validity_when_unavailable(
) -> None:
    """Storage loss cannot rewrite or erase a fact established at connect."""
    answer = {"value": "valid"}
    harness = Harness()
    harness.broker.bind_session_check(lambda _session_id: answer["value"])
    handle = harness.connect(session_id="s1")
    drain_connection(handle)

    answer["value"] = "unavailable"
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _patch_payload(handle)["session_valid"] is True
    assert handle in harness.broker.connections


# --- encoding never happens under the broker lock ---------------------------


class _GatedEncoder:
    """A patch encoder the test can park mid-JSON.

    It stands in for the pure, lock-free encoding half of ADR-0015: slow is
    legal, holding the broker lock while slow is not. Every call records
    the ``session_valid`` answer it encoded for.
    """

    def __init__(self, real):
        self._real = real
        self.lock = threading.Lock()
        self.encodings: list[bool] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, snapshot, step, session_valid):
        with self.lock:
            self.encodings.append(session_valid)
        self.entered.set()
        assert self.release.wait(5.0), "test never released the patch encoder"
        return self._real(snapshot, step, session_valid)

    def calls_for(self, session_valid: bool) -> int:
        with self.lock:
            return self.encodings.count(session_valid)


def test_patch_encoding_does_not_block_event_commits(monkeypatch) -> None:
    """A slow preparation runs before enqueue and outside the broker lock.

    The dispatcher may only offer an already prepared frame. Preparation is
    allowed to be slow, but its publisher cannot hold the commit lock while
    it runs: a domain event committed while the encoder is parked must go
    straight through.
    """
    encoder = _GatedEncoder(_patch_frames)
    # Startup uses the same encoder. Let that call finish, then arm the gate
    # only for the patch publisher below.
    encoder.release.set()
    monkeypatch.setattr(broker_module, "_patch_frames", encoder)
    harness = Harness(spawn=RealThreadSpawner())
    connection = ObservedConnection()
    handle = harness.connect(connection=connection)
    try:
        connection.wait_for_bytes(b"event: state_patch")
    except BaseException:
        assert harness.broker.close(timeout=2.0) is True
        raise
    encoder.entered.clear()
    encoder.release.clear()
    publish_done = threading.Event()
    answers: list[bool] = []
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            answers.append(
                harness.broker.publish_state_patch(
                    runtime_snapshot(state_revision=1)
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            publish_done.set()

    emitter: threading.Thread | None = None
    publisher = threading.Thread(target=publish, daemon=True)
    publisher.start()
    try:
        assert encoder.entered.wait(2.0), "patch encoding never started"
        commit_done = threading.Event()

        def commit() -> None:
            harness.emit(RunStarted(purpose="chat"))
            commit_done.set()

        emitter = threading.Thread(target=commit, daemon=True)
        emitter.start()
        # The encoder is still parked; the commit must not be queued behind
        # it.
        assert commit_done.wait(5.0), "commit waited for the patch encoding"
    finally:
        encoder.release.set()
        publisher.join(timeout=5.0)
        if emitter is not None:
            emitter.join(timeout=5.0)
            assert not emitter.is_alive(), "event publisher did not exit"
        assert harness.broker.close(timeout=2.0) is True
    assert not publisher.is_alive(), "patch publisher did not exit"
    assert publish_done.is_set()
    assert errors == []
    assert answers == [True]
    assert handle.finished.is_set(), "the test retained its stream writer"


class _GatedCost:
    """The publisher's lock-free cost probe, parked by the test.

    It stands in at the exact point the publish path measures a patch's
    ingress cost: the Step is already captured when it runs, so parking here
    holds the whole capture-to-commit window open while the rest of the
    world moves. The barrier is the rendezvous -- the parked publisher is
    the first party, the test that has finished moving the world is the
    second -- so the interleaving is decided by the test, not the scheduler.
    """

    def __init__(self, real, barrier: threading.Barrier) -> None:
        self._real = real
        self._barrier = barrier
        self.parked = threading.Event()

    def __call__(self, snapshot, step) -> int:
        if self.parked.is_set():
            # Only the first publish parks: the one the test started before
            # moving the world. Any later cost measurement -- the test's own
            # newer publish, or a recapture lap -- passes straight through.
            return self._real(snapshot, step)
        self.parked.set()
        self._barrier.wait()
        return self._real(snapshot, step)


def _patches_received(handle) -> list[dict]:
    """Every state_patch frame this connection has been handed, in order."""
    patches = []
    for item in drain_connection(handle):
        if not isinstance(item, PreparedFrames):
            # A sweep's close sentinel is a mark, not a frame the wire saw.
            continue
        wire = item.wire_bytes()
        if b"event: state_patch" in wire:
            patches.append(_decode_patch(wire))
    return patches


def _park_a_publisher(
    harness, monkeypatch, stale_snapshot
) -> tuple[threading.Barrier, threading.Thread, list[bool]]:
    """Start one publisher and hold it mid-encode, after its capture.

    Returns the gate's barrier, the publisher thread, and the list its
    return answer lands in. The test moves the world while it is parked and
    then joins the barrier as the second party; a failing test aborts the
    barrier instead, so the parked thread can never outlive the test that
    parked it.
    """
    barrier = threading.Barrier(2)
    gate = _GatedCost(broker_module._patch_cost, barrier)
    monkeypatch.setattr(broker_module, "_patch_cost", gate)
    answers: list[bool] = []

    def publisher() -> None:
        answers.append(harness.broker.publish_state_patch(stale_snapshot))

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    assert gate.parked.wait(5.0), "the publisher never reached its encoding"
    return barrier, thread, answers


def test_stale_step_never_publishes_after_newer_revision(monkeypatch) -> None:
    """A patch encoded while the world moved must not ship the old Step.

    The publisher captures its Step under the lock and measures the patch's
    cost outside it. A progress-advancing domain event and a newer
    authoritative snapshot both land inside that window; a publisher that
    commits unconditionally on return would publish the captured-old Step
    under the new revision, *after* the newer facts, and move ``_latest``
    backwards past them. The captured facts must still hold at the
    linearization point, and a snapshot the authority has already overtaken
    must be refused -- patches are absolute replacements, so publishing one
    is how an old domain fact ends up behind a newer one.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(StepStarted(step_index=0), run_id="r1")
    active = ActiveRunSummary(
        run_id="r1",
        purpose="chat",
        gateway="web",
        phase="running",
        session_id=None,
        prompt_preview="hi",
        started_at="2026-01-01T00:00:00Z",
        recording_state=None,
        current_step=0,
    )
    overtaken = runtime_snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = runtime_snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=replace(active, current_step=1),
    )
    barrier, thread, answers = _park_a_publisher(harness, monkeypatch, overtaken)
    try:
        harness.emit(StepStarted(step_index=1), run_id="r1")
        assert harness.broker.publish_state_patch(newer), (
            "the newer publish must not wait behind the parked one"
        )
        barrier.wait(timeout=5.0)  # the interleave is done: release it
    except BaseException:
        barrier.abort()
        raise
    thread.join(timeout=5.0)
    assert answers == [False], (
        "a snapshot the authority had overtaken was published anyway"
    )
    assert harness.broker._latest is newer, "_latest moved backwards"
    drain_dispatcher(harness)
    patches = _patches_received(handle)
    assert patches, "no state patch was published at all"
    revisions = [patch["state_revision"] for patch in patches]
    assert revisions == sorted(revisions), f"patch order regressed: {revisions}"
    seen_newer = False
    for patch in patches:
        if patch["state_revision"] == newer.state_revision:
            seen_newer = True
        if seen_newer:
            step = patch["step"]
            assert step is not None and step["step_index"] == 1, (
                f"a stale Step shipped after the newer facts: {patch}"
            )
    assert seen_newer


def test_connection_never_regresses_to_stale_absolute_replacement(
    monkeypatch,
) -> None:
    """Applying the patches a connection receives, in order, never goes back.

    A patch is an absolute replacement (ADR-0025): whatever it carries
    becomes the connection's whole view of the state. A stale Step published
    after newer facts would walk the connection backwards -- the exact
    outcome the revision exists to prevent -- so the patch stream a
    connection reads must advance monotonically and end on the latest
    authoritative facts, whatever the publisher's encoding window held.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(StepStarted(step_index=0), run_id="r1")
    active = ActiveRunSummary(
        run_id="r1",
        purpose="chat",
        gateway="web",
        phase="running",
        session_id=None,
        prompt_preview="hi",
        started_at="2026-01-01T00:00:00Z",
        recording_state=None,
        current_step=0,
    )
    overtaken = runtime_snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = runtime_snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=replace(active, current_step=1),
    )
    barrier, thread, _answers = _park_a_publisher(harness, monkeypatch, overtaken)
    try:
        harness.emit(StepStarted(step_index=1), run_id="r1")
        assert harness.broker.publish_state_patch(newer)
        barrier.wait(timeout=5.0)
    except BaseException:
        barrier.abort()
        raise
    thread.join(timeout=5.0)
    drain_dispatcher(harness)
    patches = _patches_received(handle)
    assert patches, "the connection saw no state patch"
    revisions = [patch["state_revision"] for patch in patches]
    assert revisions == sorted(revisions), (
        f"absolute replacements walked the connection backwards: {revisions}"
    )
    final = patches[-1]
    assert final["state_revision"] == harness.broker._latest.state_revision, (
        "the connection was left on a state that is no longer authoritative"
    )
    assert final["step"] is not None and final["step"]["step_index"] == 1, (
        f"the connection was left on a stale Step: {final}"
    )


def test_a_patch_is_prepared_once_per_session_validity_before_dispatch(
    monkeypatch,
) -> None:
    """One state is fully encoded before the dispatcher receives it.

    A patch is absolute: what two valid connections receive differs in
    nothing, and what a valid and an invalid connection receive differs in
    exactly one field. The publisher prepares those two immutable variants
    once; dispatch only selects one and offers the shared reference.
    """
    encoder = _GatedEncoder(_patch_frames)
    # Released throughout: connect's opening stream goes through the same
    # encoder now, and the counting -- not the parking -- is what this test
    # pins.
    encoder.release.set()
    monkeypatch.setattr(broker_module, "_patch_frames", encoder)
    harness = Harness()
    harness.connect(session_id="s1")
    harness.connect(session_id="s1")
    harness.connect(session_id="not-s1")  # the invalid answer
    encoder.encodings.clear()
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=2))
    assert encoder.calls_for(True) == 1
    assert encoder.calls_for(False) == 1

    def unexpected_encoding(*_args, **_kwargs):
        raise AssertionError("dispatcher encoded a state patch")

    monkeypatch.setattr(broker_module, "_patch_frames", unexpected_encoding)
    drain_dispatcher(harness)


def test_connect_encodes_its_startup_outside_the_lock(monkeypatch) -> None:
    """The opening stream is encoded lock-free, and honestly re-captured.

    A connect that encoded its startup patch inside the broker lock would
    block every commit for the length of its serialization. Encoding runs
    outside; the registration critical section then verifies the captured
    snapshot is still current and, if events moved the world meanwhile,
    recaptures and re-encodes -- so the stream the client finally gets
    names the state that was authoritative *at registration*.
    """
    real_encoder = _patch_frames
    encoder_entered = threading.Event()
    encoder_release = threading.Event()
    encoder_lock = threading.Lock()
    gate_claimed = False

    def gate_only_the_first_startup(snapshot, step, session_valid):
        nonlocal gate_claimed
        with encoder_lock:
            should_gate = not gate_claimed
            gate_claimed = True
        if should_gate:
            encoder_entered.set()
            assert encoder_release.wait(5.0), "test never released startup encoding"
        return real_encoder(snapshot, step, session_valid)

    monkeypatch.setattr(
        broker_module, "_patch_frames", gate_only_the_first_startup
    )
    harness = Harness(spawn=RealThreadSpawner())
    connect_done = threading.Event()
    descriptor: list[bytes] = []

    def do_connect() -> None:
        connection = ObservedConnection()
        handle = harness.broker.connect(connection=connection)
        connection.wait_for_count(3)
        descriptor.append(connection.written)
        handle.queue.stop()
        connect_done.set()

    connector = threading.Thread(target=do_connect, daemon=True)
    connector.start()
    try:
        assert encoder_entered.wait(2.0), "startup encoding never started"
        # While the startup is parked mid-encoding, the world moves: the
        # authoritative snapshot advances. A connect holding the lock here
        # would deadlock the publish; a connect that merely encoded outside
        # but registered the stale frame would ship revision 0.
        assert harness.broker.publish_state_patch(runtime_snapshot(state_revision=1)), (
            "publish waited for the startup encoding"
        )
    finally:
        encoder_release.set()
        assert connect_done.wait(5.0), "connect never finished"
        connector.join(timeout=5.0)
    wire = descriptor[0]
    assert b'"state_revision":1' in wire
    assert b'"state_revision":0' not in wire


def test_connect_refuses_after_bounded_startup_recaptures(monkeypatch) -> None:
    """A perpetually moving world cannot make connect spin forever.

    Every opening-patch encoding below advances one real authoritative state
    patch before registration can re-check its epoch. The old unbounded loop
    reaches a fifth encoding and has to be stopped externally; the bounded
    path instead returns the ordinary explicit refusal after exactly the
    shared capture budget, without ever registering or starting a writer.
    """
    harness = Harness()
    connection = FakeConnection()
    real_encoder = broker_module._patch_frames
    runaway = threading.Event()
    release_runaway = threading.Event()
    connect_done = threading.Event()
    signals: queue.Queue[str] = queue.Queue()
    captures = 0
    advancing = False
    answers = []
    errors: list[BaseException] = []

    def advancing_encoder(snapshot, step, session_valid):
        nonlocal captures, advancing
        if advancing:
            return real_encoder(snapshot, step, session_valid)
        captures += 1
        if captures > broker_module._MAX_PATCH_CAPTURES:
            runaway.set()
            signals.put("runaway")
            release_runaway.wait(5.0)  # anti-hang only; cleanup releases it
        encoded = real_encoder(snapshot, step, session_valid)
        advancing = True
        try:
            harness.broker.publish_state_patch(
                runtime_snapshot(state_revision=captures)
            )
        finally:
            advancing = False
        return encoded

    monkeypatch.setattr(broker_module, "_patch_frames", advancing_encoder)

    def connect() -> None:
        try:
            answers.append(harness.broker.connect(connection=connection))
        except BaseException as exc:  # preserve the worker's testimony
            errors.append(exc)
        finally:
            connect_done.set()
            signals.put("done")

    connector = threading.Thread(target=connect, daemon=True)
    connector.start()
    # Fixed code returns promptly. Old code deterministically announces that
    # it crossed the budget and parks, proving the pre-fix non-return without
    # leaving an infinite daemon consuming snapshots in the test process.
    signal = signals.get(timeout=5.0)
    bounded = signal == "done"
    if not bounded:
        assert runaway.is_set(), "connect neither returned nor crossed its budget"
    try:
        assert bounded, "connect exceeded the fixed startup capture budget"
    finally:
        if not bounded:
            # The old implementation needs an external close to escape its
            # unbounded loop. Close first, then release the parked encoder so
            # its next registration check observes the refusal.
            assert harness.broker.close(timeout=0.2) is True
            release_runaway.set()
        connector.join(timeout=5.0)

    assert not connector.is_alive()
    assert errors == []
    assert captures == broker_module._MAX_PATCH_CAPTURES
    assert len(answers) == 1
    refused = answers[0]
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused.writer is None
    assert refused not in harness.broker.connections
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []
    assert connection.written == b""
    assert connection.closed is True
    assert refused.queue.current_cost == frames.FrameCost(
        frames=0, encoded_bytes=0
    )
    assert refused.queue.close_requested is False

    # The rejected handle and its queue are not retained by the broker or a
    # never-started writer target after the caller releases its own answer.
    refused_ref = weakref.ref(refused)
    answers.clear()
    del refused
    gc.collect()
    assert refused_ref() is None


# --- the publish path never touches a connection ----------------------------


class _ProbingQueue:
    """A queue stand-in that refuses to be touched while it is armed.

    The publish path -- ``commit`` and ``publish_state_patch`` -- must cost
    the same whether one connection exists or a thousand: any traversal of
    the connection registry from the emitting thread, which is what closing
    a connection from there requires, trips this stub instead of silently
    charging the Run for the crowd.
    """

    def __init__(self) -> None:
        self.armed = True
        self.close_requests = 0

    def request_close(self, retry_ms: int | None = None) -> None:
        if self.armed:
            raise AssertionError("a connection was touched from the publish path")
        self.close_requests += 1

    def offer(self, item):
        del item
        return OfferOutcome(kind="accepted")

    def stop(self) -> None:
        return None


def _commit_direct(harness, seq: int, run_id: str):
    """One replayable event through ``commit`` alone, bypassing the fan-out.

    The fan-out catches a sink's commit failure and disables the sink; the
    behaviour under test is ``commit`` itself, so the event is committed by
    hand and anything it raises reaches the test.
    """
    envelope = EventEnvelope(
        ts=0.0,
        run_id=run_id,
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id=f"direct-{seq}",
        envelope=envelope,
        payload=RunStarted(purpose="chat"),
        trace_policy="persist",
        replayable=True,
    )
    prepared = harness.broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=seq,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=unsequenced.payload,
        trace_policy="persist",
        replayable=True,
    )
    harness.broker.commit(prepared, sequenced)
    return sequenced


def test_ingress_overflow_never_walks_the_connections_from_commit() -> None:
    """commit stays O(1) in connections even when it must refuse liveness.

    Six connections hold queues that throw the moment the publish path
    touches them. Overflowing the ingress with an undroppable event from
    the emitting thread must therefore return without touching any of them;
    asking them to hang up is the dispatcher's work, outside the publish
    critical section.
    """
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    probes: list[_ProbingQueue] = []
    for _ in range(6):
        handle = harness.connect()
        handle.queue = _ProbingQueue()
        probes.append(handle.queue)
    # The ingress is occupied by a first event, so the next one overflows.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    overflow = _commit_direct(harness, seq=2, run_id="r2")
    # Ring first: the fact is recoverable even though liveness was refused.
    assert harness.broker._ring.high_water_seq() == overflow.seq
    # commit returned without touching a single queue -- the O(1) claim.
    assert all(probe.close_requests == 0 for probe in probes)
    # The dispatcher closes every connection that was registered before the
    # overflow, and no queue is touched until it runs.
    for probe in probes:
        probe.armed = False
    drain_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)


def test_a_connection_registered_after_the_overflow_is_not_closed() -> None:
    """The generation answers "who was there when it happened".

    A connection that registers after the overflow recovered its starting
    state from the snapshot and the ring; closing it would punish the one
    client that cannot be missing anything.
    """
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    _commit_direct(harness, seq=2, run_id="r2")  # overflows the ingress
    late = harness.connect(cursor=cursor_for(0))
    drain_dispatcher(harness)
    assert handle.queue.close_requested is True
    assert late.queue.close_requested is False


def test_patch_overflow_never_walks_the_connections_from_the_publisher() -> None:
    """The state-patch overflow rides the same mechanism, not a second path.

    A refused patch moves the authoritative snapshot and raises the
    generation; the publisher itself touches no queue, and the dispatcher
    closes exactly the connections that predate the incident.
    """
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    probes: list[_ProbingQueue] = []
    for _ in range(4):
        handle = harness.connect()
        handle.queue = _ProbingQueue()
        probes.append(handle.queue)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # occupies ingress
    accepted = harness.broker.publish_state_patch(runtime_snapshot(state_revision=5))
    assert accepted is False
    # The authoritative snapshot moved anyway (a reconnect sees revision 5),
    # and no connection was touched from the publishing thread.
    assert harness.broker._latest.state_revision == 5
    assert all(probe.close_requests == 0 for probe in probes)
    # A connection registered after the refused patch already holds the new
    # state, so the sweep must leave it alone.
    late = harness.connect()
    for probe in probes:
        probe.armed = False
    drain_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)
    assert late.queue.close_requested is False


def test_patch_overflow_kick_handoff_resumes_after_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kick handoff remains retryable by the same snapshot.

    The authoritative revision and disconnect generation commit before the
    lock-free wake-up.  If a ``BaseException`` lands before that wake-up has
    any effect, the identical-revision recovery call must own another attempt;
    otherwise the old connection waits forever for a kick that was never
    queued.
    """
    harness = Harness(
        ingress_budget=frames.FrameBudget(frames=1, encoded_bytes=1)
    )
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    real_put_kick = broker._ingress.put_kick  # noqa: SLF001
    attempts = 0

    def interrupt_first_kick() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt("kick handoff interrupted before enqueue")
        real_put_kick()

    monkeypatch.setattr(
        broker._ingress,  # noqa: SLF001
        "put_kick",
        interrupt_first_kick,
    )
    snapshot = runtime_snapshot(state_revision=1)

    with pytest.raises(KeyboardInterrupt, match="before enqueue"):
        broker.publish_state_patch(snapshot)

    assert broker._latest is snapshot  # noqa: SLF001
    assert old.queue.close_requested is False
    assert broker.publish_state_patch(replace(snapshot)) is False
    assert attempts == 2, "the recovery publish inherited a phantom pending kick"
    assert broker.deliver_next(timeout=0) is True, "the retry did not queue a kick"
    assert old.queue.close_requested is True
    assert broker.deliver_next(timeout=0) is False


def test_patch_overflow_kick_handoff_coalesces_after_effect_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception after the queue effect cannot manufacture more kicks."""
    harness = Harness(
        ingress_budget=frames.FrameBudget(frames=1, encoded_bytes=1)
    )
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    owned_queue = broker._ingress._items  # noqa: SLF001

    class PutThenInterruptQueue:
        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.kick_puts = 0

        def put(self, item) -> None:
            owned_queue.put(item)
            if isinstance(item, _IngressKick):
                self.kick_puts += 1
                if self.failures:
                    self.failures -= 1
                    raise KeyboardInterrupt(
                        "kick handoff interrupted after enqueue"
                    )

        def __getattr__(self, name):
            return getattr(owned_queue, name)

    proxy = PutThenInterruptQueue(failures=4)
    monkeypatch.setattr(broker._ingress, "_items", proxy)  # noqa: SLF001
    snapshot = runtime_snapshot(state_revision=1)

    for _ in range(4):
        with pytest.raises(KeyboardInterrupt, match="after enqueue"):
            broker.publish_state_patch(replace(snapshot))

    assert proxy.kick_puts == 4
    assert proxy.qsize() == 1, "after-effect retries duplicated the kick"
    assert broker.publish_state_patch(replace(snapshot)) is False
    assert proxy.qsize() == 1
    assert broker.deliver_next(timeout=0) is True
    assert old.queue.close_requested is True
    assert broker.deliver_next(timeout=0) is False


def test_fatal_patch_kick_handoff_resumes_on_the_next_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latched fatal still owes its dispatcher a wake-up after interruption."""
    harness = Harness()
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    ingress_failure = RuntimeError("patch ingress failed")
    real_put_kick = broker._ingress.put_kick  # noqa: SLF001
    attempts = 0

    def fail_offer(_item) -> bool:
        raise ingress_failure

    def interrupt_first_kick() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt("fatal kick interrupted before enqueue")
        real_put_kick()

    monkeypatch.setattr(broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(
        broker._ingress,  # noqa: SLF001
        "put_kick",
        interrupt_first_kick,
    )
    snapshot = runtime_snapshot(state_revision=1)

    with pytest.raises(KeyboardInterrupt, match="before enqueue"):
        broker.publish_state_patch(snapshot)

    assert broker._fatal is ingress_failure  # noqa: SLF001
    assert old.queue.close_requested is False
    assert broker.publish_state_patch(replace(snapshot)) is False
    assert attempts == 2, "the latched fatal never repaid its wake-up debt"
    assert broker.deliver_next(timeout=0) is False
    assert old.queue.close_requested is True


# --- capture exhaustion keeps the authority and owes the reconnect -----------


def test_capture_exhaustion_cannot_replace_a_conflicting_same_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exhaustion close applies the ordinary revision conflict rule."""
    harness = Harness()
    broker = harness.broker
    candidate = runtime_snapshot(state_revision=1)
    winner = runtime_snapshot(
        state_revision=1,
        coordinator_state="accepted",
        active_run=ActiveRunSummary(
            run_id="r-winner",
            purpose="chat",
            gateway="web",
            phase="accepted",
            session_id=None,
            prompt_preview="winner",
            started_at=None,
            recording_state=None,
        ),
    )
    parked = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    real_cost = broker_module._patch_cost
    answers: list[bool] = []
    failures: list[BaseException] = []
    reported: list[BaseException] = []

    def gated_cost(snapshot, step) -> int:
        if snapshot is candidate:
            parked.set()
            assert release.wait(5.0), "test never released the candidate encoder"
        return real_cost(snapshot, step)

    def publish_candidate() -> None:
        try:
            answers.append(broker.publish_state_patch(candidate))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(broker_module, "_MAX_PATCH_CAPTURES", 1)
    monkeypatch.setattr(broker_module, "_patch_cost", gated_cost)
    broker.bind_fatal_handler(reported.append)
    publisher = threading.Thread(target=publish_candidate, daemon=True)
    publisher.start()
    try:
        assert parked.wait(5.0), "candidate never reached its only capture"
        assert broker.publish_state_patch(winner) is True
    finally:
        release.set()
    assert finished.wait(5.0), "exhausted candidate never completed"
    publisher.join()

    assert answers == []
    assert len(failures) == 1
    assert isinstance(failures[0], broker_module.ProcessFatalSinkError)
    assert broker._latest is winner  # noqa: SLF001
    assert broker._fatal is failures[0]  # noqa: SLF001
    assert reported == failures


def test_capture_exhaustion_treats_identical_same_revision_as_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identical exhaustion loser reconnects without replacing authority."""
    harness = Harness()
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    candidate = runtime_snapshot(state_revision=1)
    winner = replace(candidate)
    parked = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    real_cost = broker_module._patch_cost
    answers: list[bool] = []
    failures: list[BaseException] = []

    def gated_cost(snapshot, step) -> int:
        if snapshot is candidate:
            parked.set()
            assert release.wait(5.0), "test never released the candidate encoder"
        return real_cost(snapshot, step)

    def publish_candidate() -> None:
        try:
            answers.append(broker.publish_state_patch(candidate))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(broker_module, "_MAX_PATCH_CAPTURES", 1)
    monkeypatch.setattr(broker_module, "_patch_cost", gated_cost)
    publisher = threading.Thread(target=publish_candidate, daemon=True)
    publisher.start()
    try:
        assert parked.wait(5.0), "candidate never reached its only capture"
        assert broker.publish_state_patch(winner) is True
    finally:
        release.set()
    assert finished.wait(5.0), "exhausted candidate never completed"
    publisher.join()

    assert failures == []
    assert answers == [False]
    assert broker._latest is winner  # noqa: SLF001
    assert broker._fatal is None  # noqa: SLF001
    assert broker._disconnect_generation == 1  # noqa: SLF001
    drain_dispatcher(harness)
    assert old.queue.close_requested is True


def test_capture_exhaustion_kick_handoff_resumes_after_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded-exhaustion close owns the same resumable wake-up."""
    harness = Harness()
    broker = harness.broker
    old = harness.connect()
    drain_connection(old)
    real_put_kick = broker._ingress.put_kick  # noqa: SLF001
    attempts = 0

    def interrupt_first_kick() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt("exhaustion kick interrupted before enqueue")
        real_put_kick()

    monkeypatch.setattr(broker_module, "_MAX_PATCH_CAPTURES", 0)
    monkeypatch.setattr(
        broker._ingress,  # noqa: SLF001
        "put_kick",
        interrupt_first_kick,
    )
    snapshot = runtime_snapshot(state_revision=1)

    with pytest.raises(KeyboardInterrupt, match="before enqueue"):
        broker.publish_state_patch(snapshot)

    assert broker._latest is snapshot  # noqa: SLF001
    assert old.queue.close_requested is False
    assert broker.publish_state_patch(replace(snapshot)) is False
    assert attempts == 2
    assert broker.deliver_next(timeout=0) is True
    assert old.queue.close_requested is True
    assert broker.deliver_next(timeout=0) is False


class _PerLapCost:
    """The cost probe that parks the publisher once per capture lap.

    Where :class:`_GatedCost` parks a single encode so the world can move
    around it, this one parks *every* lap up to a budget, so a test can
    lose the recapture race exactly ``_MAX_PATCH_CAPTURES`` times in a row
    and walk the publisher into its exhaustion path for certain. Each lap
    gets its own rendezvous: the probe raises that lap's ``arrived`` once
    the publisher is parked mid-measurement -- outside the broker lock,
    where the world can move -- and the test sets that lap's release after
    committing its interleave. The scheduler decides nothing.
    """

    def __init__(self, real, laps: int) -> None:
        self._real = real
        self._arrived = [threading.Event() for _ in range(laps)]
        self._releases = [threading.Event() for _ in range(laps)]
        self._next = 0
        self._aborted = False

    def __call__(self, snapshot, step) -> int:
        if self._aborted or self._next >= len(self._arrived):
            return self._real(snapshot, step)
        lap = self._next
        self._next += 1
        self._arrived[lap].set()
        self._releases[lap].wait(5.0)  # anti-hang only; the test releases
        return self._real(snapshot, step)

    def wait_parked(self, lap: int) -> bool:
        return self._arrived[lap].wait(5.0)

    def release_lap(self, lap: int) -> None:
        self._releases[lap].set()

    def abort(self) -> None:
        """Free a publisher a failing test would otherwise park forever."""
        self._aborted = True
        for release in self._releases:
            release.set()


def _park_a_publisher_every_lap(
    harness, monkeypatch, snapshot
) -> tuple[_PerLapCost, threading.Thread, list[bool]]:
    """Start one publisher and hold it at every lap's cost measurement.

    Like :func:`_park_a_publisher`, but the rendezvous repeats once per
    capture lap: the test commits one real transition between each lap's
    capture and its commit, so the bounded recapture is exhausted for
    certain. The publisher's answer lands in the returned list.
    """
    gate = _PerLapCost(broker_module._patch_cost, broker_module._MAX_PATCH_CAPTURES)
    monkeypatch.setattr(broker_module, "_patch_cost", gate)
    answers: list[bool] = []

    def publisher() -> None:
        answers.append(harness.broker.publish_state_patch(snapshot))

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    assert gate.wait_parked(0), "the publisher never reached its first encoding"
    return gate, thread, answers


def _opening_revision(wire: bytes) -> int | None:
    """The state revision an opening stream's snapshot names, if any."""
    match = re.search(rb'"state_revision":(\d+)', wire)
    return None if match is None else int(match.group(1))


def test_capture_exhaustion_still_moves_the_authority_and_disconnects(
    monkeypatch,
) -> None:
    """Losing every recapture lap must not leave the authority behind.

    A world that moves during every single encode lap -- here one real
    ``StepStarted`` per lap, ``_MAX_PATCH_CAPTURES`` times -- exhausts the
    bounded recapture, and the captured Step belongs to a world that is
    already gone. A bare ``False`` that did nothing would leave the
    broker's ``_latest`` behind the authoritative store's snapshot for
    good: no patch, no disconnect, and every reconnect re-seeding the
    stale revision. The exhaustion path must still adopt the snapshot as
    the authority, raise the disconnect generation so the dispatcher asks
    the pre-incident connections to come back for an atomic snapshot, and
    answer False -- the patch genuinely never entered the ingress.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    requested = runtime_snapshot(state_revision=5)
    gate, thread, answers = _park_a_publisher_every_lap(harness, monkeypatch, requested)
    try:
        for lap in range(broker_module._MAX_PATCH_CAPTURES):
            assert gate.wait_parked(lap), f"lap {lap} never reached its encoding"
            # One real transition per lap, committed while the publisher
            # sits parked mid-measurement, holding no broker lock.
            harness.emit(StepStarted(step_index=lap), run_id="r1")
            gate.release_lap(lap)
    except BaseException:
        gate.abort()
        raise
    thread.join(timeout=5.0)
    # A connection registering after the exhaustion re-seeds from whatever
    # the broker now holds -- and registers at the current generation.
    late = harness.connect()
    late_wire = b"".join(item.wire_bytes() for item in drain_connection(late))
    drain_dispatcher(harness)
    # Observed before the first assert, so a red run reports the whole
    # failure, not just its first casualty.
    observed = {
        "answer": answers,
        "latest_revision": harness.broker._latest.state_revision,
        "disconnect_generation": harness.broker._disconnect_generation,
        "old_connection_close_requested": handle.queue.close_requested,
        "late_connection_close_requested": late.queue.close_requested,
        "late_opening_revision": _opening_revision(late_wire),
        "patches_delivered": _patches_received(handle),
    }
    assert answers == [False], observed
    assert observed["latest_revision"] == requested.state_revision, observed
    assert observed["disconnect_generation"] == 1, observed
    assert observed["old_connection_close_requested"] is True, observed
    assert observed["late_connection_close_requested"] is False, observed
    assert observed["late_opening_revision"] == requested.state_revision, observed
    assert observed["patches_delivered"] == [], observed


def test_exhausted_publisher_never_regresses_an_overtaken_snapshot(
    monkeypatch,
) -> None:
    """Exhaustion adopts the snapshot only while it is still the newest.

    The closing move of a fully lost recapture race must re-check the
    authority it is about to move: if a newer absolute replacement was
    published while the laps ran, its own patch is already in flight, and
    adopting the older snapshot would walk ``_latest`` -- and every
    reconnect -- backwards. The refusal then owes nobody a reconnect.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    requested = runtime_snapshot(state_revision=5)
    gate, thread, answers = _park_a_publisher_every_lap(harness, monkeypatch, requested)
    newer = None
    try:
        last = broker_module._MAX_PATCH_CAPTURES - 1
        for lap in range(broker_module._MAX_PATCH_CAPTURES):
            assert gate.wait_parked(lap), f"lap {lap} never reached its encoding"
            harness.emit(StepStarted(step_index=lap), run_id="r1")
            if lap == last:
                # The final lap loses to a newer *snapshot*, not only to an
                # event: by the time the exhausted publisher reaches its
                # closing move, the authority has already moved past it.
                newer = runtime_snapshot(state_revision=6)
                assert harness.broker.publish_state_patch(newer), (
                    "the newer publish must not wait behind the parked one"
                )
            gate.release_lap(lap)
    except BaseException:
        gate.abort()
        raise
    thread.join(timeout=5.0)
    drain_dispatcher(harness)
    assert answers == [False], "the exhausted publisher must still refuse"
    assert harness.broker._latest is newer, "_latest moved backwards"
    assert harness.broker._disconnect_generation == 0, (
        "the overtaking patch was delivered; nobody owes a reconnect"
    )
    assert handle.queue.close_requested is False, (
        "an overtaken refusal must not disconnect a connection that is "
        "about to receive the newer patch"
    )
    patches = _patches_received(handle)
    assert patches and patches[-1]["state_revision"] == 6, (
        "the overtaking patch never reached the connection"
    )


# --- a trailing transient is published, but never a checkpoint --------------


def _gap_payload(harness, cursor: str) -> dict:
    handle = harness.connect(cursor=cursor)
    notice = _wire_containing(handle, b"replay_gap")
    return _decode_patch(notice)


def test_a_trailing_transient_seq_is_malformed_not_ahead() -> None:
    """A transient consumes a seq without minting a checkpoint.

    The published high water knows the transient was published, so a cursor
    naming it is refused as ``malformed`` (published, never issued) -- not
    ``ahead``, which would claim this process never sent something the
    client may well be holding. One seq past the published high water is
    still ``ahead``, and the next real checkpoint is still ``valid``.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 1, replayable
    harness.emit(BlockDelta(text="x"), run_id="r1")  # seq 2, transient
    assert _gap_payload(harness, cursor_for(2)) == {
        "code": "replay_gap",
        "gap_reason": "malformed",
        "requested_seq": 2,
        "oldest_seq": 1,
        "high_water_seq": 2,
        "current_run_state": "absent",
    }
    assert _gap_payload(harness, cursor_for(3)) == {
        "code": "replay_gap",
        "gap_reason": "ahead",
        "requested_seq": 3,
        "oldest_seq": 1,
        "high_water_seq": 2,
        "current_run_state": "absent",
    }
    # The next replayable event lands on the transient's successor and is a
    # checkpoint like any other.
    third = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert third.seq == 3
    handle = harness.connect(cursor=cursor_for(1))
    assert replay_ids(drain_connection(handle)) == [3]
    # Resuming *from* 3 has nothing to catch up, as any checkpoint.
    handle = harness.connect(cursor=cursor_for(3))
    assert replay_ids(drain_connection(handle)) == []
    # The transient never entered the ring and never got an id:.
    assert len(harness.broker._ring) == 2


# --- Task 02: connection work stays outside publication --------------------


class _ThreadOwnedLock:
    """Keep real lock contention while observing only this thread's ownership."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._local.held = True
        return acquired

    def release(self) -> None:
        self._local.held = False
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return getattr(self._local, "held", False)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _observe_broker_lock_owners(broker: SSEBroker) -> None:
    # Install before start/connect: no native thread can retain the old locks.
    broker._lock = _ThreadOwnedLock()
    broker._registry_lock = _ThreadOwnedLock()


class _FatalThreadProbeQueue(ConnectionQueue):
    broker: SSEBroker | None = None
    close_threads: list[str] = []
    closed = threading.Event()

    def request_close(self, *, retry_ms: int | None = None) -> None:
        name = threading.current_thread().name
        type(self).close_threads.append(name)
        if name == "fatal-emitter":
            raise AssertionError("fatal publish touched a connection queue")
        broker = type(self).broker
        assert broker is not None
        assert not broker._lock.held_by_current_thread(), (
            "fatal sweep held the publication lock"
        )
        assert not broker._registry_lock.held_by_current_thread(), (
            "fatal sweep held the registry lock"
        )
        super().request_close(retry_ms=retry_ms)
        type(self).closed.set()


def test_ring_failure_never_touches_connection_queue_under_fanout_publish_lock(
    monkeypatch,
) -> None:
    """A ring failure only latches/wakes; the dispatcher owns the sweep."""
    monkeypatch.setattr(broker_module, "ConnectionQueue", _FatalThreadProbeQueue)
    _FatalThreadProbeQueue.close_threads = []
    _FatalThreadProbeQueue.closed = threading.Event()
    ring = _BrokenRing()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        ring=ring,
        spawn=RealThreadSpawner().spawn,
    )
    _observe_broker_lock_owners(broker)
    broker.start()
    _FatalThreadProbeQueue.broker = broker
    first = broker.connect(connection=FakeConnection())
    second = broker.connect(connection=FakeConnection())
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    emitted = threading.Event()

    def emit() -> None:
        fanout.emit(RunStarted(purpose="chat"))
        emitted.set()

    emitter = threading.Thread(target=emit, name="fatal-emitter", daemon=True)
    emitter.start()
    assert emitted.wait(5.0), "fatal emitter did not leave publication"
    assert _FatalThreadProbeQueue.closed.wait(5.0), "dispatcher never swept fatal"
    emitter.join(timeout=5.0)
    assert "fatal-emitter" not in _FatalThreadProbeQueue.close_threads
    assert first.finished.wait(5.0)
    assert second.finished.wait(5.0)
    assert first.queue.close_requested is True
    assert second.queue.close_requested is True
    assert len(_FatalThreadProbeQueue.close_threads) == 2
    fanout.emit(StepStarted(step_index=1))
    assert len(_FatalThreadProbeQueue.close_threads) == 2


class _SlowOfferProbeQueue(ConnectionQueue):
    broker: SSEBroker | None = None
    entered = threading.Event()
    release = threading.Event()
    blocked_once = False
    publication_lock_seen = False
    delivered = threading.Event()
    offer_count = 0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seen_seqs: list[int | None] = []

    def offer(self, item, **kwargs):
        if not type(self).blocked_once:
            type(self).blocked_once = True
            broker = type(self).broker
            assert broker is not None
            if broker._lock.held_by_current_thread():
                type(self).publication_lock_seen = True
                raise AssertionError("dispatcher held broker lock while offering")
            if broker._registry_lock.held_by_current_thread():
                type(self).publication_lock_seen = True
                raise AssertionError("dispatcher held registry lock while offering")
            type(self).entered.set()
            type(self).release.wait(5.0)
        outcome = super().offer(item, **kwargs)
        self.seen_seqs.append(item.seq)
        type(self).offer_count += 1
        if type(self).offer_count == 4:
            type(self).delivered.set()
        return outcome


def test_slow_connection_queue_cannot_block_next_fanout_publish_through_broker_lock(
    monkeypatch,
) -> None:
    """A slow dispatcher offer cannot reverse-block a later FanOut commit."""
    monkeypatch.setattr(broker_module, "ConnectionQueue", _SlowOfferProbeQueue)
    _SlowOfferProbeQueue.entered = threading.Event()
    _SlowOfferProbeQueue.release = threading.Event()
    _SlowOfferProbeQueue.blocked_once = False
    _SlowOfferProbeQueue.publication_lock_seen = False
    _SlowOfferProbeQueue.delivered = threading.Event()
    _SlowOfferProbeQueue.offer_count = 0
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    _observe_broker_lock_owners(broker)
    _SlowOfferProbeQueue.broker = broker
    broker.start()
    first = broker.connect(connection=FakeConnection())
    second = broker.connect(connection=FakeConnection())
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    fanout.emit(RunStarted(purpose="chat"))
    assert _SlowOfferProbeQueue.entered.wait(5.0), "dispatcher missed offer boundary"
    published = threading.Event()

    def emit_second() -> None:
        fanout.emit(StepStarted(step_index=1))
        published.set()

    emitter = threading.Thread(target=emit_second, daemon=True)
    emitter.start()
    try:
        assert published.wait(5.0), "next publish waited for the slow connection"
        assert _SlowOfferProbeQueue.publication_lock_seen is False
    finally:
        _SlowOfferProbeQueue.release.set()
        emitter.join(timeout=5.0)
    assert _SlowOfferProbeQueue.delivered.wait(5.0)
    assert first.queue.seen_seqs == [1, 2]
    assert second.queue.seen_seqs == [1, 2]
    assert first.finished.is_set() is False
    assert second.finished.is_set() is False
    assert broker.close(timeout=5.0) is True


def test_ingress_exception_uses_the_same_dispatcher_owned_fatal_sweep(
    monkeypatch,
) -> None:
    """A broken ingress is process-fatal; an ordinary refusal is not."""
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    broker.start()
    handle = broker.connect(connection=FakeConnection())
    capture, fanout = _capture_fanout(broker)
    failure = RuntimeError("ingress owner failed")

    def fail_offer(_item):
        raise failure

    monkeypatch.setattr(broker._ingress, "offer", fail_offer)  # noqa: SLF001
    fanout.emit(RunStarted(purpose="chat"))

    assert handle.finished.wait(5.0), "fatal ingress did not retire the stream"
    assert broker._fatal is failure  # noqa: SLF001
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1


@pytest.mark.parametrize("connection_count", [0, 1, 3])
def test_state_patch_ingress_exception_latches_one_dispatcher_owned_fatal(
    monkeypatch,
    connection_count: int,
) -> None:
    """Patch ingress failure uses the same O(1) fatal boundary as events."""
    monkeypatch.setattr(broker_module, "ConnectionQueue", _FatalThreadProbeQueue)
    _FatalThreadProbeQueue.close_threads = []
    _FatalThreadProbeQueue.closed = threading.Event()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    _observe_broker_lock_owners(broker)
    _FatalThreadProbeQueue.broker = broker
    reported: list[BaseException] = []
    broker.bind_fatal_handler(reported.append)
    broker.start()
    handles = [
        broker.connect(connection=FakeConnection())
        for _ in range(connection_count)
    ]
    failure = RuntimeError("state patch ingress owner failed")
    real_put_kick = broker._ingress.put_kick  # noqa: SLF001
    kicks: list[None] = []

    def fail_offer(_item):
        raise failure

    def count_kick() -> None:
        kicks.append(None)
        real_put_kick()

    monkeypatch.setattr(broker._ingress, "offer", fail_offer)  # noqa: SLF001
    monkeypatch.setattr(broker._ingress, "put_kick", count_kick)  # noqa: SLF001
    raised: list[BaseException] = []

    def publish() -> None:
        try:
            broker.publish_state_patch(runtime_snapshot(state_revision=1))
        except BaseException as exc:
            raised.append(exc)

    publisher = threading.Thread(target=publish, name="fatal-emitter", daemon=True)
    publisher.start()
    publisher.join(timeout=5.0)

    assert raised == [failure]
    assert broker._fatal is failure  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    assert reported == [failure], (
        "the first patch failure must report without a later domain event"
    )
    assert kicks == [None]
    assert "fatal-emitter" not in _FatalThreadProbeQueue.close_threads
    for handle in handles:
        assert handle.finished.wait(5.0), "dispatcher did not retire a stream"
        assert handle.queue.close_requested is True
    assert len(_FatalThreadProbeQueue.close_threads) == connection_count

    # Every ingress-facing public entry now refuses without revisiting the
    # broken ingress, replacing the first reason, or sweeping again.
    capture, fanout = _capture_fanout(broker)
    fanout.emit(RunStarted(purpose="chat"))
    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert broker.publish_state_patch(runtime_snapshot(state_revision=2)) is False
    assert broker._fatal is failure  # noqa: SLF001
    assert kicks == [None]
    assert len(_FatalThreadProbeQueue.close_threads) == connection_count
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1


class _LockCheckingQueue(ConnectionQueue):
    broker: SSEBroker | None = None
    offers = 0

    def offer(self, item, **kwargs):
        broker = type(self).broker
        assert broker is not None
        acquired = broker._lock.acquire(blocking=False)  # noqa: SLF001
        assert acquired, "connection offer ran under the broker registry lock"
        broker._lock.release()  # noqa: SLF001
        registry_acquired = broker._registry_lock.acquire(  # noqa: SLF001
            blocking=False
        )
        assert registry_acquired, "connection offer ran under the registry lock"
        broker._registry_lock.release()  # noqa: SLF001
        type(self).offers += 1
        return super().offer(item, **kwargs)


def test_domain_and_patch_delivery_offer_outside_the_broker_registry_lock(
    monkeypatch,
) -> None:
    """Both dispatcher payload families obey the same fixed lock order."""
    monkeypatch.setattr(broker_module, "ConnectionQueue", _LockCheckingQueue)
    _LockCheckingQueue.offers = 0
    harness = Harness()
    _LockCheckingQueue.broker = harness.broker
    handle = harness.connect()
    drain_connection(handle)

    harness.emit(RunStarted(purpose="chat"))
    harness.deliver()
    assert harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _LockCheckingQueue.offers == 2
