"""Shared DashboardRuntime ownership and close-progress harnesses."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    ProcessLock,
    StateDirLocked,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.server import ROLLBACK_STEP_TIMEOUT_S, DashboardRuntime


class CloseTrackingConnection:
    """A database that behaves like a closed one once it is closed."""

    def __init__(self, trace: list[str]):
        self._trace = trace
        self.close_calls = 0
        self.closed = False
        self.uses = 0

    def execute(self, sql: str = "SELECT 1") -> "CloseTrackingConnection":
        if self.closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        self.uses += 1
        return self

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._trace.append("conn_close")


class CloseTrackingHost:
    """The Host as the Dashboard sees it: one ``close`` that may refuse."""

    def __init__(
        self, conn: CloseTrackingConnection, results: list[bool], trace: list[str]
    ):
        self._conn = conn
        self._results = list(results)
        self._trace = trace
        self.close_calls = 0
        self.start_calls = 0
        self.worker_uses = 0
        self.saw_timeouts: list[float | None] = []

    def start(self) -> None:
        self.start_calls += 1

    def close(self, timeout: float | None = None) -> bool:
        self.close_calls += 1
        self.saw_timeouts.append(timeout)
        self._trace.append("host_close")
        # A worker is still inside its Run. It reaches for the database
        # *while* the shutdown is being negotiated, which is exactly the
        # moment a runtime that closed the database first would have taken
        # it away.
        self._conn.execute("SELECT 1 FROM runs")
        self.worker_uses += 1
        return self._results.pop(0) if self._results else True


class _FakeBroker:
    def __init__(
        self,
        conn: CloseTrackingConnection,
        results: list[bool],
        trace: list[str],
    ):
        self._conn = conn
        self._results = list(results)
        self._trace = trace
        self.close_calls = 0
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def close(self, timeout: float | None = None) -> bool:
        self.close_calls += 1
        self._trace.append("broker_close")
        return self._results.pop(0) if self._results else True


class RecordingServer:
    """Stands in for the bound socket. Never listens."""

    def __init__(self, address, handler):
        self.server_address = address
        self.handler = handler
        self.shut_down = False
        self.closed = False

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        self.shut_down = True

    def server_close(self) -> None:
        self.closed = True


class RecordingProcessLock(ProcessLock):
    """A real flock that records when it is let go."""

    def __init__(self, path: Path, trace: list[str]):
        super().__init__(path)
        self._trace = trace

    def release(self) -> None:
        if self._fd is not None:
            self._trace.append("lock_release")
        super().release()


class DashboardCloseRig:
    """A DashboardRuntime whose every seam is a counter or a latch."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        host_results: tuple[bool, ...] = (True,),
        broker_results: tuple[bool, ...] = (True,),
        spawn: Any = None,
        rollback_step_timeout: float | None = ROLLBACK_STEP_TIMEOUT_S,
    ):
        self.tmp_path = Path(tmp_path)
        self.trace: list[str] = []
        self.host_results = list(host_results)
        self.broker_results = list(broker_results)
        self.binds = 0
        self.database_opens = 0
        self.assemblies = 0
        self.states_seen: list[str] = []
        self.on_assemble: Any = None
        self.host: CloseTrackingHost | None = None
        self.broker: _FakeBroker | None = None
        self.conn = self._make_conn()
        self.lock = self._make_lock()
        self.runtime = DashboardRuntime(
            state_dir=self.tmp_path,
            assemble=self._assemble,
            port=7717,
            instance_id="inst-close-helper",
            open_database=self._open_database,
            server_factory=self._server_factory,
            write_descriptor=self._write_descriptor,
            lock=self.lock,
            spawn=spawn,
            rollback_step_timeout=rollback_step_timeout,
        )

    # The two seams a later closure needs to make refusable: the database
    # and the lock are built here so a subclass can stand in its own.
    def _make_conn(self) -> CloseTrackingConnection:
        return CloseTrackingConnection(self.trace)

    def _make_lock(self) -> ProcessLock:
        return RecordingProcessLock(self.tmp_path / LOCK_NAME, self.trace)

    def _server_factory(self, address, handler):
        self.binds += 1
        self.trace.append("bind")
        return RecordingServer(address, handler)

    def _open_database(self, directory):
        self.database_opens += 1
        self.trace.append("open_db")
        return self.conn

    def _write_descriptor(self, directory, descriptor):
        self.trace.append("write_descriptor")
        return write_entry_descriptor(directory, descriptor)

    def _assemble(self, conn, instance_id):
        self.assemblies += 1
        self.trace.append("assemble")
        self.states_seen.append(self.runtime.state)
        if self.on_assemble is not None:
            self.on_assemble(self)
        self.host = CloseTrackingHost(conn, self.host_results, self.trace)
        self.broker = _FakeBroker(conn, self.broker_results, self.trace)
        return self.host, self.broker

    # -- what a stuck Host's worker still does -----------------------------

    def blocked_worker_uses_the_database(self) -> int:
        """What a worker still finishing its Run does with the connection."""
        self.conn.execute("INSERT INTO runs VALUES (1)")
        return self.conn.uses

    def lock_is_held(self) -> bool:
        """Whether some *other* holder would be refused the state directory."""
        if not self.lock.acquired:
            return False
        probe = ProcessLock(self.tmp_path / LOCK_NAME)
        try:
            probe.acquire()
        except StateDirLocked:
            return True
        probe.release()
        return False


class SpawnThatRefuses:
    """A spawn seam that refuses before creating a serving thread."""

    def __init__(self):
        self.calls = 0

    def __call__(self, target):
        self.calls += 1
        raise RuntimeError("cannot create the serving thread")


class _ThreadThatWillNotStart:
    def start(self):
        raise RuntimeError("cannot start the serving thread")


class SpawnThatStartsBadly:
    """A spawn seam whose returned thread refuses to start."""

    def __init__(self):
        self.calls = 0

    def __call__(self, target):
        self.calls += 1
        return _ThreadThatWillNotStart()
