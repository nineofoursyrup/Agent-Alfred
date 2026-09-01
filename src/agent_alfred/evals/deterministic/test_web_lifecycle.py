"""The Dashboard lifecycle: lock -> bind -> describe, and undone backwards.

Every test here is about order and about undoing. The three steps are one
indivisible whole (#23 §2), so the interesting cases are the failures: a step
that fails must leave nothing behind that could be mistaken for a running
Dashboard -- no held lock, no open descriptor, no descriptor naming a port
nobody is listening on.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DESCRIPTOR_NAME,
    LOCK_NAME,
    DashboardService,
    EntryDescriptor,
    PortUnavailable,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pragma: no cover - never reached here
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr log; it is not part of any assertion."""


class _RecordingServer:
    """Stands in for the HTTP server so failures can be injected."""

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


def _service(tmp_path, **kwargs):
    port = kwargs.pop("port", None)
    return DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-lifecycle",
        port=DEFAULT_PORT if port is None else port,
        server_factory=_RecordingServer,
        **kwargs,
    )


# --- the lock -------------------------------------------------------------


def test_a_lock_is_exclusive_within_one_process(tmp_path) -> None:
    first = ProcessLock(tmp_path / LOCK_NAME)
    second = ProcessLock(tmp_path / LOCK_NAME)
    first.acquire()
    with pytest.raises(StateDirLocked) as caught:
        second.acquire()
    assert caught.value.path == tmp_path / LOCK_NAME
    assert first.acquired is True
    first.release()
    # Released means released: the next acquisition is not a conflict.
    second.acquire()
    assert second.acquired is True
    second.release()


def test_a_lock_file_left_by_a_dead_holder_does_not_block(tmp_path) -> None:
    """The lock is advisory, not a marker file.

    A file that exists without a holder is the normal state after a clean
    exit; treating it as taken would make the second start of the Dashboard
    impossible forever.
    """
    path = tmp_path / LOCK_NAME
    path.write_text("pid=999999\n", encoding="utf-8")
    lock = ProcessLock(path)
    lock.acquire()
    assert lock.acquired is True
    lock.release()


def test_a_conflict_names_the_holder_when_the_file_says_who(tmp_path) -> None:
    holder = ProcessLock(tmp_path / LOCK_NAME)
    holder.acquire()
    try:
        with pytest.raises(StateDirLocked) as caught:
            ProcessLock(tmp_path / LOCK_NAME).acquire()
    finally:
        holder.release()
    assert caught.value.holder_pid is not None


@pytest.mark.parametrize("conflict_errno", (errno.EACCES, errno.EAGAIN))
def test_only_lock_contention_is_reported_as_a_state_directory_conflict(
    tmp_path, monkeypatch, conflict_errno: int
) -> None:
    path = tmp_path / LOCK_NAME
    path.write_text("pid=123\n", encoding="utf-8")
    flock_error = OSError(conflict_errno, os.strerror(conflict_errno))

    def refuse_lock(fd: int, operation: int) -> None:
        raise flock_error

    monkeypatch.setattr(fcntl, "flock", refuse_lock)

    with pytest.raises(StateDirLocked) as caught:
        ProcessLock(path).acquire()

    assert caught.value.path == path
    assert caught.value.holder_pid == 123
    assert caught.value.__cause__ is flock_error


@pytest.mark.parametrize("failure_errno", (errno.EIO, errno.EINTR, errno.ENOLCK))
def test_a_non_contention_flock_failure_is_preserved_without_reading_a_holder(
    tmp_path, monkeypatch, failure_errno: int
) -> None:
    path = tmp_path / LOCK_NAME
    flock_error = OSError(failure_errno, os.strerror(failure_errno))
    holder_was_read = False

    def fail_lock(fd: int, operation: int) -> None:
        raise flock_error

    def record_unexpected_holder_read(*args, **kwargs):
        nonlocal holder_was_read
        holder_was_read = True
        raise AssertionError("a non-contention failure has no lock holder")

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    monkeypatch.setattr(Path, "read_text", record_unexpected_holder_read)

    with pytest.raises(OSError) as caught:
        ProcessLock(path).acquire()

    assert caught.value is flock_error
    assert caught.value.errno == failure_errno
    assert holder_was_read is False


@pytest.mark.parametrize(
    "recorded_pid",
    (
        "",
        "9" * 5_000,
        "²",
        "١٢٣",
        "+123",
        "01",
        "0",
        "2147483648",
    ),
    ids=(
        "empty",
        "five-thousand-digits",
        "unicode-digit",
        "unicode-decimal",
        "signed",
        "leading-zero",
        "zero",
        "past-portable-pid-max",
    ),
)
def test_a_malformed_recorded_pid_cannot_mask_a_lock_conflict(
    tmp_path, recorded_pid: str
) -> None:
    path = tmp_path / LOCK_NAME
    holder = ProcessLock(path)
    holder.acquire()
    path.write_text(f"pid={recorded_pid}\n", encoding="utf-8")
    try:
        with pytest.raises(StateDirLocked) as caught:
            ProcessLock(path).acquire()
    finally:
        holder.release()
    assert caught.value.path == path
    assert caught.value.holder_pid is None


def test_a_canonical_recorded_pid_is_kept_as_lock_diagnostic(tmp_path) -> None:
    path = tmp_path / LOCK_NAME
    holder = ProcessLock(path)
    holder.acquire()
    path.write_text("pid=123\n", encoding="utf-8")
    try:
        with pytest.raises(StateDirLocked) as caught:
            ProcessLock(path).acquire()
    finally:
        holder.release()
    assert caught.value.holder_pid == 123


def test_an_unreadable_recorded_pid_cannot_mask_a_lock_conflict(tmp_path) -> None:
    path = tmp_path / LOCK_NAME
    holder = ProcessLock(path)
    holder.acquire()
    path.write_bytes(b"pid=\xff\n")
    try:
        with pytest.raises(StateDirLocked) as caught:
            ProcessLock(path).acquire()
    finally:
        holder.release()
    assert caught.value.holder_pid is None


def test_an_unexpected_lock_diagnostic_error_is_not_silenced(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / LOCK_NAME
    holder = ProcessLock(path)
    holder.acquire()

    def explode(*args, **kwargs):
        raise RuntimeError("diagnostic bug")

    monkeypatch.setattr(Path, "read_text", explode)
    try:
        with pytest.raises(RuntimeError, match="diagnostic bug"):
            ProcessLock(path).acquire()
    finally:
        holder.release()


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    (
        (None, sys.int_info.default_max_str_digits),
        (0, 0),
        (10_000, 10_000),
    ),
)
def test_a_long_recorded_pid_is_unknown_under_every_integer_digit_limit(
    configured_limit: int | None, expected_limit: int
) -> None:
    environment = os.environ.copy()
    if configured_limit is None:
        environment.pop("PYTHONINTMAXSTRDIGITS", None)
    else:
        environment["PYTHONINTMAXSTRDIGITS"] = str(configured_limit)
    script = """
import fcntl
import os
import sys
import tempfile
from pathlib import Path

from agent_alfred.gateway.web.lifecycle import ProcessLock, StateDirLocked

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "dashboard.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path.write_text("pid=" + "9" * 5_000 + "\\n", encoding="utf-8")
    try:
        ProcessLock(path).acquire()
    except StateDirLocked as exc:
        print(sys.get_int_max_str_digits(), exc.holder_pid)
    finally:
        os.close(fd)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.stdout.strip() == f"{expected_limit} None"
    assert completed.stderr == ""


def test_acquire_is_idempotent_and_release_is_too(tmp_path) -> None:
    lock = ProcessLock(tmp_path / LOCK_NAME)
    lock.acquire()
    lock.acquire()
    assert lock.acquired is True
    lock.release()
    lock.release()
    assert lock.acquired is False


def test_the_lock_file_records_the_owning_pid(tmp_path) -> None:
    lock = ProcessLock(tmp_path / LOCK_NAME)
    with lock:
        assert f"pid={os.getpid()}" in (tmp_path / LOCK_NAME).read_text()


# --- order and rollback ---------------------------------------------------


def test_start_locks_then_binds_then_describes(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    descriptor = service.start()
    assert service.lock_held is True
    assert service.started is True
    assert descriptor.port == service.port
    on_disk = read_entry_descriptor(tmp_path)
    assert on_disk == descriptor
    service.close()


def test_the_descriptor_names_instance_pid_and_port(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port(), pid=4242)
    descriptor = service.start()
    assert descriptor.instance_id == "inst-lifecycle"
    assert descriptor.pid == 4242
    assert descriptor.port == service.port
    payload = json.loads((tmp_path / DESCRIPTOR_NAME).read_text())
    assert payload == {
        "instance_id": "inst-lifecycle",
        "pid": 4242,
        "port": service.port,
    }
    service.close()


def test_a_busy_port_fails_and_leaves_nothing_behind(tmp_path) -> None:
    """A real bind against a real squatter: only a socket can prove the
    port conflict reaches the caller instead of being worked around."""
    port = free_loopback_port()
    squatter = socket.socket()
    squatter.bind((DEFAULT_HOST, port))
    squatter.listen(1)
    service = DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-lifecycle",
        port=port,
    )
    try:
        with pytest.raises(PortUnavailable) as caught:
            service.start()
    finally:
        squatter.close()
    # The port is named, not swapped for another one.
    assert caught.value.port == port
    assert caught.value.host == DEFAULT_HOST
    # Rollback: no lock, no descriptor, nothing bound.
    assert service.lock_held is False
    assert service.started is False
    assert read_entry_descriptor(tmp_path) is None
    # ... and the lock is genuinely free again, not merely flagged as such.
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_a_descriptor_failure_also_rolls_the_bind_and_the_lock_back(
    tmp_path, monkeypatch
) -> None:
    def explode(directory: Path, descriptor: EntryDescriptor) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(
        "agent_alfred.gateway.web.lifecycle.write_entry_descriptor", explode
    )
    service = _service(tmp_path, port=free_loopback_port())
    with pytest.raises(OSError):
        service.start()
    assert service.started is False
    assert service.lock_held is False
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_start_is_idempotent_and_returns_the_same_entry(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    first = service.start()
    second = service.start()
    assert first == second
    assert service.server is not None
    service.close()


def test_close_undoes_everything_and_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    server = service.server
    thread = service.start_serving()
    service.close()
    # The serving loop is stopped and the listening socket is closed.
    assert server.shut_down is True
    assert server.closed is True
    # The serving thread's exit is confirmed, not assumed.
    assert not thread.is_alive()
    assert service.lock_held is False
    assert service.descriptor is None
    assert read_entry_descriptor(tmp_path) is None
    # A Dashboard that closed must not keep the port from the next start.
    service.close()


def test_close_without_serving_does_not_wait_on_a_loop(tmp_path) -> None:
    """shutdown() waits for the serving loop to notice the request.

    A start that never served has no loop, so asking it to shut down would
    block until one appeared -- turning a failed start into a hung process.
    """
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    server = service.server
    service.close()
    assert server.shut_down is False
    assert server.closed is True
    assert service.lock_held is False


def test_close_releases_the_lock_for_a_real_second_instance(tmp_path) -> None:
    first = _service(tmp_path, port=free_loopback_port())
    first.start()
    first.close()
    second = _service(tmp_path, port=free_loopback_port())
    try:
        second.start()
        assert second.lock_held is True
    finally:
        second.close()


def test_a_stale_descriptor_is_replaced_not_merged(tmp_path) -> None:
    (tmp_path / DESCRIPTOR_NAME).write_text(
        json.dumps({"instance_id": "old", "pid": 7, "port": 9}),
        encoding="utf-8",
    )
    service = _service(tmp_path, port=free_loopback_port())
    try:
        descriptor = service.start()
        assert read_entry_descriptor(tmp_path) == descriptor
        assert descriptor.instance_id == "inst-lifecycle"
    finally:
        service.close()


def test_an_unreadable_descriptor_is_an_error_not_a_missing_one(tmp_path) -> None:
    (tmp_path / DESCRIPTOR_NAME).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        read_entry_descriptor(tmp_path)


@pytest.mark.parametrize("payload", ([], None, 7, "descriptor"))
def test_an_entry_descriptor_must_be_a_json_object(tmp_path, payload) -> None:
    path = tmp_path / DESCRIPTOR_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ValueError, match="dashboard.json is not a readable entry descriptor"
    ):
        read_entry_descriptor(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("pid", True),
        ("pid", "123"),
        ("pid", 12.5),
        ("pid", None),
        ("pid", -1),
        ("pid", 0),
        ("pid", 2_147_483_648),
        ("port", True),
        ("port", "7717"),
        ("port", 7717.0),
        ("port", None),
        ("port", -1),
        ("port", 0),
        ("port", 65_536),
    ),
)
def test_an_entry_descriptor_refuses_non_exact_or_out_of_range_integers(
    tmp_path, field: str, value: object
) -> None:
    payload = {"instance_id": "inst", "pid": 123, "port": 7717}
    payload[field] = value
    path = tmp_path / DESCRIPTOR_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ValueError, match="dashboard.json is not a readable entry descriptor"
    ):
        read_entry_descriptor(tmp_path)


@pytest.mark.parametrize("instance_id", (None, True, 7, "", "bad:instance"))
def test_an_entry_descriptor_requires_a_shaped_string_instance_id(
    tmp_path, instance_id: object
) -> None:
    path = tmp_path / DESCRIPTOR_NAME
    path.write_text(
        json.dumps({"instance_id": instance_id, "pid": 123, "port": 7717}),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="dashboard.json is not a readable entry descriptor"
    ):
        read_entry_descriptor(tmp_path)


@pytest.mark.parametrize(
    "descriptor",
    (
        EntryDescriptor("minimums", 1, 1),
        EntryDescriptor("maximums", 2_147_483_647, 65_535),
    ),
)
def test_entry_descriptor_integer_boundaries_round_trip(
    tmp_path, descriptor: EntryDescriptor
) -> None:
    write_entry_descriptor(tmp_path, descriptor)
    assert read_entry_descriptor(tmp_path) == descriptor


def test_a_missing_descriptor_reads_as_none(tmp_path) -> None:
    assert read_entry_descriptor(tmp_path) is None


# --- the descriptor write itself ------------------------------------------


def test_the_descriptor_is_written_atomically_and_leaves_no_debris(
    tmp_path,
) -> None:
    write_entry_descriptor(tmp_path, EntryDescriptor("i", 1, 2))
    write_entry_descriptor(tmp_path, EntryDescriptor("j", 3, 4))
    assert read_entry_descriptor(tmp_path) == EntryDescriptor("j", 3, 4)
    debris = [path.name for path in tmp_path.iterdir() if path.name != DESCRIPTOR_NAME]
    assert debris == []


def test_a_failed_descriptor_write_leaves_the_previous_one_intact(
    tmp_path, monkeypatch
) -> None:
    write_entry_descriptor(tmp_path, EntryDescriptor("kept", 1, 2))
    monkeypatch.setattr(
        "agent_alfred.gateway.web.lifecycle.os.fsync",
        lambda fd: (_ for _ in ()).throw(OSError("cannot sync")),
    )
    with pytest.raises(OSError):
        write_entry_descriptor(tmp_path, EntryDescriptor("lost", 3, 4))
    assert read_entry_descriptor(tmp_path) == EntryDescriptor("kept", 1, 2)


def test_the_state_directory_files_are_private(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    try:
        service.start()
        for name in (LOCK_NAME, DESCRIPTOR_NAME):
            mode = stat.S_IMODE(os.stat(tmp_path / name).st_mode)
            assert mode == 0o600, f"{name} is {oct(mode)}"
    finally:
        service.close()


# --- the state directory's own mode ----------------------------------------


def test_start_forces_0700_on_a_pre_existing_state_directory(tmp_path) -> None:
    """mkdir(mode=0o700, exist_ok=True) never fixes an existing directory.

    The state directory is a managed path: whoever created it -- an older
    instance, a restore, a careless ``mkdir`` -- left a mode this process
    must not trust. Confirming the directory therefore ends with an explicit
    0700, and nothing user-owned inside it is touched.
    """
    user_file = tmp_path / "keepme"
    user_file.write_text("user data", encoding="utf-8")
    os.chmod(tmp_path, 0o755)
    os.chmod(user_file, 0o644)
    service = _service(tmp_path, port=free_loopback_port())
    try:
        service.start()
        assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
        # Only the managed directory itself: no recursion into user files.
        assert stat.S_IMODE(os.stat(user_file).st_mode) == 0o644
    finally:
        service.close()


def test_start_tightens_the_directory_even_under_a_hostile_umask(tmp_path) -> None:
    """A mode granted through mkdir is a mode the umask may take away.

    With the umask withholding every bit, a freshly created state directory
    comes out 0000 -- and the explicit enforcement, which runs before the
    process lock, is the only thing between it and a directory this process
    cannot even write a lock file into.
    """
    state = tmp_path / "state"
    previous = os.umask(0o777)
    try:
        state.mkdir()
    finally:
        os.umask(previous)
    # Precondition: the umask really did strip the mode mkdir asked for.
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o000
    service = _service(state, port=free_loopback_port())
    try:
        service.start()
        assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
    finally:
        service.close()


def test_a_bind_failure_still_tightens_a_pre_existing_directory(tmp_path) -> None:
    """The mode is enforced before the lock, so even a refused bind leaves
    the directory tighter than it found it -- and nothing else behind."""
    os.chmod(tmp_path, 0o755)
    port = free_loopback_port()
    squatter = socket.socket()
    squatter.bind((DEFAULT_HOST, port))
    squatter.listen(1)
    service = DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-lifecycle",
        port=port,
    )
    try:
        with pytest.raises(PortUnavailable):
            service.start()
    finally:
        squatter.close()
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    # No lock, no socket, no descriptor.
    assert service.lock_held is False
    assert service.started is False
    assert read_entry_descriptor(tmp_path) is None


def test_a_chmod_failure_aborts_before_lock_or_bind(tmp_path, monkeypatch) -> None:
    """Enforcement that cannot happen is a start that does not happen.

    Refusing to run with a state directory whose mode could not be forced is
    the honest answer; binding the port and writing an entry descriptor for
    it would be claiming an entry this process has not secured.
    """

    def refuse(path, mode):
        raise OSError("cannot chmod")

    monkeypatch.setattr(
        "agent_alfred.gateway.web.lifecycle.os.chmod", refuse
    )
    service = _service(tmp_path, port=free_loopback_port())
    with pytest.raises(OSError, match="cannot chmod"):
        service.start()
    assert service.lock_held is False
    assert service.started is False
    assert read_entry_descriptor(tmp_path) is None


# --- serving --------------------------------------------------------------


def test_serving_without_start_is_refused(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    with pytest.raises(RuntimeError):
        service.start_serving()


def test_the_serving_thread_is_a_daemon(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    try:
        thread = service.start_serving()
        # A Dashboard thread must never hold the interpreter open for a
        # browser that is already gone.
        assert thread.daemon is True
        service.server.shutdown()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    finally:
        service.close()


class _ServingThreadStandIn:
    """A thread stand-in that records whether its exit was confirmed."""

    def __init__(self) -> None:
        self.joins = 0
        self.daemon = True

    def start(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        self.joins += 1

    def is_alive(self) -> bool:
        return self.joins == 0


def test_close_confirms_the_serving_thread_has_exited(tmp_path) -> None:
    """shutdown, server_close, thread exit -- in that order, from outside.

    A close that never asks the serving thread whether it finished leaves
    "is this port still served?" a guess; confirming the exit is a step of
    the close like any other, taken from outside the serving thread.
    """
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    thread = _ServingThreadStandIn()
    service.start_serving(spawn=lambda target: thread)
    service.close()
    assert thread.joins == 1
    assert thread.is_alive() is False


def test_a_negative_port_is_refused_before_anything_else(tmp_path) -> None:
    with pytest.raises(ValueError):
        _service(tmp_path, port=0)


# --- a release that fails keeps what it still needs -------------------------


class _RefusingServer(_RecordingServer):
    """A server whose shutdown()/server_close() refuse the first time.

    The counting matters: a resumed close must retry exactly the step that
    refused, never one that already succeeded.
    """

    def __init__(self, address, handler, *, fail_shutdown=False, fail_close=False):
        super().__init__(address, handler)
        self.shutdown_calls = 0
        self.close_calls = 0
        self._fail_shutdown = fail_shutdown
        self._fail_close = fail_close

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._fail_shutdown and self.shutdown_calls == 1:
            raise RuntimeError("shutdown refused")
        self.shut_down = True

    def server_close(self) -> None:
        self.close_calls += 1
        if self._fail_close and self.close_calls == 1:
            raise RuntimeError("socket busy")
        self.closed = True


def _refusing_service(tmp_path, **failures) -> DashboardService:
    def factory(address, handler):
        return _RefusingServer(address, handler, **failures)

    return DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-lifecycle",
        port=free_loopback_port(),
        server_factory=factory,
    )


def test_a_failed_server_close_keeps_the_server_until_the_socket_is_closed(
    tmp_path,
) -> None:
    """server_close() raising is a step that did not finish, not one that ran.

    The reference to the server must survive the failed attempt, so the
    next close resumes at the socket -- and the descriptor and the lock,
    which may only follow a *confirmed* close, stay exactly where they are.
    Dropping the reference first would make the next close skip the socket
    and hand the state directory over while it is still open.
    """
    service = _refusing_service(tmp_path, fail_close=True)
    service.start()
    server = service.server
    service.start_serving()
    with pytest.raises(RuntimeError, match="socket busy"):
        service.close()
    # Nothing has been given up: the socket is not confirmed closed.
    assert service.server is server
    assert service.lock_held is True
    assert read_entry_descriptor(tmp_path) is not None
    # The resume finishes the socket -- without asking shutdown() again --
    # and only then runs the tail.
    service.close()
    assert server.shutdown_calls == 1, "shutdown already succeeded; not re-asked"
    assert server.close_calls == 2
    assert server.closed is True
    assert service.lock_held is False
    assert read_entry_descriptor(tmp_path) is None


def test_a_failed_shutdown_keeps_the_server_and_retries(tmp_path) -> None:
    """shutdown() raising leaves the loop stoppable by the next close.

    The second close must ask shutdown() again -- it is the only step that
    stops the serving loop -- and only then close the socket, delete the
    descriptor and release the lock. Skipping it on a resume would leave a
    serving loop running behind a closed-looking Dashboard.
    """
    service = _refusing_service(tmp_path, fail_shutdown=True)
    service.start()
    server = service.server
    service.start_serving()
    with pytest.raises(RuntimeError, match="shutdown refused"):
        service.close()
    assert service.server is server
    assert service.lock_held is True
    assert read_entry_descriptor(tmp_path) is not None
    service.close()
    assert server.shutdown_calls == 2
    assert server.close_calls == 1
    assert server.closed is True
    assert service.lock_held is False
    assert read_entry_descriptor(tmp_path) is None


# --- the real socket ------------------------------------------------------


def test_the_real_server_binds_only_the_loopback_address(tmp_path) -> None:
    """One real bind, on a random loopback port.

    The fake server above proves the ordering; only a real socket proves
    that the address is the one the threat model assumes and that nothing
    picked a different port behind our back.
    """
    port = free_loopback_port()
    service = DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-real",
        port=port,
    )
    try:
        descriptor = service.start()
        assert service.server.server_address[0] == DEFAULT_HOST
        assert service.server.server_address[1] == port
        assert descriptor.port == port
        # Threading and daemon flags are what keep one SSE stream from
        # becoming the only stream.
        assert service.server.daemon_threads is True
        assert service.server.block_on_close is False
    finally:
        service.close()


def test_a_real_close_releases_the_socket_and_the_port(tmp_path) -> None:
    port = free_loopback_port()
    service = DashboardService(
        state_dir=tmp_path,
        handler=_Handler,
        instance_id="inst-real",
        port=port,
    )
    service.start()
    service.close()
    # The descriptor went before the lock; the listening socket went too, so
    # the next instance can bind the same port immediately.
    probe = socket.socket()
    try:
        probe.bind((DEFAULT_HOST, port))
    finally:
        probe.close()
    assert read_entry_descriptor(tmp_path) is None


def test_the_default_port_is_the_decided_one() -> None:
    assert DEFAULT_PORT == 7717
    assert DEFAULT_HOST == "127.0.0.1"


def test_a_descriptor_that_refuses_deletion_keeps_the_reference(
    tmp_path, monkeypatch
) -> None:
    """A failed unlink forgets nothing.

    The descriptor names a port this process is about to stop answering, so
    "forget" may only ever forget a file that is really gone. An unlink that
    loses to a permission error must leave the reference, the file and the
    lock exactly where they were, so the next close() deletes the file for
    real and only then drops the lock. Clearing the reference first would
    make the first failure permanent: the retry would delete nothing and
    release the lock on top of a stale descriptor.
    """
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    attempts = {"count": 0}
    real_unlink = Path.unlink

    def refusing_unlink(self, missing_ok=False):
        if self.name == DESCRIPTOR_NAME and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    with pytest.raises(PermissionError):
        service.close()
    assert service.descriptor is not None
    assert (tmp_path / DESCRIPTOR_NAME).exists()
    assert service.lock_held is True

    # The retry finishes the deletion and releases the lock.
    monkeypatch.undo()
    service.close()
    assert service.descriptor is None
    assert not (tmp_path / DESCRIPTOR_NAME).exists()
    assert service.lock_held is False

    # And a third close is still idempotent.
    service.close()
    assert service.lock_held is False


def test_a_descriptor_already_gone_still_counts_as_forgotten(tmp_path) -> None:
    """A missing file needs no deletion, and the tail goes on."""
    service = _service(tmp_path, port=free_loopback_port())
    service.start()
    (tmp_path / DESCRIPTOR_NAME).unlink()
    service.close()
    assert service.descriptor is None
    assert service.lock_held is False
