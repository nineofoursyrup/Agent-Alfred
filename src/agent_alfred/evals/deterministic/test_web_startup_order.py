"""Dashboard startup ordering and rollback boundaries."""

from __future__ import annotations

import errno
import io
import os
import socket
import sqlite3
import stat
import threading

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.database import open_database as file_database
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    managed_process_lock,
    scripted_factory,
)
from agent_alfred.gateway.web.lifecycle import (
    PortUnavailable,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.server import DashboardRuntime
from agent_alfred.managed_state import (
    ManagedDirectoryLease,
    ManagedFileLease,
    ManagedPathSecurityError,
)
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard


@pytest.mark.parametrize("root_kind", ("state", "external_trace"))
def test_dashboard_refuses_a_managed_root_with_a_symlink_direct_parent(
    tmp_path, root_kind: str
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    external.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    state = alias / "state" if root_kind == "state" else tmp_path / "state"
    trace_root = alias / "traces" if root_kind == "external_trace" else None
    before = external.stat()
    before_entries = list(external.iterdir())
    dashboard = build_dashboard(
        state_dir=state,
        trace_root=trace_root,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(ManagedPathSecurityError) as caught:
            dashboard.start()
        assert caught.value.reason == "wrong_type"
    finally:
        dashboard.close()
    after = external.stat()
    assert list(external.iterdir()) == before_entries
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    if root_kind == "external_trace":
        assert read_entry_descriptor(state) is None


def test_build_dashboard_refuses_unsafe_trace_root_and_rolls_back_prior_resources(
    tmp_path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    trace_root = tmp_path / "external-traces"
    trace_root.symlink_to(outside, target_is_directory=True)
    dashboard = build_dashboard(
        state_dir=state,
        trace_root=trace_root,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == "symlink"
    assert dashboard.state == "failed"
    assert read_entry_descriptor(state) is None
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert list(outside.iterdir()) == []


def test_dashboard_reports_managed_child_permission_denied_and_rolls_back(
    tmp_path, monkeypatch
) -> None:
    state = tmp_path / "state"
    real_mkdir = os.mkdir

    def deny_traces(path, mode=0o777, *, dir_fd=None):
        if path == "traces":
            raise PermissionError(errno.EACCES, "denied", path)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent_alfred.managed_state.os.mkdir", deny_traces)
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == "permission_denied"
    assert caught.value.errno == errno.EACCES
    assert dashboard.state == "failed"
    assert read_entry_descriptor(state) is None


@pytest.mark.parametrize("shape", ["empty", "partial", "full"])
def test_dashboard_start_reclaims_valid_staging_before_runtime_construction(
    tmp_path, monkeypatch, shape: str
) -> None:
    state = tmp_path / "state"
    token = "0123456789abcdef0123456789abcdef"
    staging = state / "traces" / "2026-08-28" / f".staging-{token}"
    staging.mkdir(parents=True, mode=0o700)
    if shape in {"partial", "full"}:
        (staging / "meta.json").write_text("{}", encoding="utf-8")
    if shape == "full":
        (staging / "trace.jsonl").write_text("", encoding="utf-8")
        (staging / "artifacts").mkdir(mode=0o700)
    directories = [state, state / "traces", staging.parent, staging]
    if shape == "full":
        directories.append(staging / "artifacts")
    for directory in directories:
        directory.chmod(0o700)
    for file in staging.iterdir():
        if not file.is_file():
            continue
        file.chmod(0o600)
    real_reclaim = ManagedDirectoryLease.reclaim_stale_trace_staging
    reclaimed_counts: list[int] = []

    def record_reclaimed_count(directory: ManagedDirectoryLease) -> int:
        result = real_reclaim(directory)
        reclaimed_counts.append(result)
        return result

    monkeypatch.setattr(
        ManagedDirectoryLease,
        "reclaim_stale_trace_staging",
        record_reclaimed_count,
    )

    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        dashboard.start()
        assert reclaimed_counts == [1]
        assert not staging.exists()
    finally:
        dashboard.close()


def test_dashboard_start_leaves_a_non_staging_reclaim_name_untouched(
    tmp_path, monkeypatch
) -> None:
    state = tmp_path / "state"
    reclaim = (
        state
        / "traces"
        / "2026-08-28"
        / (
            ".reclaim-0123456789abcdef0123456789abcdef-"
            "fedcba9876543210fedcba9876543210"
        )
    )
    reclaim.mkdir(parents=True, mode=0o700)
    for directory in (state, state / "traces", reclaim.parent, reclaim):
        directory.chmod(0o700)
    real_reclaim = ManagedDirectoryLease.reclaim_stale_trace_staging
    reclaimed_counts: list[int] = []

    def record_reclaimed_count(directory: ManagedDirectoryLease) -> int:
        result = real_reclaim(directory)
        reclaimed_counts.append(result)
        return result

    monkeypatch.setattr(
        ManagedDirectoryLease,
        "reclaim_stale_trace_staging",
        record_reclaimed_count,
    )
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        dashboard.start()
        assert reclaimed_counts == [0]
        assert reclaim.is_dir()
    finally:
        dashboard.close()


def test_dashboard_start_skips_a_staging_tree_under_an_impossible_date(
    tmp_path,
) -> None:
    state = tmp_path / "state"
    staging = (
        state
        / "traces"
        / "2026-99-99"
        / ".staging-0123456789abcdef0123456789abcdef"
    )
    staging.mkdir(parents=True, mode=0o700)
    marker = staging / "meta.json"
    marker.write_text("{}", encoding="utf-8")
    marker.chmod(0o600)
    before = (staging.stat(), marker.stat())
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        dashboard.start()
        after = (staging.stat(), marker.stat())
        assert [(item.st_ino, item.st_mode) for item in after] == [
            (item.st_ino, item.st_mode) for item in before
        ]
    finally:
        dashboard.close()


def test_dashboard_start_skips_an_unknown_nonempty_staging_atomically(
    tmp_path,
) -> None:
    state = tmp_path / "state"
    staging = (
        state
        / "traces"
        / "2026-08-28"
        / ".staging-fedcba9876543210fedcba9876543210"
    )
    artifacts = staging / "artifacts"
    artifacts.mkdir(parents=True, mode=0o700)
    meta = staging / "meta.json"
    trace = staging / "trace.jsonl"
    precious = artifacts / "precious"
    for path, payload in ((meta, "{}"), (trace, ""), (precious, "keep")):
        path.write_text(payload, encoding="utf-8")
        path.chmod(0o600)
    watched = (staging, artifacts, meta, trace, precious)
    before = {path: path.lstat() for path in watched}
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        dashboard.start()
        assert precious.read_text(encoding="utf-8") == "keep"
        for path in watched:
            after = path.lstat()
            assert (after.st_ino, after.st_mode) == (
                before[path].st_ino,
                before[path].st_mode,
            )
    finally:
        dashboard.close()


@pytest.mark.parametrize(
    "unmanaged_shape", ("invalid_name", "symlink", "fifo", "external_mode")
)
def test_dashboard_start_skips_other_unmanaged_staging_candidates_unchanged(
    tmp_path, unmanaged_shape: str
) -> None:
    state = tmp_path / "state"
    date = state / "traces" / "2026-08-28"
    date.mkdir(parents=True, mode=0o700)
    date.chmod(0o700)
    name = (
        ".staging-not-a-token"
        if unmanaged_shape == "invalid_name"
        else ".staging-0123456789abcdef0123456789abcdef"
    )
    candidate = date / name
    watched: list = [date, candidate]
    if unmanaged_shape == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o755)
        precious = outside / "precious"
        precious.write_text("keep", encoding="utf-8")
        candidate.symlink_to(outside, target_is_directory=True)
        watched.extend((outside, precious))
    else:
        candidate.mkdir(mode=0o700)
        if unmanaged_shape == "fifo":
            special = candidate / "trace.jsonl"
            os.mkfifo(special)
            watched.append(special)
        elif unmanaged_shape == "external_mode":
            candidate.chmod(0o755)
        else:
            marker = candidate / "meta.json"
            marker.write_text("{}", encoding="utf-8")
            marker.chmod(0o600)
            watched.append(marker)
    before = {path: path.lstat() for path in watched}
    dashboard = build_dashboard(
        state_dir=state,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        dashboard.start()
        for path in watched:
            after = path.lstat()
            assert (after.st_ino, after.st_mode, after.st_size) == (
                before[path].st_ino,
                before[path].st_mode,
                before[path].st_size,
            )
    finally:
        dashboard.close()


def test_host_construction_failure_closes_constructed_broker_and_prior_resources(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import wiring as wiring_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    closed: list[bool] = []

    class BrokerBeforeHost:
        name = "sse"

        def __init__(self, **kwargs):
            del kwargs

        def publish_state_patch(self, patch):
            del patch

        def publish_memory_patch(self, patch):
            del patch

        def close(self, timeout=0.0):
            del timeout
            closed.append(True)
            return True

    def fail_host(**kwargs):
        del kwargs
        raise RuntimeError("injected host construction failure")

    monkeypatch.setattr(wiring_module, "SSEBroker", BrokerBeforeHost)
    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    socket.getfqdn()
    baseline = _open_fd_count()
    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(RuntimeError, match="host construction failure"):
        dashboard.start()
    assert closed == [True]
    assert read_entry_descriptor(tmp_path) is None
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("host_outcome", (False, RuntimeError("host close failed")))
def test_dashboard_post_host_assembly_failure_retains_retryable_owner(
    tmp_path, monkeypatch, host_outcome
) -> None:
    from agent_alfred import wiring as wiring_module

    trace: list[str] = []
    assembly_failure = ValueError("post-host assembly failed")

    class Broker:
        name = "sse"

        def __init__(self, **kwargs):
            del kwargs

        def publish_state_patch(self, patch):
            del patch

        def publish_memory_patch(self, patch):
            del patch

        def bind_session_check(self, check):
            del check
            raise assembly_failure

        def close(self, timeout=0.0):
            del timeout
            trace.append("broker.close")
            return True

    class Host:
        close_calls = 0
        transport_session_validity = object()

        def close(self, timeout=None):
            del timeout
            self.close_calls += 1
            trace.append("host.close")
            if self.close_calls <= 2:
                if isinstance(host_outcome, BaseException):
                    raise host_outcome
                return host_outcome
            return True

    host = Host()
    monkeypatch.setattr(wiring_module, "SSEBroker", Broker)
    monkeypatch.setattr(wiring_module, "build_host", lambda **kwargs: host)
    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )

    with pytest.raises(ValueError) as caught:
        dashboard.start()
    assert caught.value is assembly_failure
    assert trace == ["host.close", "host.close"]
    assert "broker.close" not in trace, (
        "the broker remains available while the host may still publish"
    )
    assert dashboard.state == "closing"
    assert dashboard.close() is True
    assert host.close_calls == 3
    assert trace == ["host.close", "host.close", "host.close", "broker.close"]

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

    def __init__(self, lease: ManagedFileLease, log: list[str]) -> None:
        super().__init__(lease)
        self._log = log

    def acquire(self) -> None:
        self._log.append("lock")
        super().acquire()

    def release(self) -> None:
        if self.acquired:
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
        self.serving = threading.Event()

    def serve_forever(self) -> None:
        self._log.append("serve")
        self.serving.set()

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self._log.append("socket.close")
        self.closed = True


def _step_runtime(tmp_path, *, log, bind_error=None, describe_error=None):
    """A runtime whose every process-level step is a line in ``log``."""

    def server_factory(address, handler, owner):
        log.append("bind")
        if bind_error is not None:
            raise bind_error
        owner.publish(_FakeBoundServer(log))

    def write_descriptor(directory, descriptor):
        log.append("describe")
        if describe_error is not None:
            raise describe_error
        return write_entry_descriptor(directory, descriptor)

    def open_database(directory, *, _rollback):
        log.append("database")
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        _rollback.own(conn)
        return conn

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
        lock=lambda lease: _RecordingLock(lease, log),
    )


def test_a_lock_conflict_touches_nothing_at_all(tmp_path) -> None:
    """The lock is the first process-level ownership, or there is no start.

    A second instance that migrated the database, recovered a Run or wrote
    Run state before being refused would have altered the first instance's
    facts on its way out of the door -- which is the thing the lock exists
    to prevent, and it only holds if nothing precedes it.
    """
    holder = managed_process_lock(tmp_path)
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
    fresh = managed_process_lock(tmp_path)
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
    fresh = managed_process_lock(tmp_path)
    fresh.acquire()
    fresh.release()


def test_the_successful_path_runs_the_steps_in_one_order(tmp_path) -> None:
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log)
    descriptor = runtime.start()
    try:
        server = runtime.service.server
        assert isinstance(server, _FakeBoundServer)
        assert server.serving.wait(2.0), "serving thread did not enter the server"
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
    fresh = managed_process_lock(tmp_path)
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
