"""The Dashboard lifecycle: lock -> bind -> describe, and undone backwards.

Every test here is about order and about undoing. The three steps are one
indivisible whole (#23 §2), so the interesting cases are the failures: a step
that fails must leave nothing behind that could be mistaken for a running
Dashboard -- no held lock, no open descriptor, no descriptor naming a port
nobody is listening on.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

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


def _free_port() -> int:
    """A loopback port nobody is listening on, released immediately.

    The window between closing the probe and binding it for real is not
    zero, but a collision here fails the test loudly rather than passing it
    silently, which is the only property that matters.
    """
    probe = socket.socket()
    try:
        probe.bind((DEFAULT_HOST, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


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
    service = _service(tmp_path, port=_free_port())
    descriptor = service.start()
    assert service.lock_held is True
    assert service.started is True
    assert descriptor.port == service.port
    on_disk = read_entry_descriptor(tmp_path)
    assert on_disk == descriptor
    service.close()


def test_the_descriptor_names_instance_pid_and_port(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port(), pid=4242)
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
    port = _free_port()
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
    service = _service(tmp_path, port=_free_port())
    with pytest.raises(OSError):
        service.start()
    assert service.started is False
    assert service.lock_held is False
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_start_is_idempotent_and_returns_the_same_entry(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port())
    first = service.start()
    second = service.start()
    assert first == second
    assert service.server is not None
    service.close()


def test_close_undoes_everything_and_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port())
    service.start()
    server = service.server
    service.start_serving()
    service.close()
    # The serving loop is stopped and the listening socket is closed.
    assert server.shut_down is True
    assert server.closed is True
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
    service = _service(tmp_path, port=_free_port())
    service.start()
    server = service.server
    service.close()
    assert server.shut_down is False
    assert server.closed is True
    assert service.lock_held is False


def test_close_releases_the_lock_for_a_real_second_instance(tmp_path) -> None:
    first = _service(tmp_path, port=_free_port())
    first.start()
    first.close()
    second = _service(tmp_path, port=_free_port())
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
    service = _service(tmp_path, port=_free_port())
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
    service = _service(tmp_path, port=_free_port())
    try:
        service.start()
        for name in (LOCK_NAME, DESCRIPTOR_NAME):
            mode = stat.S_IMODE(os.stat(tmp_path / name).st_mode)
            assert mode == 0o600, f"{name} is {oct(mode)}"
    finally:
        service.close()


# --- serving --------------------------------------------------------------


def test_serving_without_start_is_refused(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port())
    with pytest.raises(RuntimeError):
        service.serve_forever()
    with pytest.raises(RuntimeError):
        service.start_serving()


def test_the_serving_thread_is_a_daemon(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port())
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


def test_a_negative_port_is_refused_before_anything_else(tmp_path) -> None:
    with pytest.raises(ValueError):
        _service(tmp_path, port=0)


# --- the real socket ------------------------------------------------------


def test_the_real_server_binds_only_the_loopback_address(tmp_path) -> None:
    """One real bind, on a random loopback port.

    The fake server above proves the ordering; only a real socket proves
    that the address is the one the threat model assumes and that nothing
    picked a different port behind our back.
    """
    port = _free_port()
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
    port = _free_port()
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
