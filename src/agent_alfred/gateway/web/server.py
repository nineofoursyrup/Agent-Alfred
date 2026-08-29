"""The Dashboard as one object: lock, socket, Host, stream, and API.

This is the single owner of start-up, and the order it runs is the whole
reason it is one object. The decided sequence (#23 §2, and #28's fourth
review finding) is::

    state-directory lock  ->  bind 127.0.0.1:<port>  ->  write descriptor
      ->  open (and migrate) the database  ->  build Host and broker
      ->  start the dispatcher  ->  Host.start() (recovery)  ->  serve

The lock comes first because every later step is a process-level fact that a
second instance must not be allowed to perform: migrating the database,
recovering a Run, and binding the port are exactly the three things the lock
exists to keep to one owner. Splitting them across two components would put
the seam between two failure paths, and neither side could then decide when
to release the lock -- leaving "lock held, nothing bound, descriptor still
describes the previous instance" as a reachable state.

Nothing here is done twice and nothing is done early: the listening socket
exists before the database is touched, the descriptor before the Host has
recovered, and serving before nothing else is left to fail.

Closing runs the list backwards: stop serving, stop the stream, stop the
Host, close the database, forget the descriptor, drop the socket, drop the
lock.
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.guard import RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_PORT,
    DashboardService,
    EntryDescriptor,
    ProcessLock,
)
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.work import SubmitRequest, SubmitResult

# What :class:`DashboardRuntime` needs from the outside to build a Host and a
# broker around one connection. Injected by the assembly layer, which is the
# only place that knows about settings, factories and trace roots -- and the
# seam a test replaces to prove the order without a real Host.
AssembleHost = Callable[[sqlite3.Connection, str], tuple[RuntimeHost, SSEBroker]]


class HostFacade:
    """The Dashboard's view of the Host: the read side plus one write gate.

    It adds no state and makes no decisions. Every write goes through
    ``submit``, which is the same gate the CLI uses -- that is what makes
    "at most one Run at a time" true of the process rather than of a
    particular surface.
    """

    def __init__(self, host: RuntimeHost):
        self._host = host

    def create_session(self) -> str:
        return self._host.create_session()

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._host.submit(request)

    # The mutation gate asks the Host, not a lock of its own: the question
    # "is anything being written right now" has to include the Run whose
    # lease spans the whole Run.
    def try_begin_mutation(self) -> str | None:
        return self._host.try_begin_mutation()

    def end_mutation(self) -> None:
        self._host.end_mutation()

    def mutation_in_flight(self) -> bool:
        return self._host.mutation_in_flight()

    def session_exists(self, session_id: str) -> bool:
        return self._host.session_exists(session_id)

    def snapshot(self) -> Any:
        return self._host.snapshot()

    def list_sessions(self, *, limit: int, cursor: str | None = None) -> Any:
        return self._host.list_sessions(limit=limit, cursor=cursor)

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None = None
    ) -> Any:
        return self._host.open_session(
            session_id, page_size=page_size, cursor=cursor
        )

    def list_runs(
        self, *, filter: str, limit: int, cursor: str | None = None
    ) -> Any:
        return self._host.list_runs(filter=filter, limit=limit, cursor=cursor)

    def locate_run(self, run_id: str, *, limit: int) -> Any | None:
        return self._host.locate_run(run_id, limit=limit)

    def mainbar_pairs(self, *, limit: int, cursor: str | None = None) -> Any:
        return self._host.mainbar_pairs(limit=limit, cursor=cursor)


class DashboardRuntime:
    """The Dashboard as one object: lock, socket, Host, stream, and API.

    Construction does nothing that another instance could notice -- no lock,
    no socket, no database. :meth:`start` is where process-level ownership
    is taken, and it takes it in one order only.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        assemble: AssembleHost,
        port: int = DEFAULT_PORT,
        instance_id: str | None = None,
        csrf_token: str | None = None,
        open_database: Callable[[Path], sqlite3.Connection] | None = None,
        server_factory: Any = None,
        write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
        lock: ProcessLock | None = None,
        pid: int | None = None,
    ):
        self._state_dir = state_dir
        self._assemble = assemble
        self._port = port
        # Minted here rather than by the Host because the descriptor is
        # written before the Host exists -- the bind has to precede opening
        # the database, and building a Host means opening it. The Host still
        # *holds* this identity (CONTEXT.md: RuntimeHost owns
        # ``process_instance_id`` and the allocation of ``seq``); assembly
        # only chooses the value, so the descriptor, every snapshot and
        # every SSE cursor name one and the same process.
        self._instance_id = instance_id or uuid.uuid4().hex
        # One token per process, minted here so it cannot be shared with
        # anything that outlives this process. A cross-origin page cannot
        # read it (no Access-Control-Allow-Origin is ever sent), which is
        # what makes handing it out on a GET safe.
        self._csrf_token = csrf_token or secrets.token_urlsafe(32)
        self._open_database = open_database
        self._service = DashboardService(
            state_dir=state_dir,
            handler=DashboardHandler,
            instance_id=self._instance_id,
            port=port,
            server_factory=server_factory,
            write_descriptor=write_descriptor,
            lock=lock,
            pid=pid,
        )
        self._host: RuntimeHost | None = None
        self._broker: SSEBroker | None = None
        self._conn: sqlite3.Connection | None = None
        self._thread: Any = None

    # -- reads ------------------------------------------------------------

    @property
    def host(self) -> RuntimeHost | None:
        """The one Host. ``None`` until :meth:`start` has built it."""
        return self._host

    @property
    def broker(self) -> SSEBroker | None:
        return self._broker

    @property
    def csrf_token(self) -> str:
        return self._csrf_token

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def service(self) -> DashboardService:
        return self._service

    @property
    def port(self) -> int:
        return self._service.port

    @property
    def descriptor(self) -> EntryDescriptor | None:
        return self._service.descriptor

    @property
    def started(self) -> bool:
        return self._service.started and self._host is not None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> EntryDescriptor:
        """Take ownership, then build, then serve. In that order or not at all.

        Every failure undoes exactly the steps that succeeded, in reverse,
        and the whole thing is one object's job rather than a convention
        shared between an assembler and a service.
        """
        service = self._service
        if service.started:
            assert service.descriptor is not None
            return service.descriptor
        # 1-3. The lock, the socket and the descriptor. Nothing below this
        #      line is reachable unless all three are held, so a second
        #      instance never migrates, recovers or writes.
        descriptor = service.start()
        try:
            # 4. The database: opened (and migrated) only once this process
            #    is the one that owns the state directory.
            conn = self._open_conn()
            self._conn = conn
        except BaseException:
            service.close()
            raise
        try:
            # 5. The Host and the broker, around that one connection.
            host, broker = self._assemble(conn, self._instance_id)
            self._host, self._broker = host, broker
            # The guard depends on the port that was actually bound, which
            # is only knowable now, so the context is attached here -- still
            # before anything can be accepted.
            service.attach_context(
                HandlerContext(
                    guard=RequestGuard(
                        port=service.port, csrf_token=self._csrf_token
                    ),
                    api=DashboardApi(facade=HostFacade(host)),
                    broker=broker,
                    instance_id=self._instance_id,
                )
            )
            # 6. The dispatcher starts before the Host recovers, so an event
            #    published during recovery is delivered to the first
            #    connection that arrives rather than left sitting in a queue
            #    nobody is draining.
            broker.start()
            # 7. Recovery and the worker. The Host's last chance to fail
            #    before the port is open to a browser.
            host.start()
        except BaseException:
            self._stop_all()
            raise
        # 8. Serving, once nothing else is left that can fail.
        self._thread = service.start_serving()
        return descriptor

    def close(self, timeout: float | None = None) -> bool:
        """Undo the whole list, from the outside in. Idempotent.

        Returns False when the stream or the Host could not be brought down
        within the timeout -- an honest "not closed" -- but the listening
        socket, the descriptor and the lock are released either way: a
        half-dead instance must not hold the state directory hostage.
        """
        return self._stop_all(timeout)

    # -- internals --------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        if self._open_database is not None:
            return self._open_database(self._state_dir)
        from agent_alfred.wiring import open_database

        return open_database(self._state_dir)

    def _stop_all(self, timeout: float | None = None) -> bool:
        service = self._service
        # 8. Stop accepting first: nothing new may arrive while the rest is
        #    being wound down.
        service.stop_serving()
        host, self._host = self._host, None
        broker, self._broker = self._broker, None
        conn, self._conn = self._conn, None
        stopped = True
        # 7-6. The Host before the stream it emits into, and the stream
        #      before the socket: a late patch must not re-prime a stream
        #      whose Host is already gone.
        if host is not None:
            stopped = (
                host.close() if timeout is None else host.close(timeout)
            )
        if broker is not None:
            drained = (
                broker.close()
                if timeout is None
                else broker.close(timeout=timeout)
            )
            stopped = drained and stopped
        # 5. The database last of the process's own state.
        if conn is not None:
            conn.close()
        # 3-1. The descriptor stops claiming a port this process no longer
        #      answers, and only then does the lock go.
        service.close()
        self._thread = None
        return stopped
