"""Dashboard rollback ownership while socket closure is incomplete.

``DashboardService.start()`` composes directory -> process lock -> socket ->
descriptor write, and a failure in the later steps runs an undo of its own.
That undo is steps too, and its steps can refuse: a ``server_close()`` that
raises stops the undo halfway. When that happened, the exception that started
it all was replaced by the undo's own, and the runtime above -- which never
saw ``start()`` return -- went on believing it owned nothing: its rollback
"completed" without releasing the lock, and the state machine reported the
terminal ``failed`` (or a dishonest ``closed``) while the state directory's
process lock was still held. A second instance was then shut out forever by
a Dashboard that insisted it had already ended.

The rule the fix lands: what a partial start took is ownership the runtime
can see. A start whose undo refused stays ``closing``, still holding the
socket and the lock, and ``close()`` resumes the undo in the confirmed
order -- socket, then descriptor fact, then lock, exactly once.

Every test here fails on ``33e7925`` for the reason its docstring names. No
sleeps: the lock conflict is a real flock probed from the same process, and
every ordering is read off the rigs' traces.
"""

from __future__ import annotations

import pytest

from agent_alfred.evals.deterministic._web_close_test_helpers import (
    DashboardCloseRig,
    RecordingServer,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    managed_process_lock,
)
from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    StateDirLocked,
    read_entry_descriptor,
)
from agent_alfred.resource_rollback import IncompleteRollback


class _DescriptorWriteRefused(RuntimeError):
    """The one failure the start is asked to report."""


class _RefusingCloseServer(RecordingServer):
    """A bound socket whose ``server_close()`` refuses the first N times.

    Every ask is counted and traced, so the confirmation order -- socket
    before descriptor before lock -- is read off the trace, not assumed.
    """

    def __init__(self, address, handler, trace: list[str], failures: int):
        super().__init__(address, handler)
        self._trace = trace
        self.close_calls = 0
        self._failures = failures
        self.close_error = RuntimeError("socket busy")

    def server_close(self) -> None:
        self.close_calls += 1
        self._trace.append("server_close")
        if self.close_calls <= self._failures:
            raise self.close_error
        self.closed = True


class _StartFailureRig(DashboardCloseRig):
    """A close-progress rig with refusable startup rollback seams.

    The descriptor write always refuses: the start fails at step 3, after
    the lock and the socket were taken, so every test here is about what
    those two already-taken resources owe their owner.
    """

    def __init__(self, tmp_path, *, close_failures: int = 0):
        self._close_failures = close_failures
        self.start_error = _DescriptorWriteRefused("disk full")
        self.server: _RefusingCloseServer | None = None
        super().__init__(tmp_path)

    def _server_factory(self, address, handler, owner):
        self.binds += 1
        self.trace.append("bind")
        self.server = _RefusingCloseServer(
            address, handler, self.trace, self._close_failures
        )
        owner.publish(self.server)

    def _write_descriptor(self, directory, descriptor):
        self.trace.append("write_descriptor")
        raise self.start_error


def test_start_failure_keeps_runtime_closing_with_resources_owned(tmp_path) -> None:
    """A start whose own undo refused stays ``closing``, still owning.

    The descriptor write failed and the undo's first ``server_close()``
    refused too: the start never returned, so nothing was published -- but
    the socket and the process lock were taken, and they are still this
    runtime's to release. The caller is told why the start failed -- the
    descriptor error, not the rollback's -- with the refused undo step
    riding as its cause.
    """
    rig = _StartFailureRig(tmp_path, close_failures=2)
    try:
        with pytest.raises(RuntimeError) as caught:
            rig.runtime.start()

        # The start failure is the one that propagates, never the rollback's.
        assert caught.value is rig.start_error
        cleanup = caught.value.__cause__
        assert isinstance(cleanup, IncompleteRollback)
        assert cleanup.failure is rig.start_error
        assert cleanup.errors == (rig.server.close_error,)
        # Retryable, not terminal: the undo has not finished.
        assert rig.runtime.state == "closing"
        # What was taken before the failure is still held, references and all.
        assert rig.server is not None
        assert rig.runtime.service.server is rig.server
        assert rig.lock.acquired is True
        assert rig.lock_is_held() is True
        # Nothing was ever published: a start that failed mid-composition has
        # no descriptor to retract, only resources to keep or give back.
        assert read_entry_descriptor(tmp_path) is None
    finally:
        assert rig.runtime.close(timeout=2.0) is True
    assert rig.lock.acquired is False, "the test retained its process lock"


def test_process_lock_rejects_second_lock_until_socket_confirmed_closed(
    tmp_path,
) -> None:
    """The lock is real for exactly as long as the socket is unconfirmed.

    A second ``ProcessLock`` on the same state directory is refused by the
    kernel, not by a flag -- deterministically, from the same process -- and
    only the close that confirms the socket lets the flock go with it. A
    runtime that called itself finished while the lock was still held used
    to keep the second instance out forever.
    """
    rig = _StartFailureRig(tmp_path, close_failures=2)
    with pytest.raises(RuntimeError):
        rig.runtime.start()
    second = managed_process_lock(tmp_path)

    # The socket has not been confirmed closed, so the lock is really held.
    with pytest.raises(StateDirLocked) as refused:
        second.acquire()
    assert refused.value.path == tmp_path / LOCK_NAME

    # The close confirms the socket, withdraws the entry, lets the lock go.
    assert rig.runtime.close() is True
    assert rig.lock.acquired is False
    # A refused capability is spent; a fresh central capability now succeeds.
    second = managed_process_lock(tmp_path)
    second.acquire()
    second.release()


def test_close_recovers_socket_descriptor_lock_in_confirmation_order(
    tmp_path,
) -> None:
    """The recovery is the close: socket, then descriptor, then lock.

    The first two ``server_close()`` asks refused -- one inside the start's
    own undo, one inside the runtime's rollback -- so the ``close()`` that
    follows a failed start is the one that finally confirms the socket. Only
    that confirmation lets the tail run: the descriptor fact is withdrawn
    and the lock is released, exactly once, after the socket -- never before
    it, never twice.
    """
    rig = _StartFailureRig(tmp_path, close_failures=2)
    with pytest.raises(RuntimeError):
        rig.runtime.start()
    # The socket is not confirmed yet, so the lock has not moved.
    assert rig.trace.count("lock_release") == 0
    assert rig.lock.acquired is True

    assert rig.runtime.close() is True
    assert rig.runtime.state == "closed"
    assert rig.server is not None and rig.server.close_calls == 3
    assert rig.server.closed is True
    # The descriptor fact is gone and the lock was let go exactly once,
    # after the socket was confirmed -- the whole story in one trace.
    assert rig.runtime.descriptor is None
    assert rig.lock.acquired is False
    assert rig.trace == [
        "bind",
        "write_descriptor",
        "server_close",
        "server_close",
        "server_close",
        "lock_release",
    ]


def test_a_failed_start_whose_undo_completes_is_failed_and_empty(tmp_path) -> None:
    """``failed`` may only mean "undid itself completely".

    One refused ``server_close()`` is retried by the undo itself: the socket
    is confirmed, the entry withdrawn and the lock released -- and only then
    is the start ``failed``. A ``failed`` that still held the lock was a
    terminal state keeping a second instance out forever.
    """
    rig = _StartFailureRig(tmp_path, close_failures=1)
    with pytest.raises(RuntimeError) as caught:
        rig.runtime.start()

    # The start failure, not the undo's, is what the caller sees.
    assert caught.value is rig.start_error
    # The undo finished, so the terminal state is honest: nothing held.
    assert rig.runtime.state == "failed"
    assert rig.lock.acquired is False
    assert rig.lock_is_held() is False
    assert read_entry_descriptor(tmp_path) is None
    assert rig.trace == [
        "bind",
        "write_descriptor",
        "server_close",
        "server_close",
        "lock_release",
    ]
