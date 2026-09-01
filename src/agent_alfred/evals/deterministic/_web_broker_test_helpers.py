"""Shared deterministic SSE broker harnesses and coordination helpers."""

import queue
import threading

from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    RunStarted,
)
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.broker import (
    SSEBroker,
)
from agent_alfred.gateway.web.connection import (
    FakeConnection,
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.replay import CursorText, ReplayRing
from agent_alfred.runtime.snapshot import (
    RuntimeSnapshot,
)

INSTANCE = "inst-test"


def runtime_snapshot(**kwargs) -> RuntimeSnapshot:
    base = {
        "process_instance_id": INSTANCE,
        "state_revision": 0,
        "coordinator_state": "idle",
        "active_run": None,
        "unrecorded_terminal_projection": None,
    }
    base.update(kwargs)
    return RuntimeSnapshot(**base)


class Harness:
    """A broker fed by a real FanOutSink, with threads under test control."""

    def __init__(
        self,
        *,
        ring=None,
        ingress_budget=frames.FrameBudget(4096, 32 * 1024 * 1024),
        connection_budget=frames.FrameBudget(512, 8 * 1024 * 1024),
        max_frame_bytes=frames.MAX_FRAME_BYTES,
        spawn=None,
    ):
        self.spawn = spawn if spawn is not None else _NoThreads()
        self.connection_budget = connection_budget
        self.broker = SSEBroker(
            process_instance_id=INSTANCE,
            snapshot=runtime_snapshot(),
            session_is_valid=lambda session_id: session_id in (None, "s1"),
            ring=ring if ring is not None else ReplayRing(),
            ingress_budget=ingress_budget,
            connection_budget=connection_budget,
            max_frame_bytes=max_frame_bytes,
            spawn=self.spawn.spawn,
        )
        self.fanout = FanOutSink([self.broker], process_instance_id=INSTANCE)
        self.seq = 0

    def emit(self, payload, *, run_id="r1", session_id=None):
        envelope = EventEnvelope(
            ts=float(self.seq),
            run_id=run_id,
            session_id=session_id,
            step_index=None,
            attempt_id=None,
            node_id=None,
        )
        self.seq += 1
        return self.fanout.emit(payload, envelope)

    def emit_many(self, count, *, start=0):
        return [
            self.emit(RunStarted(purpose="chat"), run_id=f"r{start + i}")
            for i in range(count)
        ]

    def connect(self, *, cursor=None, session_id=None, connection=None, **limits):
        return self.broker.connect(
            connection=connection or FakeConnection(),
            cursor=None if cursor is None else CursorText(cursor),
            session_id=session_id,
            **limits,
        )

    def deliver(self, count=1) -> None:
        for _ in range(count):
            assert self.broker.deliver_next(timeout=0.2), "nothing to deliver"


class _NoThreads:
    """Keeps every writer unstarted so a test drives the fan-out by hand."""

    def __init__(self):
        self.targets: list = []

    def spawn(self, target):
        self.targets.append(target)
        return _FakeThread(target)


class RealThreadSpawner:
    """Runs every spawned thread for real, like the production broker."""

    def spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread


class GatedWriteConnection(FakeConnection):
    """A connection whose first write parks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered_write = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes) -> None:
        self.entered_write.set()
        self.release.wait()
        super().write(data)


class _FakeThread:
    def __init__(self, target):
        self._target = target
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False

    def run(self):
        return self._target()


def drain_connection(handle) -> list:
    """Everything this connection would receive now -- and consume it.

    The opening stream belongs only to the writer. The no-thread harness
    drives that public writer seam first, then drains queue items, matching
    the production wire order without putting replay references on a handle.
    """
    items = []
    if handle.writer is not None:
        handle.writer.deliver_startup(items.append)
    while True:
        try:
            items.append(handle.queue.take(timeout=0))
        except queue.Empty:
            return items


def replay_ids(items) -> list[int]:
    out = []
    for item in items:
        if isinstance(item, PreparedFrames) and item.id_line:
            out.append(int(item.id_line.split(b":")[-1].strip()))
    return out


def cursor_for(seq: int) -> str:
    return f"{INSTANCE}:{seq}"


def user_message_with(text: str):
    from agent_alfred.messages import text_message

    return text_message("user", text)


class GatedThreads:
    """Thread stand-ins a test can hold at the starting line.

    Every spawned thread runs its real target only when the gate named for
    that target opens, so a test decides exactly which part of the broker
    is still alive -- without a sleep and without guessing at scheduling.
    The dispatcher's target is the bound method ``_dispatch_loop``; each
    writer's target is the ``<lambda>`` that wraps ``_run_writer``.
    """

    def __init__(self):
        self.gates: dict[str, threading.Event] = {}
        self.by_name: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def spawn(self, target):
        name = getattr(target, "__name__", "<lambda>")
        with self._lock:
            gate = self.gates.setdefault(name, threading.Event())

        def run():
            gate.wait()
            target()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.by_name.setdefault(name, thread)
        return thread

    def open(self, name: str) -> None:
        self.gates[name].set()


def drain_dispatcher(harness) -> None:
    """Drive the dispatcher until the ingress is empty, kick included."""
    while harness.broker.deliver_next(timeout=0.05):
        pass
