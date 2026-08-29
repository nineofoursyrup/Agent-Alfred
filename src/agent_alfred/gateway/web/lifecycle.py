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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DashboardService",
    "EntryDescriptor",
    "PortUnavailable",
    "ProcessLock",
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

    def __init__(self, path: Path):
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(
            self._path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise StateDirLocked(self._path, _recorded_pid(self._path)) from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, b"pid=%d\n" % os.getpid())
            os.fsync(fd)
        except OSError:
            # The lock is held either way; the pid is only a note. Undo the
            # whole acquisition rather than report success on a half-done
            # one -- closing the descriptor drops the flock with it.
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _recorded_pid(path: Path) -> int | None:
    """The pid the lock file names, if it names one.

    Diagnostic only. A lock file with no readable pid still means exactly one
    thing -- somebody else holds it -- so a failed read reports "unknown"
    instead of guessing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("pid="):
            digits = line[len("pid=") :].strip()
            if digits.isdigit():
                return int(digits)
    return None


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
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(descriptor.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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
        return EntryDescriptor(
            instance_id=payload["instance_id"],
            pid=int(payload["pid"]),
            port=int(payload["port"]),
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
        lock: "ProcessLock | None" = None,
        write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
    ):
        # There is deliberately no address parameter. Refusing a value that
        # was never accepted is stronger than validating one that was: no
        # caller -- and no injected server factory, the seam every test uses
        # to avoid a real socket -- can carry another address to the bind.
        if port < 1 or port > 65535:
            raise ValueError(f"port must be in 1..65535, got {port}")
        self._state_dir = state_dir
        self._handler = handler
        self._instance_id = instance_id
        self._requested_port = port
        self._server_factory = server_factory
        self._context = context
        self._pid = os.getpid() if pid is None else pid
        self._lock = lock if lock is not None else ProcessLock(
            state_dir / LOCK_NAME
        )
        self._write_descriptor_fn = write_descriptor or write_entry_descriptor
        self._server: Any = None
        self._serving = False
        self._descriptor: EntryDescriptor | None = None

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
        return self._lock.acquired

    @property
    def server(self) -> Any:
        return self._server

    # -- lifecycle --------------------------------------------------------

    def start(self) -> EntryDescriptor:
        """Lock, then bind, then describe. Anything less is fully undone.

        This is the first third of start-up and deliberately nothing more:
        the database, the Host and the stream all belong to later steps that
        may fail for their own reasons, and running any of them before the
        lock is held would let a second instance migrate the database or
        write Run state before being told it may not.
        """
        if self._server is not None:
            return self._descriptor or self._write_descriptor()
        # The one thing that has to precede the lock: the lock file lives in
        # the state directory, and a file cannot be locked inside a
        # directory that does not exist. Creating it takes no part in the
        # arbitration -- it is idempotent, it is 0700, and it contains
        # nothing a competing instance could misread. Every *decision*
        # follows the lock.
        self._state_dir.mkdir(mode=0o700, exist_ok=True)
        self._lock.acquire()
        try:
            self._bind()
        except BaseException:
            self._lock.release()
            raise
        try:
            return self._write_descriptor()
        except BaseException:
            self._release_server()
            self._lock.release()
            raise

    def _bind(self) -> None:
        factory = self._server_factory
        if factory is None:
            factory = _default_server_factory
        try:
            self._server = factory(
                (DEFAULT_HOST, self._requested_port), self._handler
            )
        except OSError as exc:
            raise PortUnavailable(
                DEFAULT_HOST, self._requested_port, _bind_error_reason(exc)
            ) from exc
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
        self._write_descriptor_fn(self._state_dir, descriptor)
        self._descriptor = descriptor
        return descriptor

    def serve_forever(self) -> None:
        """Block handling requests. The caller owns the thread."""
        if self._server is None:
            raise RuntimeError("DashboardService.start() must precede serving")
        self._serving = True
        self._server.serve_forever()

    def start_serving(self, spawn: Any = None) -> Any:
        """Handle requests on a daemon thread; return it.

        Daemon because a Dashboard thread must never keep the interpreter
        alive past the process the user asked to exit -- there is no reply
        in flight that outlives the Run it belongs to.

        ``spawn`` is the seam for the two ways this step can fail: a thread
        that cannot be created, and one that cannot be started. Both are
        failures of the last start-up step like any other, so the caller has
        to be able to reach them without crashing the interpreter.

        ``_serving`` is set only once the thread is actually running. It is
        what makes :meth:`stop_serving` call ``shutdown()``, and calling
        ``shutdown()`` on a server whose loop never started waits forever --
        which would turn a failed start into a hung process.
        """
        if self._server is None:
            raise RuntimeError("DashboardService.start() must precede serving")
        factory = spawn if spawn is not None else _default_spawn
        thread = factory(
            target=self._server.serve_forever, name="dashboard-http", daemon=True
        )
        thread.start()
        self._serving = True
        return thread

    def stop_serving(self) -> None:
        """Close the listening socket and stop accepting. Idempotent.

        Separate from :meth:`close` because the rest of the process has to
        be wound down *before* the descriptor is deleted and the lock
        dropped: a second instance allowed to start while this one's worker
        is still finishing a Run would be two processes with one state
        directory, which is the exact thing the lock exists to prevent.
        """
        self._release_server()

    def close(self) -> None:
        """Undo the lifecycle from the outside in. Idempotent.

        The descriptor goes before the lock: as long as it exists it points
        at a port this process is about to stop answering.
        """
        self._release_server()
        self._forget_descriptor()
        self._lock.release()

    # -- internals --------------------------------------------------------

    def _release_server(self) -> None:
        server, self._server = self._server, None
        serving, self._serving = self._serving, False
        if server is None:
            return
        if serving:
            # Only ever called once a serving loop was asked for.
            # ``shutdown()`` waits for that loop to observe the request, so
            # calling it on a server that never served would wait forever --
            # which is how a failed start would turn into a hung process.
            server.shutdown()
        server.server_close()

    def _forget_descriptor(self) -> None:
        if self._descriptor is None:
            return
        self._descriptor = None
        try:
            (self._state_dir / DESCRIPTOR_NAME).unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "DashboardService":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _default_spawn(**kwargs: Any) -> Any:
    """The only thing that makes a serving thread, when nothing is injected."""
    return threading.Thread(**kwargs)


def _default_server_factory(address: tuple[str, int], handler: Any) -> Any:
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

    return _Server(address, handler)
