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
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePath

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    file_database,
    managed_process_lock,
    scripted_factory,
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
from agent_alfred.managed_state import ManagedPathSecurityError, ManagedStateDirectory
from agent_alfred.wiring import build_dashboard


@pytest.mark.parametrize(
    ("drift", "reason"),
    (("owner", "wrong_owner"), ("type", "wrong_type")),
)
def test_dashboard_rejects_state_root_owner_or_type_drift_after_fchmod(
    tmp_path, monkeypatch, drift: str, reason: str
) -> None:
    """The public startup boundary rechecks every root invariant after chmod."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    real_fstat = managed_module._managed_fstat
    root_checks = 0

    def drift_after_fchmod(fd: int, *, role: str, path: Path):
        nonlocal root_checks
        observed = real_fstat(fd, role=role, path=path)
        if role != "state root":
            return observed
        root_checks += 1
        if root_checks != 3:
            return observed
        values = list(observed)
        if drift == "owner":
            values[4] = os.geteuid() + 1
        else:
            values[0] = stat.S_IFREG | stat.S_IMODE(observed.st_mode)
        return os.stat_result(values)

    monkeypatch.setattr(managed_module, "_managed_fstat", drift_after_fchmod)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == reason
    assert not (tmp_path / "state" / LOCK_NAME).exists()
    assert not (tmp_path / "state" / DESCRIPTOR_NAME).exists()
    assert not (tmp_path / "state" / "db.sqlite3").exists()
    assert _open_fd_count() == baseline


def _managed_child_for_descriptor_contract(state, kind: str):
    if kind == "file":
        return state.open_regular(
            PurePath("child"), access="read_write", create=True, role="test file"
        )
    return state.ensure_directory(PurePath("child"))


@pytest.mark.parametrize("kind", ("file", "directory"))
@pytest.mark.parametrize("failure_position", (1, 2))
def test_managed_capability_close_attempts_all_descriptors_and_preserves_first_error(
    tmp_path, monkeypatch, kind: str, failure_position: int
) -> None:
    """Both public lease kinds share the same progress-preserving close contract."""
    from agent_alfred import managed_state as managed_module

    state = ManagedStateDirectory.acquire(tmp_path / f"state-{kind}-{failure_position}")
    lease = _managed_child_for_descriptor_contract(state, kind)
    real_close = managed_module.os.close
    calls: list[int] = []

    def close_with_one_failure(fd: int) -> None:
        calls.append(fd)
        real_close(fd)
        if len(calls) == failure_position:
            raise OSError(errno.EIO, f"close failure {failure_position}")

    monkeypatch.setattr(managed_module.os, "close", close_with_one_failure)
    with pytest.raises(OSError, match=f"close failure {failure_position}"):
        lease.close()
    attempted = len(calls)
    assert attempted >= 2
    assert len(set(calls)) == attempted
    lease.close()
    assert len(calls) == attempted
    monkeypatch.setattr(managed_module.os, "close", real_close)
    state.close()


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_managed_capability_name_replacement_is_identity_changed(
    tmp_path, kind: str
) -> None:
    """The shared identity skeleton retains the file/directory predicate."""
    state = ManagedStateDirectory.acquire(tmp_path / f"state-{kind}")
    lease = _managed_child_for_descriptor_contract(state, kind)
    original = lease.path
    moved = original.with_name("moved")
    original.rename(moved)
    if kind == "file":
        original.write_bytes(b"replacement")
    else:
        original.mkdir()
    with pytest.raises(ManagedPathSecurityError) as caught:
        lease.verify_identity()
    assert caught.value.reason == "identity_changed"
    lease.close()
    state.close()


def _create_invalid_managed_object(path: Path, kind: str, outside: Path) -> None:
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_bytes(b"not the managed object")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to(outside, target_is_directory=outside.is_dir())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX managed paths")
@pytest.mark.parametrize(
    ("role", "kind"),
    (
        ("state", "file"),
        ("state", "fifo"),
        ("state", "symlink"),
        ("lock", "directory"),
        ("lock", "fifo"),
        ("lock", "symlink"),
        ("descriptor", "directory"),
        ("descriptor", "fifo"),
        ("descriptor", "symlink"),
    ),
)
def test_dashboard_public_entry_rejects_invalid_managed_object_matrix(
    tmp_path, role: str, kind: str
) -> None:
    """State root, lock and descriptor types are proved at the real start seam."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state = tmp_path / f"state-{role}-{kind}"
    outside = tmp_path / f"outside-{role}-{kind}"
    outside.mkdir()
    target = state
    if role != "state":
        state.mkdir()
        target = state / (LOCK_NAME if role == "lock" else DESCRIPTOR_NAME)
    _create_invalid_managed_object(target, kind, outside)
    before = outside.stat()
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == ("symlink" if kind == "symlink" else "wrong_type")
    after = outside.stat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert _open_fd_count() == baseline


@pytest.mark.parametrize(
    "managed_role", ("process lock", "descriptor temporary file")
)
def test_dashboard_public_entry_rejects_child_owner_drift_after_fchmod(
    tmp_path, monkeypatch, managed_role: str
) -> None:
    """Lock and descriptor owner checks are repeated after their chmod."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    real_fstat = managed_module._managed_fstat
    checks = 0

    def drift_owner(fd: int, *, role: str, path: Path):
        nonlocal checks
        observed = real_fstat(fd, role=role, path=path)
        if role != managed_role:
            return observed
        checks += 1
        if checks != 3:
            return observed
        values = list(observed)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(managed_module, "_managed_fstat", drift_owner)
    state = tmp_path / managed_role.replace(" ", "-")
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == "wrong_owner"
    assert not (state / DESCRIPTOR_NAME).exists()
    assert _open_fd_count() == baseline


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


def test_dashboard_refuses_symlink_state_dir_before_lock_bind_or_descriptor(
    tmp_path,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    state = tmp_path / "state"
    state.symlink_to(target, target_is_directory=True)
    service = _service(state)

    with pytest.raises(ManagedPathSecurityError) as caught:
        service.start()

    assert caught.value.reason == "symlink"
    assert caught.value.role == "state root"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / LOCK_NAME).exists()
    assert not (target / DESCRIPTOR_NAME).exists()


def test_dashboard_refuses_foreign_owned_state_dir_before_lock_bind_or_descriptor(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_alfred.managed_state.os.geteuid",
        lambda: os.stat(tmp_path).st_uid + 1,
    )
    service = _service(tmp_path)
    with pytest.raises(ManagedPathSecurityError) as caught:
        service.start()
    assert caught.value.reason == "wrong_owner"
    assert not (tmp_path / LOCK_NAME).exists()
    assert not (tmp_path / DESCRIPTOR_NAME).exists()


def test_dashboard_rechecks_mode_and_identity_after_tightening(
    tmp_path, monkeypatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    displaced = tmp_path / "displaced"
    real_open = os.open
    before_open = threading.Event()
    swapped = threading.Event()
    replacement_before: list[os.stat_result] = []

    def replace_root() -> None:
        before_open.wait()
        state.rename(displaced)
        state.mkdir(mode=0o755)
        replacement_before.append(state.stat())
        swapped.set()

    worker = threading.Thread(target=replace_root)
    worker.start()

    def swap_before_open(path, flags, *args, **kwargs):
        if (
            path == state.name
            and kwargs.get("dir_fd") is not None
            and not swapped.is_set()
        ):
            before_open.set()
            swapped.wait()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("agent_alfred.managed_state.os.open", swap_before_open)
    with pytest.raises(ManagedPathSecurityError) as caught:
        _service(state).start()
    worker.join()
    assert caught.value.reason == "identity_changed"
    replacement_after = state.stat()
    before = replacement_before[0]
    replacement_identity = (
        replacement_after.st_ino,
        replacement_after.st_mode,
        replacement_after.st_mtime_ns,
    )
    assert replacement_identity == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert not (state / LOCK_NAME).exists()
    assert not (displaced / LOCK_NAME).exists()


def test_dashboard_does_not_tighten_name_replaced_after_open(
    tmp_path, monkeypatch
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    opened_before = state.stat()
    displaced = tmp_path / "opened-original"
    before_tighten = threading.Event()
    swapped = threading.Event()
    replacement_before: list[os.stat_result] = []
    real_stat = os.stat
    root_stat_calls = 0

    def replace_root() -> None:
        before_tighten.wait()
        state.rename(displaced)
        state.mkdir(mode=0o755)
        replacement_before.append(state.stat())
        swapped.set()

    worker = threading.Thread(target=replace_root)
    worker.start()

    def stat_after_open(path, *args, **kwargs):
        nonlocal root_stat_calls
        if path == state.name and kwargs.get("dir_fd") is not None:
            root_stat_calls += 1
            if root_stat_calls == 2:
                before_tighten.set()
                swapped.wait()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("agent_alfred.managed_state.os.stat", stat_after_open)
    with pytest.raises(ManagedPathSecurityError) as caught:
        _service(state).start()
    worker.join()
    assert caught.value.reason == "identity_changed"
    after = state.stat()
    before = replacement_before[0]
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    displaced_after = displaced.stat()
    assert (
        displaced_after.st_ino,
        displaced_after.st_mode,
        displaced_after.st_mtime_ns,
    ) == (
        opened_before.st_ino,
        opened_before.st_mode,
        opened_before.st_mtime_ns,
    )


def test_dashboard_reports_state_root_permission_denied_and_rolls_back(
    tmp_path, monkeypatch
) -> None:
    real_open = os.open

    def deny_root(path, flags, *args, **kwargs):
        if path == tmp_path.name and kwargs.get("dir_fd") is not None:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("agent_alfred.managed_state.os.open", deny_root)
    with pytest.raises(ManagedPathSecurityError) as caught:
        _service(tmp_path).start()
    assert caught.value.reason == "permission_denied"
    assert caught.value.errno == errno.EACCES
    assert not (tmp_path / LOCK_NAME).exists()
    assert not (tmp_path / DESCRIPTOR_NAME).exists()


def test_managed_directory_enotdir_is_the_proven_wrong_type_classification(
    tmp_path,
) -> None:
    state = ManagedStateDirectory.acquire(tmp_path)
    (tmp_path / "child").write_bytes(b"not a directory")
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            state.ensure_directory(PurePath("child/grandchild"))
        assert caught.value.reason == "wrong_type"
        assert caught.value.errno == errno.ENOTDIR
    finally:
        state.close()


def test_process_lock_refuses_symlink_without_touching_target(tmp_path) -> None:
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    target.chmod(0o644)
    before = target.stat()
    (tmp_path / LOCK_NAME).symlink_to(target)
    with pytest.raises(ManagedPathSecurityError) as caught:
        _service(tmp_path).start()
    after = target.stat()
    assert caught.value.reason == "symlink"
    assert target.read_text(encoding="utf-8") == "outside"
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )


def test_dashboard_normalizes_a_vanished_post_open_lock_name(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    real_open = managed_module.os.open
    removed = False

    def open_then_remove(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal removed
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == LOCK_NAME and not removed:
            removed = True
            os.unlink(path, dir_fd=dir_fd)
        return fd

    monkeypatch.setattr(managed_module.os, "open", open_then_remove)
    socket.getfqdn()
    baseline = _open_fd_count()
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == "identity_changed"
    assert caught.value.role == "process lock"
    assert caught.value.errno == errno.ENOENT
    assert dashboard.state == "failed"
    assert _open_fd_count() == baseline


def test_injected_process_lock_refuses_symlink_without_touching_target(
    tmp_path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    target.chmod(0o644)
    before = target.stat()
    lock_path = state / LOCK_NAME
    lock_path.symlink_to(target)
    dashboard = build_dashboard(
        state_dir=state,
        lock=lambda lease: ProcessLock(lease),
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            dashboard.start()
        assert caught.value.reason == "symlink"
    finally:
        dashboard.close()
    after = target.stat()
    assert target.read_text(encoding="utf-8") == "outside"
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert read_entry_descriptor(state) is None


def test_process_lock_write_failure_spends_capability_without_fd_leak(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    real_ftruncate = os.ftruncate
    failed = False

    def fail_once(fd, length):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError(errno.EIO, "injected lock diagnostic failure")
        return real_ftruncate(fd, length)

    monkeypatch.setattr("agent_alfred.gateway.web.lifecycle.os.ftruncate", fail_once)
    with pytest.raises(OSError) as caught:
        _service(tmp_path / "state").start()
    assert caught.value.errno == errno.EIO
    assert _open_fd_count() == baseline

    retry = _service(tmp_path / "state")
    retry.start()
    retry.close()
    assert _open_fd_count() == baseline


def test_entry_descriptor_refuses_non_regular_existing_target(tmp_path) -> None:
    (tmp_path / DESCRIPTOR_NAME).mkdir()
    service = _service(tmp_path)
    with pytest.raises(ManagedPathSecurityError) as caught:
        service.start()
    assert caught.value.reason == "wrong_type"
    assert (tmp_path / DESCRIPTOR_NAME).is_dir()
    assert service.lock_held is False


# --- the lock -------------------------------------------------------------


def test_a_lock_is_exclusive_within_one_process(tmp_path) -> None:
    first = managed_process_lock(tmp_path)
    second = managed_process_lock(tmp_path)
    first.acquire()
    with pytest.raises(StateDirLocked) as caught:
        second.acquire()
    assert caught.value.path == tmp_path / LOCK_NAME
    assert first.acquired is True
    first.release()
    # A refused capability is spent; a fresh centrally issued one can acquire.
    second = managed_process_lock(tmp_path)
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
    lock = managed_process_lock(tmp_path)
    lock.acquire()
    assert lock.acquired is True
    lock.release()


def test_a_conflict_names_the_holder_when_the_file_says_who(tmp_path) -> None:
    holder = managed_process_lock(tmp_path)
    holder.acquire()
    try:
        with pytest.raises(StateDirLocked) as caught:
            managed_process_lock(tmp_path).acquire()
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
        managed_process_lock(tmp_path).acquire()

    assert caught.value.path == path
    assert caught.value.holder_pid == 123
    assert caught.value.__cause__ is flock_error


@pytest.mark.parametrize("failure_errno", (errno.EIO, errno.EINTR, errno.ENOLCK))
def test_a_non_contention_flock_failure_is_preserved_without_reading_a_holder(
    tmp_path, monkeypatch, failure_errno: int
) -> None:
    flock_error = OSError(failure_errno, os.strerror(failure_errno))
    holder_was_read = False

    def fail_lock(fd: int, operation: int) -> None:
        raise flock_error

    def record_unexpected_holder_read(*args, **kwargs):
        nonlocal holder_was_read
        holder_was_read = True
        raise AssertionError("a non-contention failure has no lock holder")

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    monkeypatch.setattr(os, "pread", record_unexpected_holder_read)

    with pytest.raises(OSError) as caught:
        managed_process_lock(tmp_path).acquire()

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
    holder = managed_process_lock(tmp_path)
    holder.acquire()
    path.write_text(f"pid={recorded_pid}\n", encoding="utf-8")
    try:
        with pytest.raises(StateDirLocked) as caught:
            managed_process_lock(tmp_path).acquire()
    finally:
        holder.release()
    assert caught.value.path == path
    assert caught.value.holder_pid is None


def test_a_canonical_recorded_pid_is_kept_as_lock_diagnostic(tmp_path) -> None:
    path = tmp_path / LOCK_NAME
    holder = managed_process_lock(tmp_path)
    holder.acquire()
    path.write_text("pid=123\n", encoding="utf-8")
    try:
        with pytest.raises(StateDirLocked) as caught:
            managed_process_lock(tmp_path).acquire()
    finally:
        holder.release()
    assert caught.value.holder_pid == 123


def test_process_lock_refuses_a_replaced_post_open_name(tmp_path) -> None:
    state = ManagedStateDirectory.acquire(tmp_path)
    lease = state.open_regular(
        PurePath(LOCK_NAME), access="read_write", create=True, role="process lock"
    )
    holder_fd = os.open(tmp_path / LOCK_NAME, os.O_RDWR)
    external = tmp_path.parent / f"{tmp_path.name}-external-pid"
    external.write_text("pid=456\n", encoding="utf-8")
    displaced = tmp_path / "original-lock"
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (tmp_path / LOCK_NAME).rename(displaced)
    (tmp_path / LOCK_NAME).symlink_to(external)
    contender = ProcessLock(lease)
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            contender.acquire()
        assert caught.value.reason == "identity_changed"
        assert external.read_text(encoding="utf-8") == "pid=456\n"
    finally:
        os.close(holder_fd)
        contender.release()
        state.close()


@pytest.mark.parametrize("process_control", (False, True))
def test_dashboard_releases_post_flock_duplicate_when_identity_recheck_fails(
    tmp_path, monkeypatch, process_control: bool
) -> None:
    """The duplicate that owns the real flock is rollback-owned immediately."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedFileLease

    socket.getfqdn()
    baseline = _open_fd_count()
    failure: BaseException = (
        KeyboardInterrupt()
        if process_control
        else ManagedPathSecurityError(
            reason="identity_changed",
            role="process lock",
            path=tmp_path / LOCK_NAME,
        )
    )
    real_verify = ManagedFileLease.verify_identity
    lock_verifications = 0

    def fail_post_flock_verify(lease):
        nonlocal lock_verifications
        real_verify(lease)
        if lease.path.name == LOCK_NAME:
            lock_verifications += 1
            if lock_verifications == 2:
                raise failure

    monkeypatch.setattr(ManagedFileLease, "verify_identity", fail_post_flock_verify)
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    expected = KeyboardInterrupt if process_control else ManagedPathSecurityError
    with pytest.raises(expected) as caught:
        dashboard.start()
    assert caught.value is failure
    assert _open_fd_count() == baseline
    monkeypatch.setattr(ManagedFileLease, "verify_identity", real_verify)
    successor = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


def test_dashboard_lock_rollback_never_closes_a_reused_duplicate(
    tmp_path, monkeypatch
) -> None:
    """A post-flock uncertain close cannot target a later occupant of its fd."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedFileLease

    socket.getfqdn()
    baseline = _open_fd_count()
    failure = ManagedPathSecurityError(
        reason="identity_changed",
        role="process lock",
        path=tmp_path / "state" / LOCK_NAME,
    )
    real_duplicate = ManagedFileLease.duplicate_fd
    real_close = managed_module.os.close
    real_verify = ManagedFileLease.verify_identity
    duplicate_fd: int | None = None
    lock_verifications = 0
    close_failed = False

    def remember_duplicate(lease):
        nonlocal duplicate_fd
        result = real_duplicate(lease)
        if lease.path.name == LOCK_NAME:
            duplicate_fd = result
        return result

    def refuse_post_flock(lease):
        nonlocal lock_verifications
        real_verify(lease)
        if lease.path.name == LOCK_NAME:
            lock_verifications += 1
            if lock_verifications == 2:
                raise failure

    def close_then_raise(fd):
        nonlocal close_failed
        real_close(fd)
        if fd == duplicate_fd and not close_failed:
            close_failed = True
            raise OSError(errno.EIO, "lock close result was uncertain")

    monkeypatch.setattr(ManagedFileLease, "duplicate_fd", remember_duplicate)
    monkeypatch.setattr(ManagedFileLease, "verify_identity", refuse_post_flock)
    monkeypatch.setattr(managed_module.os, "close", close_then_raise)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value is failure
    assert duplicate_fd is not None

    monkeypatch.setattr(managed_module.os, "close", real_close)
    replacements: list[int] = []
    while not replacements or replacements[-1] != duplicate_fd:
        replacements.append(os.open(os.devnull, os.O_RDONLY))
        assert len(replacements) < 64
    try:
        assert dashboard.close() is True
        os.fstat(duplicate_fd)
    finally:
        for descriptor in replacements:
            real_close(descriptor)
    assert _open_fd_count() == baseline


def test_dashboard_rechecks_lock_identity_after_writing_diagnostic(
    tmp_path, monkeypatch
) -> None:
    """The held flock is fenced again immediately before publication."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state_dir = tmp_path / "state"
    port = free_loopback_port()
    real_ftruncate = os.ftruncate
    replaced = False

    def replace_name_during_diagnostic(fd: int, length: int) -> None:
        nonlocal replaced
        real_ftruncate(fd, length)
        if replaced:
            return
        replaced = True
        lock_path = state_dir / LOCK_NAME
        lock_path.rename(state_dir / "displaced.lock")
        lock_path.write_bytes(b"successor-sentinel")
        os.chmod(lock_path, 0o600)

    monkeypatch.setattr(os, "ftruncate", replace_name_during_diagnostic)
    dashboard = build_dashboard(
        state_dir=state_dir,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            dashboard.start()
        assert caught.value.reason == "identity_changed"
    finally:
        dashboard.close()
    assert (state_dir / LOCK_NAME).read_bytes() == b"successor-sentinel"
    assert _open_fd_count() == baseline

    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    successor = build_dashboard(
        state_dir=state_dir,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("write_result", ("partial", "zero", "second_error"))
def test_dashboard_requires_a_complete_pid_diagnostic_before_publishing_lock(
    tmp_path, monkeypatch, write_result: str
) -> None:
    """The public start boundary never accepts a partial lock diagnostic."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state = tmp_path / f"state-pid-{write_result}"
    failure = OSError(errno.EIO, "second pid write failed")
    real_ftruncate = os.ftruncate
    real_write = os.write
    lock_fd: int | None = None
    writes = 0

    def remember_lock(fd: int, length: int) -> None:
        nonlocal lock_fd
        real_ftruncate(fd, length)
        lock_path = state / LOCK_NAME
        if lock_path.exists() and os.fstat(fd).st_ino == lock_path.stat().st_ino:
            lock_fd = fd

    def scripted_write(fd: int, payload) -> int:
        nonlocal writes
        if fd != lock_fd:
            return real_write(fd, payload)
        writes += 1
        if writes == 1 and write_result == "zero":
            return 0
        if writes == 1:
            return real_write(fd, memoryview(payload)[:2])
        if write_result == "second_error":
            raise failure
        return real_write(fd, payload)

    monkeypatch.setattr(os, "ftruncate", remember_lock)
    monkeypatch.setattr(os, "write", scripted_write)
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    if write_result == "partial":
        dashboard.start()
        assert (state / LOCK_NAME).read_bytes() == b"pid=%d\n" % os.getpid()
    else:
        with pytest.raises(OSError) as caught:
            dashboard.start()
        if write_result == "zero":
            assert caught.value.errno == errno.EIO
        else:
            assert caught.value is failure
        assert not (state / DESCRIPTOR_NAME).exists()
    assert dashboard.close() is True
    assert dashboard.close() is True
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    successor = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert (state / LOCK_NAME).read_bytes() == b"pid=%d\n" % os.getpid()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("operation", ("write", "fsync", "replace"))
def test_dashboard_descriptor_failure_unlinks_temp_after_uncertain_close(
    tmp_path, monkeypatch, operation: str
) -> None:
    """Descriptor publication keeps its business failure and removes its temp."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state = tmp_path / f"state-{operation}"
    temporary_fd: int | None = None
    close_failed = False
    failure = OSError(errno.EIO, f"descriptor {operation} failed")
    close_failure = RuntimeError("temporary close result was uncertain")
    real_open = managed_module.os.open
    real_write = managed_module.os.write
    real_fsync = managed_module.os.fsync
    real_replace = managed_module.os.replace
    real_close = managed_module.os.close

    def remember_temp(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal temporary_fd
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".dashboard.json."):
            temporary_fd = fd
        return fd

    def fail_write(fd, payload):
        if operation == "write" and fd == temporary_fd:
            raise failure
        return real_write(fd, payload)

    def fail_fsync(fd):
        if operation == "fsync" and fd == temporary_fd:
            raise failure
        return real_fsync(fd)

    def fail_replace(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        if operation == "replace" and source.startswith(".dashboard.json."):
            raise failure
        return real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def close_then_raise(fd):
        nonlocal close_failed
        real_close(fd)
        if operation != "replace" and fd == temporary_fd and not close_failed:
            close_failed = True
            raise close_failure

    monkeypatch.setattr(managed_module.os, "open", remember_temp)
    monkeypatch.setattr(managed_module.os, "write", fail_write)
    monkeypatch.setattr(managed_module.os, "fsync", fail_fsync)
    monkeypatch.setattr(managed_module.os, "replace", fail_replace)
    monkeypatch.setattr(managed_module.os, "close", close_then_raise)
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(BaseException) as caught:
        dashboard.start()
    assert caught.value is failure
    assert not list(state.glob(".dashboard.json.*.tmp"))
    assert dashboard.close() is True
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    successor = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("close_mode", ("false", "raise", "close_then_raise"))
def test_dashboard_never_publishes_descriptor_before_temp_cleanup_finishes(
    tmp_path, monkeypatch, close_mode: str
) -> None:
    """A descriptor becomes public only after its temporary lease is closed."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state = tmp_path / f"state-{close_mode}"
    failure = RuntimeError(f"temporary close {close_mode}")
    real_close = managed_module.ManagedFileLease.close
    attempts = 0

    def fail_first_temp_close(lease):
        nonlocal attempts
        if lease.path.name.startswith(".dashboard.json."):
            attempts += 1
            if attempts == 1:
                if close_mode == "false":
                    return False
                if close_mode == "close_then_raise":
                    real_close(lease)
                raise failure
        return real_close(lease)

    monkeypatch.setattr(
        managed_module.ManagedFileLease, "close", fail_first_temp_close
    )
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(RuntimeError) as caught:
        dashboard.start()
    if close_mode != "false":
        assert caught.value is failure
    assert not (state / DESCRIPTOR_NAME).exists()
    assert not list(state.glob(".dashboard.json.*.tmp"))
    assert dashboard.close() is True
    assert dashboard.close() is True
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    successor = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize(
    "failure_kind", ("ordinary", "process_control", "replaced_control")
)
def test_dashboard_owns_an_uncertain_descriptor_replace_result(
    tmp_path, monkeypatch, failure_kind: str
) -> None:
    """A replace that completed before raising cannot publish an orphan."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    state = tmp_path / f"state-{failure_kind}"
    failure: BaseException = (
        RuntimeError("replace result uncertain")
        if failure_kind == "ordinary"
        else SystemExit(73)
    )
    real_replace = managed_module.os.replace
    replacement_payload = b"replacement must survive"
    displaced = state / "published-by-failed-start"

    def replace_then_raise(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if failure_kind == "replaced_control":
            os.rename(
                target,
                displaced.name,
                src_dir_fd=dst_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            replacement_fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(replacement_fd, replacement_payload)
            finally:
                os.close(replacement_fd)
        raise failure

    monkeypatch.setattr(managed_module.os, "replace", replace_then_raise)
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(BaseException) as caught:
        dashboard.start()
    assert caught.value is failure
    assert not list(state.glob(".dashboard.json.*.tmp"))
    if failure_kind != "replaced_control":
        assert not (state / DESCRIPTOR_NAME).exists()
    else:
        assert (state / DESCRIPTOR_NAME).read_bytes() == replacement_payload
        assert displaced.exists()
    assert dashboard.close() is True
    assert dashboard.close() is True
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    if displaced.exists():
        displaced.unlink()
    successor = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline



@pytest.mark.parametrize("failure_point", ("constructor", "acquire"))
def test_lock_factory_failure_closes_child_and_state_leases_once(
    tmp_path, failure_point: str
) -> None:
    captured = []

    class FailingLock:
        def __init__(self, lease):
            self.lease = lease
            self.release_calls = 0
            captured.append(self)
            if failure_point == "constructor":
                raise RuntimeError("constructor failed")

        @property
        def acquired(self):
            return False

        def acquire(self):
            raise RuntimeError("acquire failed")

        def release(self):
            self.release_calls += 1
            self.lease.close()

    service = _service(tmp_path, lock=FailingLock)
    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        service.start()

    assert len(captured) == 1
    assert captured[0].lease._closed is True
    assert captured[0].release_calls == (0 if failure_point == "constructor" else 1)
    assert service._managed_state is None
    assert service.server is None
    assert read_entry_descriptor(tmp_path) is None


def test_dashboard_releases_a_factory_owned_lock_after_acquire_failure(
    tmp_path
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    socket.getfqdn()
    baseline = _open_fd_count()
    captured = []
    failure = RuntimeError("acquire failed after consuming lease")

    class ConsumingLock:
        def __init__(self, lease) -> None:
            self.lease = lease
            self.release_calls = 0
            captured.append(self)

        @property
        def acquired(self) -> bool:
            return False

        def acquire(self) -> None:
            self.lease.close()
            raise failure

        def release(self) -> None:
            self.release_calls += 1
            self.lease.close()

    state = tmp_path / "state"
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
        lock=ConsumingLock,
    )
    with pytest.raises(RuntimeError) as caught:
        dashboard.start()
    assert caught.value is failure
    assert captured[0].release_calls == 1
    assert dashboard.state == "failed"
    assert _open_fd_count() == baseline

    successor = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize(
    ("operation", "failure_errno"),
    (("fstat", errno.EACCES), ("dup", errno.EPERM)),
)
def test_managed_security_syscall_permission_denial_is_typed_and_rolled_back(
    tmp_path, monkeypatch, operation: str, failure_errno: int
) -> None:
    def refuse(*args, **kwargs):
        del args, kwargs
        raise OSError(failure_errno, os.strerror(failure_errno))

    monkeypatch.setattr(f"agent_alfred.managed_state.os.{operation}", refuse)
    service = _service(tmp_path, port=free_loopback_port())
    with pytest.raises(ManagedPathSecurityError) as caught:
        service.start()
    assert caught.value.reason == "permission_denied"
    assert caught.value.errno == failure_errno
    assert caught.value.operation == operation
    assert service.server is None
    assert service.lock_held is False
    assert read_entry_descriptor(tmp_path) is None


@pytest.mark.parametrize("failure_errno", (errno.EACCES, errno.EPERM))
def test_state_root_identity_stat_permission_denial_is_typed_and_retryable(
    tmp_path, monkeypatch, failure_errno: int
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    state_path = tmp_path / f"state-{failure_errno}"
    real_stat = os.stat

    def refuse(path, *args, **kwargs):
        if path == state_path.name and kwargs.get("dir_fd") is not None:
            raise OSError(failure_errno, os.strerror(failure_errno))
        return real_stat(path, *args, **kwargs)

    socket.getfqdn()
    baseline = _open_fd_count()
    monkeypatch.setattr("agent_alfred.managed_state.os.stat", refuse)
    dashboard = build_dashboard(
        state_dir=state_path,
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            dashboard.start()
    finally:
        dashboard.close()
    assert caught.value.reason == "permission_denied"
    assert caught.value.operation == "stat"
    assert caught.value.role == "state root"
    assert caught.value.errno == failure_errno
    assert "/bin/ls -ld --" in caught.value.repair_hint
    assert _open_fd_count() == baseline

    monkeypatch.setattr("agent_alfred.managed_state.os.stat", real_stat)
    retry = build_dashboard(
        state_dir=tmp_path / f"retry-{failure_errno}",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    retry.start()
    retry.close()
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("failure_errno", (errno.EACCES, errno.EPERM))
def test_process_lock_dup_permission_denial_is_typed_and_retryable(
    tmp_path, monkeypatch, failure_errno: int
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    real_dup = os.dup
    calls = 0

    def refuse(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(failure_errno, os.strerror(failure_errno))
        return real_dup(fd)

    socket.getfqdn()
    baseline = _open_fd_count()
    monkeypatch.setattr("agent_alfred.gateway.web.lifecycle.os.dup", refuse)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            dashboard.start()
    finally:
        dashboard.close()
    assert caught.value.reason == "permission_denied"
    assert caught.value.operation == "dup"
    assert caught.value.role == "process lock"
    assert caught.value.errno == failure_errno
    assert _open_fd_count() == baseline

    monkeypatch.setattr("agent_alfred.gateway.web.lifecycle.os.dup", real_dup)
    retry = build_dashboard(
        state_dir=tmp_path / "retry",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    retry.start()
    retry.close()
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("lease_kind", ("file", "directory"))
def test_managed_lease_close_never_retries_an_uncertain_fd_number(
    tmp_path, monkeypatch, lease_kind: str
) -> None:
    state = ManagedStateDirectory.acquire(tmp_path / "state")
    lease = (
        state.open_regular(
            PurePath("owned"), access="read_write", create=True, role="owned file"
        )
        if lease_kind == "file"
        else state.ensure_directory(PurePath("owned"))
    )
    target_fd = lease.fd
    real_close = os.close
    failed = False

    def close_then_raise(fd):
        nonlocal failed
        if fd == target_fd and not failed:
            failed = True
            real_close(fd)
            raise OSError(errno.EIO, "close result was uncertain")
        return real_close(fd)

    monkeypatch.setattr("agent_alfred.managed_state.os.close", close_then_raise)
    with pytest.raises(OSError, match="close result was uncertain"):
        lease.close()
    reused_fds = []
    while target_fd not in reused_fds:
        reused_fds.append(os.open("/dev/null", os.O_RDONLY))
        assert len(reused_fds) < 64
    lease.close()
    os.fstat(target_fd)
    for reused in reused_fds:
        real_close(reused)
    state.close()


def test_dashboard_rollback_never_double_closes_a_reused_state_fd(
    tmp_path, monkeypatch
) -> None:
    captured_state = []

    def capture_database(state):
        captured_state.append(state)
        return file_database(state)

    dashboard = build_dashboard(
        state_dir=tmp_path / "dashboard-close",
        factory=scripted_factory(),
        port=free_loopback_port(),
        open_database=capture_database,
    )
    dashboard.start()
    state = captured_state[0]
    target_fd = state.fd
    real_close = os.close
    failed = False

    def close_then_raise(fd):
        nonlocal failed
        if fd == target_fd and not failed:
            failed = True
            real_close(fd)
            raise OSError(errno.EIO, "dashboard state close was uncertain")
        return real_close(fd)

    monkeypatch.setattr("agent_alfred.managed_state.os.close", close_then_raise)
    with pytest.raises(OSError, match="dashboard state close was uncertain"):
        dashboard.close()
    reused_fds = []
    while target_fd not in reused_fds:
        reused_fds.append(os.open("/dev/null", os.O_RDONLY))
        assert len(reused_fds) < 64
    assert dashboard.close() is True
    os.fstat(target_fd)
    for reused in reused_fds:
        real_close(reused)

def test_an_unreadable_recorded_pid_cannot_mask_a_lock_conflict(tmp_path) -> None:
    path = tmp_path / LOCK_NAME
    holder = managed_process_lock(tmp_path)
    holder.acquire()
    path.write_bytes(b"pid=\xff\n")
    try:
        with pytest.raises(StateDirLocked) as caught:
            managed_process_lock(tmp_path).acquire()
    finally:
        holder.release()
    assert caught.value.holder_pid is None


def test_an_unexpected_lock_diagnostic_error_is_not_silenced(
    tmp_path, monkeypatch
) -> None:
    holder = managed_process_lock(tmp_path)
    holder.acquire()

    def explode(*args, **kwargs):
        raise RuntimeError("diagnostic bug")

    monkeypatch.setattr(os, "pread", explode)
    try:
        with pytest.raises(RuntimeError, match="diagnostic bug"):
            managed_process_lock(tmp_path).acquire()
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
from pathlib import Path, PurePath

from agent_alfred.gateway.web.lifecycle import ProcessLock, StateDirLocked
from agent_alfred.managed_state import ManagedStateDirectory

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "dashboard.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path.write_text("pid=" + "9" * 5_000 + "\\n", encoding="utf-8")
    state = ManagedStateDirectory.acquire(Path(directory).resolve())
    lease = state.open_regular(
        PurePath("dashboard.lock"), access="read_write", create=True
    )
    try:
        ProcessLock(lease).acquire()
    except StateDirLocked as exc:
        print(sys.get_int_max_str_digits(), exc.holder_pid)
    finally:
        state.close()
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
    lock = managed_process_lock(tmp_path)
    lock.acquire()
    lock.acquire()
    assert lock.acquired is True
    lock.release()
    lock.release()
    assert lock.acquired is False


def test_the_lock_file_records_the_owning_pid(tmp_path) -> None:
    lock = managed_process_lock(tmp_path)
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
    fresh = managed_process_lock(tmp_path)
    fresh.acquire()
    fresh.release()


def test_dashboard_bind_failure_retains_business_error_during_lock_cleanup(
    tmp_path,
) -> None:
    """Entry-stage cleanup is one resumable owner, not nested direct closes."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.resource_rollback import IncompleteRollback

    socket.getfqdn()
    baseline = _open_fd_count()
    port = free_loopback_port()
    squatter = socket.socket()
    squatter.bind((DEFAULT_HOST, port))
    squatter.listen(1)
    cleanup_failure = RuntimeError("lock release interrupted")

    class RefusingReleaseLock(ProcessLock):
        release_calls = 0

        def release(self) -> None:
            type(self).release_calls += 1
            if type(self).release_calls <= 2:
                raise cleanup_failure
            super().release()

    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
        lock=RefusingReleaseLock,
    )
    try:
        with pytest.raises(PortUnavailable) as caught:
            dashboard.start()
        assert caught.value.port == port
        assert isinstance(caught.value.__cause__, IncompleteRollback)
        assert caught.value.__cause__.errors == (cleanup_failure,)
        assert dashboard.state == "closing"
    finally:
        squatter.close()

    assert dashboard.close() is True
    assert dashboard.close() is True
    successor = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("cleanup_mode", ("process_control", "close_then_raise"))
def test_dashboard_bind_failure_normalizes_lock_cleanup_outcomes(
    tmp_path, cleanup_mode: str
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.resource_rollback import IncompleteRollback

    socket.getfqdn()
    baseline = _open_fd_count()
    port = free_loopback_port()
    squatter = socket.socket()
    squatter.bind((DEFAULT_HOST, port))
    squatter.listen(1)
    control = KeyboardInterrupt()
    cleanup_failure = RuntimeError("release completed with uncertain result")

    class ScriptedReleaseLock(ProcessLock):
        release_calls = 0

        def release(self) -> None:
            type(self).release_calls += 1
            if type(self).release_calls == 1:
                if cleanup_mode == "process_control":
                    raise control
                super().release()
                raise cleanup_failure
            super().release()

    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
        lock=ScriptedReleaseLock,
    )
    try:
        if cleanup_mode == "process_control":
            with pytest.raises(KeyboardInterrupt) as caught:
                dashboard.start()
            assert caught.value is control
            cleanup = control.__cause__
            assert isinstance(cleanup, IncompleteRollback)
            assert isinstance(cleanup.failure, PortUnavailable)
            assert dashboard.state == "closing"
        else:
            with pytest.raises(PortUnavailable):
                dashboard.start()
            assert dashboard.state == "failed"
    finally:
        squatter.close()

    assert dashboard.close() is True
    assert dashboard.close() is True
    successor = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


def test_a_descriptor_failure_also_rolls_the_bind_and_the_lock_back(
    tmp_path,
) -> None:
    def explode(directory: Path, descriptor: EntryDescriptor) -> Path:
        raise OSError("disk full")

    service = _service(
        tmp_path,
        port=free_loopback_port(),
        write_descriptor=explode,
    )
    with pytest.raises(OSError):
        service.start()
    assert service.started is False
    assert service.lock_held is False
    fresh = managed_process_lock(tmp_path)
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
    ("field", "value"),
    (
        ("instance_id", True),
        ("instance_id", ""),
        ("instance_id", "bad:instance"),
        ("pid", True),
        ("pid", 0),
        ("pid", 2_147_483_648),
        ("port", True),
        ("port", 0),
        ("port", 65_536),
    ),
)
def test_an_entry_descriptor_cannot_be_constructed_outside_its_wire_contract(
    field: str, value: object
) -> None:
    values = {"instance_id": "inst", "pid": 123, "port": 7717}
    values[field] = value

    with pytest.raises(ValueError):
        EntryDescriptor(**values)


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


def test_start_refuses_an_unopenable_directory_created_under_hostile_umask(
    tmp_path,
) -> None:
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
        with pytest.raises(ManagedPathSecurityError) as caught:
            service.start()
        assert caught.value.reason == "permission_denied"
        assert stat.S_IMODE(os.stat(state).st_mode) == 0o000
    finally:
        os.chmod(state, 0o700)
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

    def refuse(fd, mode):
        del fd, mode
        raise OSError(errno.EPERM, "cannot chmod")

    monkeypatch.setattr("agent_alfred.managed_state.os.fchmod", refuse)
    service = _service(tmp_path, port=free_loopback_port())
    with pytest.raises(ManagedPathSecurityError) as caught:
        service.start()
    assert caught.value.reason == "mode_tighten_failed"
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.EPERM
    assert caught.value.__cause__.strerror == "cannot chmod"
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


@pytest.mark.parametrize("port", (True, 0, 65_536))
def test_an_invalid_service_port_is_refused_without_side_effects(
    tmp_path, port: object
) -> None:
    state_dir = tmp_path / "state"
    side_effects: list[str] = []

    class RecordingLock:
        acquired = False

        def acquire(self) -> None:
            side_effects.append("lock")

    def bind(address, handler):
        side_effects.append("bind")
        return _RecordingServer(address, handler)

    def write(directory: Path, descriptor: EntryDescriptor) -> Path:
        side_effects.append("write")
        return directory / DESCRIPTOR_NAME

    with pytest.raises(ValueError):
        DashboardService(
            state_dir=state_dir,
            handler=_Handler,
            instance_id="inst-lifecycle",
            port=port,
            server_factory=bind,
            lock=RecordingLock(),
            write_descriptor=write,
        )

    assert side_effects == []
    assert not state_dir.exists()


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
    real_unlink = os.unlink

    def refusing_unlink(path, *, dir_fd=None):
        if path == DESCRIPTOR_NAME and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(1, "Operation not permitted", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("agent_alfred.managed_state.os.unlink", refusing_unlink)
    with pytest.raises(ManagedPathSecurityError) as caught:
        service.close()
    assert caught.value.reason == "permission_denied"
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
