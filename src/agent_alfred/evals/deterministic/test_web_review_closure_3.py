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
  the database was still open.

Every test here fails on ``47f6759`` for the reason named in its
docstring. No sleeps: the orderings are pinned with gates, events and
latches, so a slow machine makes a test slower to fail, never less
correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_alfred.evals.deterministic.test_web_broker import _GatedThreads
from agent_alfred.evals.deterministic.test_web_review_closure_2 import (
    _FakeConnection,
    _FakeHost,
    _RecordingLock,
    _RecordingServer,
    _Rig,
)
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.connection import FakeConnection
from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.server import DashboardRuntime
from agent_alfred.runtime.snapshot import RuntimeSnapshot


# --- the runtime's tail: database, then entry, each refusable ---------------


class _FailingCloseConnection(_FakeConnection):
    """A database whose close() refuses the first N times."""

    def __init__(self, trace: list[str], failures: int):
        super().__init__(trace)
        self._failures = failures

    def close(self) -> None:
        if self.close_calls < self._failures:
            self.close_calls += 1
            raise RuntimeError("database busy")
        super().close()


class _StickyReleaseLock(_RecordingLock):
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


class _Rig3(_Rig):
    """The closure-2 rig, with the tail's own steps made refusable."""

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
    rig = _Rig3(tmp_path, conn_close_failures=1)
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
    rig = _Rig3(tmp_path, lock_release_failures=1)
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

    Everything else is the closure-2 rig's shape: a fake Host that stops
    when asked, a fake database, a real flock. The broker is the real one,
    so its ``close()`` answer is about its own threads -- which this rig's
    gates hold at the starting line for exactly as long as the test says.
    """

    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.trace: list[str] = []
        self.gates = _GatedThreads()
        self.conn = _FakeConnection(self.trace)
        self.lock = _RecordingLock(self.tmp_path / LOCK_NAME, self.trace)
        self.host: _FakeHost | None = None
        self.broker: SSEBroker | None = None
        self.runtime = DashboardRuntime(
            state_dir=self.tmp_path,
            assemble=self._assemble,
            port=7717,
            instance_id="inst-closure-3",
            open_database=self._open_database,
            server_factory=_RecordingServer,
            write_descriptor=lambda d, e: write_entry_descriptor(d, e),
            lock=self.lock,
        )

    def _open_database(self, directory):
        del directory
        return self.conn

    def _assemble(self, conn, instance_id):
        self.host = _FakeHost(conn, (True,), self.trace)
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
