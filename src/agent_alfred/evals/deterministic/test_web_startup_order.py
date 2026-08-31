"""Dashboard startup ordering and rollback boundaries."""

from __future__ import annotations

import errno
import io
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    file_database,
    scripted_factory,
)
from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    PortUnavailable,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.server import DashboardRuntime
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard

# --- single-process Host startup ordering ---------------------------------
#
# #23 §1 and §2: the default command starts HTTP on a daemon thread and then
# enters the CLI, on the same Host; and the state-directory lock is the first
# process-level ownership taken, before anything is written or bound. They
# are one finding here because the second is what makes the first safe: a CLI
# that opened the database before it owned the state directory would migrate
# and recover another instance's data on its way to being refused.


class _RecordingLock(ProcessLock):
    """A real flock that writes down when it was taken and when it went."""

    def __init__(self, path: Path, log: list[str]) -> None:
        super().__init__(path)
        self._log = log

    def acquire(self) -> None:
        self._log.append("lock")
        super().acquire()

    def release(self) -> None:
        self._log.append("unlock")
        super().release()


class _FakeHost:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.process_instance_id = "inst-steps"
        self.started = False
        self.closed = False
        self.mutating = False

    def start(self) -> None:
        self._log.append("host.start")
        self.started = True

    def close(self, timeout: float | None = None) -> bool:
        self._log.append("host.close")
        self.closed = True
        return True

    def create_session(self) -> str:
        assert self.mutating
        self._log.append("session.create")
        return "session-steps"

    def try_begin_mutation(self) -> str | None:
        self._log.append("mutation.begin")
        if self.mutating:
            return "mutation_in_flight"
        self.mutating = True
        return None

    def end_mutation(self) -> None:
        assert self.mutating
        self._log.append("mutation.end")
        self.mutating = False


class _FakeBroker:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("broker.start")

    def close(self, timeout: float = 0.0) -> bool:
        self._log.append("broker.close")
        return True


class _FakeBoundServer:
    daemon_threads = True
    block_on_close = False

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.server_address = ("127.0.0.1", 17717)
        self.closed = False
        self.context = None

    def serve_forever(self) -> None:
        self._log.append("serve")

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self._log.append("socket.close")
        self.closed = True


def _step_runtime(tmp_path, *, log, bind_error=None, describe_error=None):
    """A runtime whose every process-level step is a line in ``log``."""

    def server_factory(address, handler):
        log.append("bind")
        if bind_error is not None:
            raise bind_error
        return _FakeBoundServer(log)

    def write_descriptor(directory, descriptor):
        log.append("describe")
        if describe_error is not None:
            raise describe_error
        return write_entry_descriptor(directory, descriptor)

    def open_database(directory):
        log.append("database")
        return sqlite3.connect(":memory:", check_same_thread=False)

    def assemble(conn, instance_id):
        log.append("assemble")
        return _FakeHost(log), _FakeBroker(log)

    return DashboardRuntime(
        state_dir=tmp_path,
        assemble=assemble,
        port=17717,
        instance_id="inst-steps",
        open_database=open_database,
        server_factory=server_factory,
        write_descriptor=write_descriptor,
        lock=_RecordingLock(tmp_path / LOCK_NAME, log),
    )


def test_a_lock_conflict_touches_nothing_at_all(tmp_path) -> None:
    """The lock is the first process-level ownership, or there is no start.

    A second instance that migrated the database, recovered a Run or wrote
    Run state before being refused would have altered the first instance's
    facts on its way out of the door -- which is the thing the lock exists
    to prevent, and it only holds if nothing precedes it.
    """
    holder = ProcessLock(tmp_path / LOCK_NAME)
    holder.acquire()
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log)
    try:
        with pytest.raises(StateDirLocked):
            runtime.start()
    finally:
        holder.release()
    assert log == ["lock"]


def test_a_failed_bind_releases_the_lock_and_never_starts_the_host(tmp_path) -> None:
    log: list[str] = []
    runtime = _step_runtime(
        tmp_path, log=log, bind_error=OSError(errno.EADDRINUSE, "in use")
    )
    with pytest.raises(PortUnavailable):
        runtime.start()
    # Unwound completely: no descriptor, no database, no Host, lock back.
    assert log == ["lock", "bind", "unlock"]
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_a_failed_descriptor_closes_the_socket_and_releases_the_lock(
    tmp_path,
) -> None:
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log, describe_error=OSError("disk full"))
    with pytest.raises(OSError):
        runtime.start()
    assert "socket.close" in log
    assert log[-1] == "unlock"
    assert "database" not in log
    assert "host.start" not in log
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_the_successful_path_runs_the_steps_in_one_order(tmp_path) -> None:
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log)
    descriptor = runtime.start()
    try:
        assert log == [
            "lock",
            "bind",
            "describe",
            "database",
            "assemble",
            "broker.start",
            "host.start",
            "serve",
        ]
        assert descriptor.port == 17717
        assert read_entry_descriptor(tmp_path) == descriptor
    finally:
        started = len(log)
        runtime.close()
    # Closed in reverse and completely: the Host before the stream, the
    # socket and descriptor before the lock.
    undone = log[started:]
    assert "socket.close" in undone
    assert undone.index("host.close") < undone.index("broker.close")
    assert undone.index("broker.close") < undone.index("unlock")
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_a_second_instance_cannot_touch_the_first_instances_database(
    tmp_path,
) -> None:
    """Refused before it can migrate. Proven on a real database file."""
    first = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    first.start()
    try:
        before = (tmp_path / "db.sqlite3").read_bytes()
        log: list[str] = []
        second = _step_runtime(tmp_path, log=log)
        with pytest.raises(StateDirLocked):
            second.start()
        # Not one step: no bind, no database, no recovery, no Host.
        assert log == ["lock"]
        assert (tmp_path / "db.sqlite3").read_bytes() == before
        # The first instance is untouched: still described, still serving.
        assert read_entry_descriptor(tmp_path) is not None
    finally:
        first.close()


def test_the_cli_and_serve_paths_share_one_start_up_order(
    tmp_path, monkeypatch
) -> None:
    """One order, two surfaces: neither rediscovers it for itself."""
    from agent_alfred.gateway import cli as cli_module

    logs: dict[str, list[str]] = {}

    def build(**kwargs):
        assert "bind_host" not in kwargs
        log: list[str] = []
        runtime = _step_runtime(kwargs["state_dir"], log=log)
        logs["last"] = log
        return runtime

    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: (_ for _ in ()).throw(EOFError())
    )
    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            port=17717,
            out=io.StringIO(),
            stop=stop,
            build=build,
        )
        == 0
    )
    serve_log = list(logs["last"])
    # No ``-m``: the REPL's first read raises EOFError, which is how a
    # terminal exits. The order is what is under test, not the chat.
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", "17717"],
            build=build,
        )
        == 0
    )
    cli_log = list(logs["last"])
    # Both took the lock first, bound second, described third, and only then
    # opened the database and built the Host.
    assert serve_log[:3] == ["lock", "bind", "describe"]
    assert cli_log[:3] == ["lock", "bind", "describe"]
    assert cli_log.index("mutation.begin") < cli_log.index("session.create")
    assert cli_log.index("session.create") < cli_log.index("mutation.end")
    assert cli_log.index("mutation.end") < cli_log.index("host.close")
