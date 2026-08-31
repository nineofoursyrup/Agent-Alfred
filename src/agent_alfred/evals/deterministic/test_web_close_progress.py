"""The release tail is progress, not a completion report.

One shape, three places. A step that publishes "done" before the action
behind it has succeeded turns a failure tail into a lie the next caller
acts on:

- ``SSEBroker.close()`` set ``_closed`` on the first attempt, so a second
  call answered True while its threads were still running -- and the
  runtime holding that answer released the database and the process lock
  under a stream that was still draining (proven here end to end, with a
  real broker whose threads the test holds at the starting line);
- ``DashboardService._release_server()`` dropped the server before the
  socket was confirmed closed, so the next close skipped the socket and
  handed over the descriptor and the lock;
- ``DashboardRuntime._release_locked()`` set ``_released`` before the
  database was closed and released the lock in a ``finally``, so a failing
  tail reported itself complete and left the state directory unowned while
  the database was still open;
- ``FanOutSink.close()`` kept no per-sink progress and ``RuntimeHost.close()``
  raised its ``_fanout_closed`` bit before calling it, so one sink whose
  close() raised left the whole FanOut marked closed forever: the Host
  reported itself closed on the next ask, the runtime released the tail,
  and the failed sink was never closed by anyone (proven here end to end,
  with a real Host whose sink refuses its first close).

Every test here fails before its fix, for the reason named in its
docstring. No sleeps: the orderings are pinned with gates, events and
latches, so a slow machine makes a test slower to fail, never less
correct.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_broker_test_helpers import GatedThreads
from agent_alfred.evals.deterministic._web_close_test_helpers import (
    CloseTrackingConnection,
    CloseTrackingHost,
    DashboardCloseRig,
    RecordingProcessLock,
    RecordingServer,
)
from agent_alfred.events import (
    BestEffortFlushResult,
    CapturingSink,
    FanOutSink,
)
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.connection import FakeConnection
from agent_alfred.gateway.web.lifecycle import (
    DESCRIPTOR_NAME,
    LOCK_NAME,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.server import DashboardRuntime
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.settings import Settings

# --- the runtime's tail: database, then entry, each refusable ---------------


class _FailingCloseConnection(CloseTrackingConnection):
    """A database whose close() refuses the first N times."""

    def __init__(self, trace: list[str], failures: int):
        super().__init__(trace)
        self._failures = failures

    def close(self) -> None:
        if self.close_calls < self._failures:
            self.close_calls += 1
            raise RuntimeError("database busy")
        super().close()


class _StickyReleaseLock(RecordingProcessLock):
    """A process lock whose release refuses the first N times."""

    def __init__(self, path: Path, trace: list[str], failures: int):
        super().__init__(path, trace)
        self.release_calls = 0
        self._failures = failures

    def release(self) -> None:
        self.release_calls += 1
        if self.release_calls <= self._failures:
            raise RuntimeError("lock release refused")
        super().release()


class RefusableTailRig(DashboardCloseRig):
    """A dashboard close rig whose tail steps can refuse completion."""

    def __init__(self, tmp_path, *, conn_close_failures=0, lock_release_failures=0):
        self._conn_close_failures = conn_close_failures
        self._lock_release_failures = lock_release_failures
        super().__init__(tmp_path)

    def _make_conn(self):
        if self._conn_close_failures:
            return _FailingCloseConnection(self.trace, self._conn_close_failures)
        return super()._make_conn()

    def _make_lock(self):
        if self._lock_release_failures:
            return _StickyReleaseLock(
                self.tmp_path / LOCK_NAME, self.trace, self._lock_release_failures
            )
        return super()._make_lock()


def test_a_database_that_will_not_close_keeps_the_tail_pending(tmp_path) -> None:
    """A failing database close is an unfinished tail, not a finished one.

    ``conn.close()`` raising must not publish "released": the runtime keeps
    the database open, the descriptor on disk and the lock held, stays in
    ``closing``, and the next close resumes at the database instead of
    skipping the rest of the tail on the strength of a flag set in advance.
    """
    rig = RefusableTailRig(tmp_path, conn_close_failures=1)
    rig.runtime.start()
    with pytest.raises(RuntimeError, match="database busy"):
        rig.runtime.close()

    assert rig.runtime.state == "closing"
    assert rig.conn.closed is False
    assert rig.conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.lock_is_held() is True

    assert rig.runtime.close() is True
    assert rig.runtime.state == "closed"
    assert rig.conn.closed is True
    assert rig.conn.close_calls == 2, "the resume retried the database"
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False


def test_a_lock_release_that_fails_leaves_the_runtime_closing(tmp_path) -> None:
    """The entry is released only when the lock is actually let go.

    The lock refusing to release is the last step failing -- after the
    database is closed and the descriptor deleted. The runtime must stay in
    ``closing`` rather than report a completed close, and the next close
    must release the lock without closing the database a second time or
    touching the descriptor again.
    """
    rig = RefusableTailRig(tmp_path, lock_release_failures=1)
    rig.runtime.start()
    with pytest.raises(RuntimeError, match="lock release refused"):
        rig.runtime.close()

    assert rig.runtime.state == "closing"
    assert rig.conn.close_calls == 1
    assert rig.conn.closed is True
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock_is_held() is True

    assert rig.runtime.close() is True
    assert rig.lock.release_calls == 2
    assert rig.lock.acquired is False
    assert rig.conn.close_calls == 1, "the database is closed exactly once"
    assert rig.runtime.state == "closed"


# --- the runtime above a real broker that is still draining -----------------


class _RealBrokerRig:
    """A runtime whose stream is a real SSEBroker with gated threads.

    Everything else follows the shared close rig: a fake Host that stops
    when asked, a fake database, a real flock. The broker is the real one,
    so its ``close()`` answer is about its own threads -- which this rig's
    gates hold at the starting line for exactly as long as the test says.
    """

    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.trace: list[str] = []
        self.gates = GatedThreads()
        self.conn = CloseTrackingConnection(self.trace)
        self.lock = RecordingProcessLock(self.tmp_path / LOCK_NAME, self.trace)
        self.host: CloseTrackingHost | None = None
        self.broker: SSEBroker | None = None
        self.runtime = DashboardRuntime(
            state_dir=self.tmp_path,
            assemble=self._assemble,
            port=7717,
            instance_id="inst-close-progress",
            open_database=self._open_database,
            server_factory=RecordingServer,
            write_descriptor=lambda d, e: write_entry_descriptor(d, e),
            lock=self.lock,
        )

    def _open_database(self, directory):
        del directory
        return self.conn

    def _assemble(self, conn, instance_id):
        self.host = CloseTrackingHost(conn, (True,), self.trace)
        self.broker = SSEBroker(
            process_instance_id=instance_id,
            snapshot=RuntimeSnapshot(
                process_instance_id=instance_id,
                state_revision=0,
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            ),
            session_is_valid=lambda _sid: True,
            spawn=self.gates.spawn,
        )
        return self.host, self.broker

    def lock_is_held(self) -> bool:
        if not self.lock.acquired:
            return False
        probe = ProcessLock(self.tmp_path / LOCK_NAME)
        try:
            probe.acquire()
        except StateDirLocked:
            return True
        probe.release()
        return False


def test_a_stream_that_is_still_draining_holds_the_runtime_shut(tmp_path) -> None:
    """The broker's False must be the runtime's "not yet" -- every time.

    With a real broker whose dispatcher and writer the test holds at the
    starting line: the first close reports False and the runtime keeps its
    database, its descriptor and its lock. The dispatcher then exits, but
    the writer is still alive -- so the second close must also report
    False, and the tail must not run. Only once the last thread has really
    exited does the tail run, exactly once, and further closes are True.
    """
    rig = _RealBrokerRig(tmp_path)
    rig.runtime.start()
    rig.broker.connect(connection=FakeConnection())
    dispatcher = rig.gates.by_name["_dispatch_loop"]
    writer = rig.gates.by_name["<lambda>"]

    assert rig.runtime.close(timeout=0.05) is False
    assert rig.conn.closed is False
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.lock_is_held() is True

    rig.gates.open("_dispatch_loop")
    dispatcher.join(timeout=5.0)
    # The dispatcher is gone; the writer is not. This is the attempt the
    # first close's bookkeeping must not be allowed to answer for.
    assert rig.runtime.close(timeout=0.05) is False
    assert writer.is_alive()
    assert rig.conn.closed is False
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.lock_is_held() is True

    rig.gates.open("<lambda>")
    writer.join(timeout=5.0)
    assert rig.runtime.close() is True
    assert rig.conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False
    assert rig.runtime.close() is True


def test_a_descriptor_that_refuses_deletion_keeps_the_tail_pending(
    tmp_path, monkeypatch
) -> None:
    """The entry is withdrawn only when the file is really gone.

    A ``_forget_descriptor()`` that dropped its reference before ``unlink()``
    succeeded turned the first failure into a permanent one: the next close
    deleted nothing and released the lock on top of a descriptor that still
    named this process -- and a browser still reading it would knock on a
    dead port. The reference survives a failed unlink, so the retry deletes
    the file for real, releases the lock exactly once, and only then is the
    runtime ``closed``.
    """
    rig = RefusableTailRig(tmp_path)
    rig.runtime.start()
    attempts = {"count": 0}
    real_unlink = Path.unlink

    def refusing_unlink(self, missing_ok=False):
        if self.name == DESCRIPTOR_NAME and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    with pytest.raises(PermissionError):
        rig.runtime.close()

    assert rig.runtime.state == "closing"
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.lock_is_held() is True
    assert rig.conn.close_calls == 1
    monkeypatch.undo()

    assert rig.runtime.close() is True
    assert rig.runtime.state == "closed"
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False
    assert rig.conn.close_calls == 1, "the resume does not close it twice"


# --- the runtime above a real Host whose sink refuses to close --------------


class _FlakyCloseSink:
    """A sink whose close() refuses until released, counting every ask."""

    name = "flaky-close"
    flush_at_run_end = False

    def __init__(self) -> None:
        self.close_calls = 0
        self.release = threading.Event()

    def prepare(self, event) -> object:
        del event
        return None

    def commit(self, prepared, event) -> None:
        del prepared, event

    def flush(self, run_id) -> BestEffortFlushResult:
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        self.close_calls += 1
        if not self.release.is_set():
            raise RuntimeError("sink close refused")


class _SpyCloseConnection(sqlite3.Connection):
    """A real database that records whether its close was ever asked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self._trace.append("conn_close")
        super().close()


class _RealHostFanOutRig:
    """A runtime whose Host is the real one, carrying a refusing sink.

    Everything around the Host is the closure rigs' usual counters: a real
    file-migrated sqlite connection behind a close-counting factory, a real
    flock, a recording socket, and the FanOut wired the way production wires
    it -- broker first, capture, then the sink that fails. The false
    completion this closure is about only exists in the real chain: sink
    raises -> FanOut "closed" -> Host "closed" -> tail released.
    """

    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.trace: list[str] = []
        self.lock = RecordingProcessLock(self.tmp_path / LOCK_NAME, self.trace)
        self.sink = _FlakyCloseSink()
        self.conn: _SpyCloseConnection | None = None
        self.host: RuntimeHost | None = None
        self.broker: SSEBroker | None = None
        self.runtime = DashboardRuntime(
            state_dir=self.tmp_path,
            assemble=self._assemble,
            port=7717,
            instance_id="inst-close-progress",
            open_database=self._open_database,
            server_factory=self._server_factory,
            write_descriptor=self._write_descriptor,
            lock=self.lock,
        )

    def _server_factory(self, address, handler):
        self.trace.append("bind")
        return RecordingServer(address, handler)

    def _write_descriptor(self, directory, descriptor):
        self.trace.append("write_descriptor")
        return write_entry_descriptor(directory, descriptor)

    def _open_database(self, directory):
        del directory
        self.trace.append("open_db")
        self.conn = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=_SpyCloseConnection
        )
        self.conn._trace = self.trace
        schema.migrate(self.conn)
        return self.conn

    def _assemble(self, conn, instance_id):
        self.trace.append("assemble")
        broker = SSEBroker(
            process_instance_id=instance_id,
            snapshot=RuntimeSnapshot(
                process_instance_id=instance_id,
                state_revision=0,
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            ),
            session_is_valid=lambda _sid: True,
        )
        capture = CapturingSink(name="capture", flush_at_run_end=True)
        fanout = FanOutSink(
            [broker, capture, self.sink], process_instance_id=instance_id
        )
        host = RuntimeHost(
            conn=conn,
            factory=ScriptedModelFactory(ScriptedModel(["pong"])),
            settings=Settings(),
            clock=FakeClock(),
            fanout=fanout,
            process_instance_id=instance_id,
            snapshot_listener=broker.publish_state_patch,
        )
        broker.bind_session_check(host.session_exists)
        self.host, self.broker = host, broker
        return host, broker

    def lock_is_held(self) -> bool:
        if not self.lock.acquired:
            return False
        probe = ProcessLock(self.tmp_path / LOCK_NAME)
        try:
            probe.acquire()
        except StateDirLocked:
            return True
        probe.release()
        return False


def test_dashboard_tail_stays_owned_when_fanout_close_raises(tmp_path) -> None:
    """A FanOut close that raises is an unfinished close at every layer.

    The runtime may only run the release tail behind a Host that really
    stopped, and the Host may only claim to have stopped behind a FanOut
    that really closed. With a real Host whose sink refuses its first
    close(): the failing close propagates to the caller unchanged, the
    runtime keeps the database, the descriptor and the lock, and stays in
    ``closing`` -- not ``closed``, and not ``failed``. The retry closes the
    failed sink for real, and only that success releases anything, in the
    confirmed order: database, then descriptor, then lock.
    """
    rig = _RealHostFanOutRig(tmp_path)
    rig.runtime.start()
    try:
        with pytest.raises(RuntimeError, match="sink close refused"):
            rig.runtime.close()

        # Nothing of the tail has run: the database, the descriptor and the
        # lock are exactly where the running parts left them.
        assert rig.runtime.state == "closing"
        assert rig.conn is not None and rig.conn.close_calls == 0
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True
        assert rig.host is not None and rig.host.closed is False
        assert rig.sink.close_calls == 1

        # The retry closes the failed sink, and the tail follows only then.
        rig.sink.release.set()
        assert rig.runtime.close() is True
        assert rig.sink.close_calls == 2, "the retry closed the failed sink"
        assert rig.conn.close_calls == 1
        assert read_entry_descriptor(tmp_path) is None
        assert rig.lock.acquired is False
        assert rig.runtime.state == "closed"
        # The whole story, in order: one bind and one entry taken, then the
        # tail exactly once -- one database close, and the lock only after
        # it. (The Host is the real one, so its asks are counted on the
        # sink above, not in this trace.)
        assert rig.trace == [
            "bind",
            "write_descriptor",
            "open_db",
            "assemble",
            "conn_close",
            "lock_release",
        ]
    finally:
        rig.sink.release.set()
        rig.runtime.close()
