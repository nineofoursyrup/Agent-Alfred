"""The Dashboard's single-instance lifecycle: lock, bind, describe.

The order is decided (#23 §2) and is not negotiable::

    state-directory process lock  ->  bind 127.0.0.1:<port>  ->  atomically
    write the entry descriptor

Every step is a place this process can fail, and each failure undoes exactly
the steps that succeeded. The three steps are one indivisible whole: if one
component took the lock and another bound the port, the seam would land
between two failure paths and nobody could decide when to release the lock --
leaving "lock held, nothing bound, descriptor still describes the previous
instance" as a reachable state.

Neither failure is worked around:

- a **lock conflict** means another instance owns the state directory. Two
  owners would split ``seq`` and ``process_instance_id``, and with them the
  one property the replay ring and the cursor depend on: that this process
  is the only writer of the facts it hands out.
- a **port conflict** is, under the single-user assumption, almost always
  "the previous instance did not exit cleanly". Choosing another port would
  silently point the user's bookmark at a service that is not theirs, so the
  honest answer is to fail and name the port.

The entry descriptor is written last because it is the only step that claims
anything to the outside world: until it exists there is no Dashboard to
connect to, and after it exists every field in it is true.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePath
from typing import Any

from agent_alfred.gateway.web.frames import validate_process_instance_id
from agent_alfred.managed_state import (
    ManagedFileLease,
    ManagedStateDirectory,
    ManagedStateLease,
)
from agent_alfred.resource_rollback import (
    BackgroundCloseStep,
    OwnedDescriptor,
    OwnedResource,
    ResumableRollback,
    RollbackSlot,
    thread_exit_confirmed,
    thread_start_effect_happened,
)

# The one and only bindable address. "Only listen locally" is one of four
# independent defences (ADR-0014) and it is the one that shrinks the attack
# surface rather than rejecting a request -- which is exactly why it is not a
# parameter. ``127.0.0.1`` is accepted and every other spelling is refused at
# construction, before a socket can be asked for: a value that reached the
# bind would already have made the other three defences the only thing
# standing between an unauthenticated Dashboard and the network.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7717

LOCK_NAME = "dashboard.lock"
DESCRIPTOR_NAME = "dashboard.json"

# ``pid_t`` is a signed integer on the POSIX systems this flock-based
# lifecycle supports.  Keeping the persisted/diagnostic boundary inside the
# positive signed-32-bit domain accepts every real PID those systems issue
# without turning an arbitrary Python integer into a purported OS process.
MIN_PID = 1
MAX_PID = (1 << 31) - 1
MAX_PID_DIGITS = len(str(MAX_PID))
MIN_PORT = 1
MAX_PORT = 65_535

# The seam ``start_serving`` takes: a target, in; an **unstarted** thread,
# out. Injected so both failures of the step are reachable from a test --
# "cannot be created" from the factory raising, "cannot be started" from the
# ``start()`` the caller performs itself.
#
# Unstarted on purpose, matching the broker seam: the construction owner must
# publish the concrete thread before ``start()`` can make it live, so either
# side of that effect has an explicit owner.
SpawnThread = Callable[[Callable[[], None]], Any]


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DashboardService",
    "EntryDescriptor",
    "PortUnavailable",
    "ProcessLock",
    "SpawnThread",
    "StateDirLocked",
    "read_entry_descriptor",
    "write_entry_descriptor",
]


class StateDirLocked(RuntimeError):
    """Another instance holds the state directory's process lock."""

    def __init__(self, path: Path, holder_pid: int | None = None):
        detail = f" (held by pid {holder_pid})" if holder_pid is not None else ""
        super().__init__(
            f"another instance holds {path}{detail}; "
            "this process will not take a second write authority"
        )
        self.path = path
        self.holder_pid = holder_pid


class PortUnavailable(RuntimeError):
    """The loopback port is taken. It is never silently exchanged for another."""

    def __init__(self, host: str, port: int, reason: str):
        super().__init__(
            f"cannot bind {host}:{port}: {reason}; "
            "the port is never chosen automatically"
        )
        self.host = host
        self.port = port
        self.reason = reason


class ProcessLock:
    """An exclusive, kernel-scoped lock on one file in the state directory.

    It is advisory (``flock``), not an ``O_EXCL`` marker file. The kernel
    drops an flock when its owner dies, so a crashed instance cannot leave a
    lock behind; a marker file would, and then "is the holder still alive"
    would become a question this process cannot answer -- guessing wrong
    either blocks a legitimate start or lets a second owner in.

    The pid written into the file is a diagnostic for a human reading the
    state directory, nothing more: it is not consulted to decide ownership.
    """

    def __init__(
        self,
        lease: ManagedFileLease,
        *,
        owned_state: ManagedStateLease | None = None,
    ):
        self._path = lease.path
        self._lease = lease
        self._owned_state = owned_state
        self._fd: int | None = None
        self._fd_owner: OwnedDescriptor | None = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        owner = self._fd_owner
        return owner is not None and owner.fd >= 0

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if self._closed:
            raise RuntimeError("a released ProcessLock capability cannot be reused")
        self._lease.verify_identity()
        rollback = ResumableRollback()
        capability = object()
        rollback.own(capability, self._close_capability)
        descriptor: OwnedDescriptor | None = None
        fd: int | None = None
        try:
            descriptor = self._lease.duplicate_fd(_rollback=rollback)
            fd = descriptor.fd
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                holder_pid = _recorded_pid(fd)
                raise StateDirLocked(self._path, holder_pid) from exc
            self._lease.write_lock_diagnostic(fd, b"pid=%d\n" % os.getpid())
            # Publish the descriptor while rollback still owns it. A control
            # exit before this STORE is rolled back; one after it leaves a
            # reachable owner for release() instead of an anonymous flock.
            self._fd_owner = descriptor
            self._fd = fd
        except BaseException as exc:
            if descriptor is not None and self._fd_owner is descriptor:
                # Publication won the race with process control. The
                # ProcessLock now owns both the descriptor and capability;
                # its caller can release them without guessing at the effect.
                raise
            # The lock may be held even though its descriptor was never
            # published. Undo the whole acquisition; closing the descriptor
            # drops the flock with it.
            rollback.raise_failure(exc)
        assert descriptor is not None
        rollback.transfer(descriptor)
        rollback.transfer(capability)

    def _close_capability(self) -> None:
        self._lease.close()
        if self._owned_state is not None:
            self._owned_state.close()
            self._owned_state = None
        self._closed = True

    def release(self) -> None:
        if self._closed:
            return
        owner = self._fd_owner
        if owner is None and self._fd is not None:
            owner = OwnedDescriptor(self._fd)
            self._fd_owner = owner
        if owner is not None:
            owner.close()
        self._fd = None
        self._fd_owner = None
        self._close_capability()

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _recorded_pid(fd: int) -> int | None:
    """The pid the lock file names, if it names one.

    Diagnostic only. A lock file with no readable pid still means exactly one
    thing -- somebody else holds it -- so a failed read reports "unknown"
    instead of guessing.
    """
    try:
        raw = os.pread(fd, len("pid=") + MAX_PID_DIGITS + 2, 0)
        text = raw.decode("utf-8")
    except OSError, UnicodeError:
        # This file is never evidence of ownership -- flock is.  A read or
        # decode failure therefore cannot replace the lock-conflict fact.
        return None
    for line in text.splitlines():
        if line.startswith("pid="):
            digits = line[len("pid=") :]
            if (
                digits.isascii()
                and digits.isdecimal()
                and len(digits) <= MAX_PID_DIGITS
                and digits[0] != "0"
            ):
                try:
                    pid = int(digits)
                except ValueError:
                    return None
                if MIN_PID <= pid <= MAX_PID:
                    return pid
            return None
    return None


def _exact_int_in_range(value: object, minimum: int, maximum: int) -> int:
    """One entry-integer boundary: exact int, then domain range."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer field has the wrong type or range")
    return value


@dataclass(frozen=True)
class EntryDescriptor:
    """The one entry fact: which instance, which process, which port.

    It is written only after the bind succeeded, so nothing in it can point
    at a service that is not running. It is the single source a bookmark,
    the ``Origin`` whitelist and the single-instance check all agree on.
    """

    instance_id: str
    pid: int
    port: int

    def __post_init__(self) -> None:
        validate_process_instance_id(self.instance_id)
        _exact_int_in_range(self.pid, MIN_PID, MAX_PID)
        _exact_int_in_range(self.port, MIN_PORT, MAX_PORT)

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "instance_id": self.instance_id,
                    "pid": self.pid,
                    "port": self.port,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


def write_entry_descriptor(directory: Path, descriptor: EntryDescriptor) -> Path:
    """Replace the descriptor atomically, or leave the old one in place.

    A reader either sees the whole previous descriptor or the whole new one.
    Writing in place could leave a truncated document at exactly the moment
    somebody is opening the Dashboard.
    """
    target = directory / DESCRIPTOR_NAME
    tmp = directory / f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp"
    rollback = RollbackSlot()
    temporary_rollback = ResumableRollback()
    descriptor_rollback = ResumableRollback()
    rollback.begin(temporary_rollback)
    rollback.begin(descriptor_rollback)
    temporary = object()

    def discard_temporary() -> None:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

    temporary_rollback.own(temporary, discard_temporary)
    try:
        fd = OwnedDescriptor.open(
            descriptor_rollback,
            tmp,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        remaining = memoryview(descriptor.to_json().encode("utf-8"))
        while remaining:
            written = os.write(fd.fd, remaining)
            if written == 0:
                raise OSError(errno.EIO, "zero-byte descriptor write")
            remaining = remaining[written:]
        os.fsync(fd.fd)
        fd.close()
        descriptor_rollback.transfer(fd)
        os.replace(tmp, target)
        temporary_rollback.transfer(temporary)
    except BaseException as exc:
        rollback.raise_failure(exc)
    rollback.close()
    return target


def read_entry_descriptor(directory: Path) -> EntryDescriptor | None:
    """The descriptor as it was last written, or None if there is none.

    A descriptor that exists but does not parse is an error, not a missing
    one: something wrote that file and whatever it wrote is not an entry.
    """
    path = directory / DESCRIPTOR_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
        if type(payload) is not dict:
            raise TypeError("entry descriptor must be a JSON object")
        return EntryDescriptor(
            instance_id=payload["instance_id"],
            pid=payload["pid"],
            port=payload["port"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path} is not a readable entry descriptor") from exc


def _bind_error_reason(exc: OSError) -> str:
    if exc.errno == errno.EADDRINUSE:
        return "address already in use"
    if exc.errno == errno.EACCES:
        return "permission denied"
    if exc.errno is not None:
        return f"{errno.errorcode.get(exc.errno, exc.errno)}"
    return type(exc).__name__


class DashboardService:
    """Owns the lock, the listening socket and the descriptor, in that order.

    Every seam is injected because every one of them is where this object
    can fail, and each failure has to be reachable from a test without a
    real second process.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        handler: Any,
        instance_id: str,
        port: int = DEFAULT_PORT,
        server_factory: Any = None,
        context: Any = None,
        pid: int | None = None,
        lock: Callable[[ManagedFileLease], "ProcessLock"] | None = None,
        write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
    ):
        # There is deliberately no address parameter. Refusing a value that
        # was never accepted is stronger than validating one that was: no
        # caller -- and no injected server factory, the seam every test uses
        # to avoid a real socket -- can carry another address to the bind.
        instance_id = validate_process_instance_id(instance_id)
        port = _exact_int_in_range(port, MIN_PORT, MAX_PORT)
        pid = _exact_int_in_range(
            os.getpid() if pid is None else pid, MIN_PID, MAX_PID
        )
        self._state_dir = state_dir
        self._handler = handler
        self._instance_id = instance_id
        self._requested_port = port
        self._server_factory = server_factory
        self._context = context
        self._pid = pid
        self._lock_factory = lock
        self._lock: ProcessLock | None = None
        self._managed_state: ManagedStateLease | None = None
        self._write_descriptor_fn = write_descriptor or write_entry_descriptor
        self._managed_descriptor = write_descriptor is None
        self._server: Any = None
        self._serving = False
        self._serving_thread: Any = None
        self._descriptor: EntryDescriptor | None = None
        self._descriptor_owned = False
        # The release's own progress: which of its steps have actually
        # succeeded. They live beside the references they are about, so a
        # close that resumes after a refusal picks up at the refused step
        # instead of re-running the whole list or skipping its remainder.
        self._server_shutdown_done = False
        self._server_shutdown = BackgroundCloseStep("dashboard-http-shutdown")
        self._server_closed = False
        self._server_close = BackgroundCloseStep("dashboard-http-server-close")
        self._thread_confirmed = False

    # -- reads ------------------------------------------------------------

    @property
    def bind_address(self) -> str:
        """The address the socket is bound to. Not a knob -- the answer.

        Named for what it is rather than ``host``, because in this codebase
        "host" means :class:`~agent_alfred.runtime.host.RuntimeHost`, and a
        property that returned a process owner would be a worse surprise
        than one that returns an address.
        """
        return DEFAULT_HOST

    @property
    def port(self) -> int:
        """The port actually bound. Meaningless before ``start()``."""
        server = self._server
        if server is None:
            return self._requested_port
        return int(server.server_address[1])

    @property
    def descriptor(self) -> EntryDescriptor | None:
        return self._descriptor

    @property
    def started(self) -> bool:
        return self._server is not None

    @property
    def lock_held(self) -> bool:
        return self._lock is not None and self._lock.acquired

    @property
    def managed_state(self) -> ManagedStateLease | None:
        return self._managed_state

    @property
    def server(self) -> Any:
        return self._server

    # -- lifecycle --------------------------------------------------------

    def start(
        self, *, rollback_step_timeout: float | None = None,
    ) -> EntryDescriptor:
        """Lock, then bind, then describe. Anything less is undone.

        This is the first third of start-up and deliberately nothing more:
        the database, the Host and the stream all belong to later steps that
        may fail for their own reasons, and running any of them before the
        lock is held would let a second instance migrate the database or
        write Run state before being told it may not.

        The undo is the close: the same confirmed order -- socket, descriptor
        fact, lock -- and the same progress bits. A step of the undo that
        itself refuses leaves the rest exactly where a later ``close()``
        resumes it, and the start failure still reaches the caller as the
        exception it was, with the refused undo step as its cause.
        """
        if self._server is not None:
            return self._descriptor or self._write_descriptor()
        # The one thing that has to precede the lock: the lock file lives in
        # the state directory, and a file cannot be locked inside a
        # directory that does not exist. Creating it takes no part in the
        # arbitration -- it is idempotent, and it contains nothing a
        # competing instance could misread. Every *decision* follows the
        # lock.
        rollback = ResumableRollback()

        def close_state() -> bool | None:
            state = self._managed_state
            if state is None:
                return True
            lock = self._lock
            if self._server is not None or (lock is not None and lock.acquired):
                return False
            closed = state.close()
            if closed is not False:
                self._managed_state = None
            return closed

        rollback.own(close_state, close_state)
        try:
            # The caller-owned rollback exists before acquisition.  The raw
            # lease remains in it across acquire's RETURN and our STORE_ATTR;
            # only after ``self`` can close it do we retire that raw step.
            self._managed_state = ManagedStateDirectory.acquire(
                self._state_dir, _rollback=rollback
            )
            rollback.transfer(self._managed_state)
            lock_file = self._managed_state.open_regular(
                PurePath(LOCK_NAME),
                access="read_write",
                create=True,
                role="process lock",
                _rollback=rollback,
            )
            rollback.own(lock_file)
            factory = self._lock_factory or ProcessLock
            self._lock = factory(lock_file)

            def close_lock() -> bool | None:
                if self._server is not None:
                    return False
                self._lock.release()
                return True

            rollback.own(self._lock, close_lock)
            # A successfully constructed lock factory owns the lease.
            rollback.transfer(lock_file)
            self._lock.acquire()
            release_server = partial(self._release_server, rollback_step_timeout)
            rollback.own(release_server, release_server)
            self._bind(rollback, release_server)
            forget_descriptor = self._forget_descriptor
            rollback.own(forget_descriptor, forget_descriptor)
            descriptor = self._write_descriptor()
        except BaseException as exc:
            rollback.raise_failure(exc)
        rollback.transfer(release_server)
        rollback.transfer(forget_descriptor)
        rollback.transfer(self._lock)
        rollback.transfer(close_state)
        return descriptor

    def _bind(
        self, rollback: ResumableRollback, release_server: Callable[[], bool],
    ) -> None:
        factory = self._server_factory
        if factory is None:
            factory = _default_server_factory

        def close_bound(server: Any) -> bool:
            if self._server is server:
                return release_server()
            if (
                getattr(server, "_construction_pending", False)
                and getattr(server, "socket", None) is None
            ):
                return True
            # A factory may publish its socket before failing. Give the
            # resumable service ownership before its bounded close begins.
            self._server = server
            return release_server()

        try:
            server = OwnedResource.acquire(
                rollback,
                partial(
                    _initialize_bound_server,
                    factory,
                    (DEFAULT_HOST, self._requested_port),
                    self._handler,
                ),
                close=close_bound,
            )
        except OSError as exc:
            raise PortUnavailable(
                DEFAULT_HOST, self._requested_port, _bind_error_reason(exc)
            ) from exc
        self._server = server
        rollback.transfer(server)
        # Attached before anything can be accepted, so a handler instance can
        # never find itself without the process-wide context it needs.
        if self._context is not None:
            self._server.context = self._context

    def attach_context(self, context: Any) -> None:
        """Give the bound server the context it could not have had yet.

        The context is built from the Host, and the Host is built after the
        bind -- the bind has to precede any write to the state directory, and
        building a Host means migrating it. So the context arrives here,
        still before the first request can be accepted, because serving does
        not start until the caller says so.
        """
        self._context = context
        if self._server is not None:
            self._server.context = context

    def _write_descriptor(self) -> EntryDescriptor:
        descriptor = EntryDescriptor(
            instance_id=self._instance_id,
            pid=self._pid,
            port=self.port,
        )
        if self._managed_descriptor:
            assert self._managed_state is not None
            self._managed_state.replace_bytes(
                PurePath(DESCRIPTOR_NAME),
                descriptor.to_json().encode("utf-8"),
                published=self._claim_descriptor,
            )
        else:
            self._claim_descriptor()
            self._write_descriptor_fn(self._state_dir, descriptor)
        self._descriptor = descriptor
        return descriptor

    def _claim_descriptor(self) -> None:
        """Publish the cleanup owner while the atomic writer still owns rollback."""
        self._descriptor_owned = True

    def start_serving(self, spawn: SpawnThread | None = None) -> Any:
        """Handle requests on a daemon thread; return it.

        The one serving seam this lifecycle has: the bound server's own
        ``serve_forever`` is the thread target, and the lifecycle object
        holds both the server and the thread so the close can confirm the
        thread's exit from outside it.

        Daemon because a Dashboard thread must never keep the interpreter
        alive past the process the user asked to exit -- there is no reply
        in flight that outlives the Run it belongs to.

        ``spawn`` is the seam for the two ways this step can fail: a thread
        that cannot be created, and one that cannot be started. Both are
        failures of the last start-up step like any other, so the caller has
        to be able to reach them without crashing the interpreter.

        The Thread identity is retained *before* ``start()``. If an asynchronous
        exit lands after the start effect but before ``_serving`` is published,
        close can still distinguish the started Thread from a before-effect
        refusal and must stop/join it before releasing the entry.
        """
        if self._server is None:
            raise RuntimeError("DashboardService.start() must precede serving")
        make = spawn if spawn is not None else _new_thread
        thread = make(self._server.serve_forever)
        self._serving_thread = thread
        thread.start()
        self._serving = True
        return thread

    def stop_serving(self, timeout: float | None = None) -> bool:
        """Close the listening socket and stop accepting. Idempotent.

        ``False`` means one of shutdown, socket close or serving-thread
        confirmation is still in flight. Each unfinished sub-step gets its
        own ``timeout`` bound, and a later call resumes that same step without
        restarting an action already in flight or one already confirmed.

        Separate from :meth:`close` because the rest of the process has to
        be wound down *before* the descriptor is deleted and the lock
        dropped: a second instance allowed to start while this one's worker
        is still finishing a Run would be two processes with one state
        directory, which is the exact thing the lock exists to prevent.
        """
        return self._release_server(timeout)

    def close(self) -> None:
        """Undo the lifecycle from the outside in. Idempotent.

        The descriptor goes before the lock: as long as it exists it points
        at a port this process is about to stop answering.
        """
        self._release_server()
        self._forget_descriptor()
        if self._lock is not None:
            self._lock.release()
        if self._managed_state is not None:
            self._managed_state.close()
            self._managed_state = None

    # -- internals --------------------------------------------------------

    def _release_server(self, timeout: float | None = None) -> bool:
        """Stop the serving loop and close the listening socket. Resumable.

        The server reference survives every failed attempt: ``shutdown()``
        and ``server_close()`` are retryable steps, and dropping the
        reference before they succeed would make the next close skip them
        entirely -- handing the descriptor and the lock over on top of a
        socket that was never confirmed closed. Each progress bit advances
        only after the action behind it has returned, so a close that
        resumes never re-runs a step that succeeded and never skips one
        that did not.

        The steps run in order and from outside the serving thread:
        ``shutdown()`` asks the loop to stop and returns only once its loop
        has observed the request, ``server_close()`` releases the listening
        socket, and joining the thread is the confirmation that the last
        serving loop is really gone -- "the port is no longer served" is a
        fact about a thread, not about an intention.

        The stdlib requires ``shutdown()`` to run outside the serving thread.
        Shutdown and socket close therefore use retained daemon workers; the
        owner waits on their completion events with the current sub-step's
        remaining deadline and always joins them before advancing. A blocked
        system close stays owned for a later retry without holding process
        exit open forever.
        """
        server = self._server
        if server is None:
            return True
        thread = self._serving_thread
        if thread is not None and not self._serving and not self._thread_confirmed:
            # Resolve the native-handle fact, not ``_started``/``join(0)``:
            # CPython creates the former before the child publishes the latter.
            if thread_start_effect_happened(thread):
                self._serving = True
            else:
                self._thread_confirmed = True
                self._serving_thread = None
        if self._serving and not self._server_shutdown_done:
            # Only ever called once a serving loop was asked for.
            # ``shutdown()`` waits for that loop to observe the request, so
            # calling it on a server that never served would wait forever --
            # which is how a failed start would turn into a hung process.
            if not self._server_shutdown.complete(server.shutdown, timeout):
                return False
            self._server_shutdown_done = True
        if not self._server_closed:
            if not self._server_close.complete(server.server_close, timeout):
                return False
            self._server_closed = True
        if not self._thread_confirmed:
            # The thread only exists if ``start()`` returned, so joining it
            # here is always legal; and since ``shutdown()`` returns only
            # after the serving loop exited, this join is the thread's last
            # step, not a wait for work to finish.
            thread = self._serving_thread
            if thread is not None:
                deadline = (
                    None
                    if timeout is None
                    else time.monotonic() + max(0.0, timeout)
                )
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if not thread_exit_confirmed(thread, remaining):
                    return False
            self._thread_confirmed = True
        # Reached only when the socket is confirmed closed and the thread is
        # confirmed gone; this is what makes "the port is free" a fact
        # rather than an intention.
        self._server = None
        self._serving = False
        self._serving_thread = None
        return True

    def _forget_descriptor(self) -> None:
        if not self._descriptor_owned:
            return
        if self._managed_state is not None:
            self._managed_state.unlink_regular(
                PurePath(DESCRIPTOR_NAME), missing_ok=True
            )
        else:
            path = self._state_dir / DESCRIPTOR_NAME
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        # Only a file that is really gone is forgotten: a permission or
        # filesystem error keeps the reference, so the next close() retries
        # the deletion instead of releasing the lock on top of a descriptor
        # that still names this process.
        self._descriptor = None
        self._descriptor_owned = False

    def __enter__(self) -> "DashboardService":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _new_thread(target: Callable[[], None]) -> Any:
    """An **unstarted** serving thread: the caller starts it.

    Daemon and named here rather than at the call site: a Dashboard thread
    has one shape, and the name is what shows up in a stack dump.
    """
    return threading.Thread(target=target, name="dashboard-http", daemon=True)


def _initialize_bound_server(
    factory: Any,
    address: tuple[str, int],
    handler: Any,
    owner: OwnedResource[Any],
) -> None:
    """Initialize a class or owner-aware function behind a published token."""
    if isinstance(factory, type):
        server = factory.__new__(factory)
        server._construction_pending = True
        owner.publish(server)
        factory.__init__(server, address, handler)
        server._construction_pending = False
        return
    factory(address, handler, owner)


def _default_server_factory(
    address: tuple[str, int], handler: Any, owner: OwnedResource[Any]
) -> None:
    """``ThreadingHTTPServer`` with the flags this lifecycle depends on."""
    from http.server import ThreadingHTTPServer

    class _Server(ThreadingHTTPServer):
        # One thread per connection, and every one of them a daemon. SSE
        # holds a handler open for as long as the stream lives, so a
        # single-threaded server would serve exactly one browser tab and a
        # non-daemon pool would keep the interpreter alive for a browser
        # that is already gone.
        daemon_threads = True
        # Do not wait on handler threads when closing: an SSE handler is
        # blocked in serve_forever's own read loop by design, and close()
        # must be able to finish without it. The socket is closed either
        # way, and a daemon thread cannot outlive the process.
        block_on_close = False
        # The stdlib default. It does not weaken the port check: SO_REUSEADDR
        # lets this process rebind its own address out of TIME_WAIT after a
        # restart, while a socket another process is LISTENing on still
        # fails with EADDRINUSE -- which is exactly the failure that has to
        # reach the caller unchanged.

    server = _Server.__new__(_Server)
    server._construction_pending = True
    owner.publish(server)
    _Server.__init__(server, address, handler)
    server._construction_pending = False
