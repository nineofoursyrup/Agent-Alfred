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

Closing runs the list backwards -- stop accepting, stop the Host, stop the
stream, close the database, forget the descriptor, drop the lock -- but it
runs **as far as it gets, and no further**. A Host that will not stop within
the timeout means a worker is still inside its Run, and that worker is still
using the database this process opened. So a refused stop ends the close
there: the listening socket is shut, the runtime sits in ``closing``, and
the database, the descriptor and the lock are all still held. The next
``close()`` resumes from the step that did not finish rather than starting
over, and the tail -- database, descriptor, lock -- runs only once both the
Host and the stream have confirmed they are down.

Releasing any of them early is worse than not closing: a descriptor naming a
dead port sends a browser into a reconnect loop, a closed database crashes
the worker mid-Run, and a released lock lets a second instance take write
authority over state that is still being written. "Not closed yet" is an
answer this process can give honestly; "closed, but a worker is still
writing" is not.

The eight steps are one state machine, not eight independent steps
(``new`` -> ``starting`` -> ``running`` -> ``closing`` -> ``closed``, with
``failed`` for a start that undid itself), and one lock covers checking the
state, doing the start, publishing the state and doing the close. Two
threads calling ``start()`` therefore produce one Dashboard and two copies
of the same descriptor, and a ``close()`` that arrives while a ``start()``
is in flight waits for it instead of closing a database that is still being
opened.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from agent_alfred.database import open_database
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.guard import RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_PORT,
    DashboardService,
    EntryDescriptor,
    ProcessLock,
    SpawnThread,
)
from agent_alfred.managed_state import ManagedStateLease
from agent_alfred.resource_rollback import (
    IncompleteRollback,
    ResumableRollback,
    RollbackSlot,
)
from agent_alfred.runtime.host import RuntimeHost

# What :class:`DashboardRuntime` needs from the outside to build a Host and a
# broker around one connection. Injected by the assembly layer, which is the
# only place that knows about settings, factories and trace roots -- and the
# seam a test replaces to prove the order without a real Host.
AssembleHost = Callable[[sqlite3.Connection, str], tuple[RuntimeHost, SSEBroker]]


class DatabaseOpener(Protocol):
    """Open the Dashboard database inside its caller-owned rollback."""

    def __call__(
        self,
        state: ManagedStateLease,
        *,
        _rollback: ResumableRollback,
    ) -> sqlite3.Connection: ...


# Where the Dashboard is in its one life. Closed and failed are both
# terminal: neither is a state a Dashboard comes back from.
DashboardState = Literal["new", "starting", "running", "closing", "closed", "failed"]

# How long each **step** of a failed start's rollback may wait, when the
# caller has not said. Not the whole rollback: every step that has not run
# yet gets its own bound, which is the same reading
# :meth:`DashboardRuntime.close` gives its ``timeout``.
#
# Bounded on purpose, and not borrowed from the components' own defaults the
# way ``close()`` borrows them. ``close()`` may be asked again, so an expired
# wait there costs the caller a second call; a rollback is the one close that
# has to *return*, because the caller is waiting to be told why the
# Dashboard did not come up.
#
# And on that path nothing has been admitted yet, on any surface. The HTTP
# one needs step 8, which is the step whose failure is the interesting case;
# the CLI one submits to the same Host directly, but only after
# ``start()`` has returned (gateway/cli.py starts the Dashboard before its
# first prompt). Recovery, step 7, rewrites the index rows of interrupted
# Runs rather than executing them. So what the rollback waits on is a worker
# and a dispatcher with empty queues. If that turns out to be wrong, the
# honest answer is the ``closing`` state -- which keeps the database, the
# descriptor and the lock, and lets the caller ask again -- not a longer
# wait.
ROLLBACK_STEP_TIMEOUT_S = 2.0


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
        open_database: DatabaseOpener | None = None,
        server_factory: Any = None,
        write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
        lock: Callable[[Any], ProcessLock] | None = None,
        pid: int | None = None,
        spawn: SpawnThread | None = None,
        rollback_step_timeout: float | None = ROLLBACK_STEP_TIMEOUT_S,
        construction_rollback: RollbackSlot | None = None,
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
        # The seam that makes step 8's failures reachable: a serving thread
        # that cannot be created, and one that cannot be started. Both are
        # failures like any other and both undo steps 1-7.
        self._spawn = spawn
        # How long each step of a failed start's rollback may wait. Defaults
        # to :data:`ROLLBACK_STEP_TIMEOUT_S`, not to the components' own
        # answers the way ``close()`` uses them.
        self._rollback_step_timeout = rollback_step_timeout
        self._construction_rollback = construction_rollback
        self._host: RuntimeHost | None = None
        self._broker: SSEBroker | None = None
        self._conn: sqlite3.Connection | None = None
        self._database_owner: ResumableRollback | None = None
        self._thread: Any = None
        # One lock for the whole lifecycle: the check, the start, the state
        # it publishes and the close transitions. Re-entrant because the
        # rollback path closes from inside the start that holds it.
        self._lifecycle_lock = threading.RLock()
        self._state: DashboardState = "new"
        # Which of the two things that can refuse to stop have actually
        # stopped. Both start "stopped" because before start() there is
        # nothing to stop.
        self._host_stopped = True
        self._broker_stopped = True
        # The tail's real progress: which of its steps have actually
        # succeeded. "Released" is not a flag raised when the tail *begins*
        # -- a database that refuses to close, or a lock that refuses to
        # let go, leaves the tail unfinished, and the next close() resumes
        # from exactly the step that refused. Both start unrun for the same
        # reason ``_host_stopped`` starts True: before start() there is
        # nothing here to release.
        self._db_closed = False
        self._entry_released = False
        # Whether steps 1-3 ever completed -- or were taken and are still
        # held by a start whose own undo refused -- i.e. whether this
        # process owns the socket, the descriptor and the lock. Without it a
        # start that was refused at the lock would go on to release a lock
        # it never held -- and "released a lock it never held" is only
        # harmless because ``ProcessLock`` happens to be idempotent.
        self._entry_owned = False

    # -- reads ------------------------------------------------------------

    @property
    def host(self) -> RuntimeHost | None:
        """The one Host this runtime owns.

        ``None`` until :meth:`start` has built it, and ``None`` again once a
        close has run to the end. In between there is a third answer: while a
        close is still waiting for the Host to stop, the reference is still
        here, because it is what the next ``close()`` has to ask.
        """
        return self._host

    @property
    def broker(self) -> SSEBroker | None:
        """The one stream this runtime owns. Same three answers as :attr:`host`."""
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
        return self.state == "running"

    @property
    def state(self) -> DashboardState:
        """Where this Dashboard is in its one life.

        Read under the same lock that moves it, so a caller never sees a
        state that is half-published: ``state == "running"`` and "the
        descriptor is on disk and the Host is up" are the same observation.
        """
        with self._lifecycle_lock:
            return self._state

    # -- lifecycle --------------------------------------------------------

    def start(self) -> EntryDescriptor:
        """Take ownership, then build, then serve. In that order or not at all.

        Every failure undoes exactly the steps that succeeded, in reverse,
        and the whole thing is one object's job rather than a convention
        shared between an assembler and a service.

        The check, the start and the state it publishes are one critical
        section, so two callers produce one Dashboard: the second waits for
        the first and is handed the same descriptor.
        """
        with self._lifecycle_lock:
            if self._state == "running":
                descriptor = self._service.descriptor
                assert descriptor is not None
                return descriptor
            if self._state != "new":
                # Closing, closed and failed are all terminal. Restarting a
                # Dashboard that was closed would take the lock, migrate the
                # database and mint a second instance id behind the back of
                # whoever closed it.
                raise RuntimeError(
                    f"this Dashboard is {self._state}; start() runs once"
                )
            self._state = "starting"
            try:
                descriptor = self._start_locked()
            except BaseException as start_failure:
                if self._construction_rollback is not None:
                    self._construction_rollback.capture_failure(start_failure)
                # A start that failed inside the service may still own what
                # it took: the service's own undo can refuse, and then the
                # socket and the lock are still held even though this loop
                # never got to mark the entry below. The lock is taken
                # before the socket exists and let go last, so ``lock_held``
                # is the one fact that says the entry was taken; a start
                # refused *at* the lock holds nothing, and must leave the
                # winner's descriptor alone.
                if self._service.lock_held:
                    self._entry_owned = True
                start_is_control = isinstance(
                    start_failure, (KeyboardInterrupt, SystemExit, GeneratorExit)
                )
                construction_cause = start_failure.__cause__
                if (
                    start_is_control
                    and isinstance(construction_cause, IncompleteRollback)
                    and self._construction_rollback is not None
                    and self._construction_rollback.owns(construction_cause.owner)
                ):
                    # Assembly already attempted all rollback steps and put
                    # its resumable owner on this exact exception. Preserve
                    # that observable progress until the caller retries.
                    self._state = "closing"
                    raise
                # One rollback, run once, for all eight steps. A rollback
                # that cannot stop the Host has not finished, so it stays in
                # ``closing`` still owning everything; one that ran to the
                # end is simply ``failed``, with nothing left. A rollback
                # whose tail *raises* -- a database that refuses to close --
                # has also not finished: it stays in ``closing`` too, and
                # the next close() resumes from the step that refused and
                # reports that failure there. The start failure is the one
                # the caller is waiting for, so it is the one re-raised.
                rollback_done = False
                try:
                    rollback_done = self._stop_all_locked(self._rollback_step_timeout)
                except BaseException as cleanup_failure:
                    rollback_done = False
                    self._state = "closing"
                    if isinstance(
                        cleanup_failure,
                        (KeyboardInterrupt, SystemExit, GeneratorExit),
                    ):
                        if isinstance(
                            cleanup_failure.__cause__, IncompleteRollback
                        ):
                            raise cleanup_failure
                        raise cleanup_failure from start_failure
                self._state = "failed" if rollback_done else "closing"
                raise
            self._state = "running"
            return descriptor

    def _start_locked(self) -> EntryDescriptor:
        """Steps 1-8. The caller owns the rollback for all of them.

        Deliberately no ``try`` of its own: eight steps with two rollback
        paths is how a failure ends up being undone twice, and an undone
        twice is a Host asked to stop twice and a close that "finishes"
        because the second ask happened to succeed.
        """
        service = self._service
        # 1-3. The lock, the socket and the descriptor. Nothing below this
        #      line is reachable unless all three are held, so a second
        #      instance never migrates, recovers or writes. ``service.start``
        #      undoes its own steps.
        descriptor = service.start(rollback_step_timeout=self._rollback_step_timeout)
        self._entry_owned = True
        state = service.managed_state
        assert state is not None
        state.reclaim_stale_trace_staging()
        # 4. The database: opened (and migrated) only once this process is
        #    the one that owns the state directory.
        conn = self._open_conn()
        self._conn = conn
        # 5. The Host and the broker, around that one connection.
        host, broker = self._assemble(conn, self._instance_id)
        self._host, self._broker = host, broker
        # Storing the pair is not yet owning it: the close path skips whatever
        # is still marked stopped, so these bits are what make this runtime an
        # effective owner. They fall first, and only then may the construction
        # owner retire -- the overlap is safe because both closes are
        # idempotent and resumable, an unowned Host is not.
        self._host_stopped = False
        self._broker_stopped = False
        if self._construction_rollback is not None:
            # Assembly kept its construction owner across the return edge
            # above, and across every interrupt point between it and here.
            self._construction_rollback.settle()
        # The guard depends on the port that was actually bound, which is
        # only knowable now, so the context is attached here -- still before
        # anything can be accepted.
        service.attach_context(
            HandlerContext(
                guard=RequestGuard(port=service.port, csrf_token=self._csrf_token),
                # The Host satisfies the DashboardFacade protocol itself, so
                # the API is built on it directly: every member it needs --
                # the reads, the snapshot and the mutation gate's authority --
                # is answered by the one object that owns the facts.
                api=DashboardApi(facade=host),
                broker=broker,
                instance_id=self._instance_id,
            )
        )
        # 6. The dispatcher starts before the Host recovers, so an event
        #    published during recovery is delivered to the first connection
        #    that arrives rather than left sitting in a queue nobody is
        #    draining.
        broker.start()
        # 7. Recovery and the worker. The Host's last chance to fail before
        #    the port is open to a browser.
        host.start()
        # 8. Serving. Inside the rollback boundary on purpose: a thread that
        #    cannot be created, or cannot be started, is a step that failed,
        #    and it undoes 1-7 exactly as a failure at step 5 would. Leaving
        #    it outside would produce a Dashboard that reports "failed to
        #    start" while its Host, its worker and its open database are all
        #    still running behind it.
        self._thread = service.start_serving(self._spawn)
        return descriptor

    def close(self, timeout: float | None = None) -> bool:
        """Undo the whole list, from the outside in. Resumable and idempotent.

        Returns False when the Host or the stream could not be brought down
        within the timeout -- an honest "not closed". New HTTP admission has
        stopped, but the database, the descriptor and the lock are all still
        held: a worker is still inside its Run and those are exactly the
        things it is using. Calling ``close()`` again resumes from the step
        that did not finish and, once the Host and the stream have both
        confirmed they are down, runs the tail exactly once.

        A half-dead instance holding the state directory is not hostage-taking;
        it is the alternative to a second instance taking write authority
        over a Run that is still being recorded.

        ``timeout`` bounds *each* step that has not run yet, not the whole
        close, and ``None`` hands every component its own default -- which is
        why this one may wait a lot longer than a failed start's rollback,
        whose bound is :data:`ROLLBACK_STEP_TIMEOUT_S`.
        """
        with self._lifecycle_lock:
            if self._state in ("closed", "failed"):
                # Both are terminal and both left nothing behind. Re-running
                # the steps would be pointless.
                return True
            self._state = "closing"
            if not self._stop_all_locked(timeout):
                return False
            self._state = "closed"
            return True

    # -- internals --------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        state = self._service.managed_state
        assert state is not None
        owner = ResumableRollback()
        self._database_owner = owner
        if self._open_database is not None:
            conn = self._open_database(state, _rollback=owner)
        else:
            conn = open_database(state, _rollback=owner)
        owner.own(conn)
        self._conn = conn
        return conn

    def _stop_all_locked(self, timeout: float | None) -> bool:
        """Run every step of the close that has not run yet.

        True means all of them ran. False means one of them refused, and
        everything the refusing step may still be using is still here --
        including the database, the descriptor and the lock. Call it again
        with the same intent and it resumes from the step that refused.

        ``timeout`` bounds *each* remaining step, not the whole close, and
        ``None`` means "each component's own default" -- this layer does not
        know how long a Run takes and does not pretend to.
        """
        service = self._service
        # 8. Stop accepting first: nothing new may arrive while the rest is
        #    being wound down.
        if not service.stop_serving(timeout=timeout):
            return False
        # An assembler can fail after creating a Host but before returning
        # it.  Its typed owner must finish before this runtime releases the
        # borrowed database or any earlier process-level capability.
        if self._construction_rollback is not None:
            if not self._construction_rollback.retry():
                return False
        # 7. The Host before the stream it emits into. A worker that is still
        #    inside its Run owns the database, so a refused stop ends here:
        #    tearing the Broker down under a Host that is still publishing
        #    would take the stream away from the very worker we are waiting
        #    for.
        if not self._host_stopped and self._host is not None:
            if not self._host.close(timeout=timeout):
                return False
            self._host_stopped = True
        # 6. The stream. A broker that has not drained still holds frames a
        #    client is owed, and those frames were built from this database.
        if not self._broker_stopped and self._broker is not None:
            # Spelled with the default rather than passed through because the
            # two defaults are not the same kind: ``RuntimeHost.close(None)``
            # falls back to its own worker-join bound, while
            # ``SSEBroker.close``'s default *is* a number and ``None`` would
            # be a TypeError. "Ask with no argument" is the one spelling both
            # read as "however long you need".
            drained = (
                self._broker.close()
                if timeout is None
                else self._broker.close(timeout=timeout)
            )
            if not drained:
                return False
            self._broker_stopped = True
        # 5-1. Only once nothing is running does this process give up what
        #      the running parts were using.
        self._release_locked()
        return True

    def _release_locked(self) -> None:
        """The tail: database, descriptor, socket, lock. Resumable progress.

        Each step advances its own bit only after the action behind it has
        returned, so a step that raises leaves everything a later attempt
        needs right where it is -- the open connection, the Host and broker
        references, the entry -- and the exception reaches the caller, who
        is told the close did not finish rather than being handed a
        completion that ran halfway and let go of a state directory whose
        database is still open.
        """
        if not self._db_closed:
            owner = self._database_owner
            if owner is not None:
                owner.close()
                self._database_owner = None
            else:
                conn = self._conn
                if conn is not None:
                    conn.close()
            self._db_closed = True
            self._conn = None
        if not self._entry_released:
            if self._entry_owned:
                # The descriptor stops claiming a port this process no
                # longer answers, and only then does the lock go. Not
                # reached when the start was refused before the lock was
                # taken: there is no entry to withdraw, and the lock is not
                # ours.
                self._service.close()
            self._entry_released = True
        # Everything that was being waited for has confirmed it stopped, so
        # this runtime owns none of it any more. The Host and the broker
        # are let go here and not before: a close that has not finished
        # still needs to ask them again.
        self._host = None
        self._broker = None
        self._thread = None
