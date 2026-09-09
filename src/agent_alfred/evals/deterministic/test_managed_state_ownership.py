"""Managed-state capability and construction ownership through public seams."""

from __future__ import annotations

import errno
import os
import socket
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import PurePath

import pytest

from agent_alfred.database import open_database as file_database
from agent_alfred.evals.deterministic._managed_state_ownership_test_helpers import (
    build_standalone_host,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    record_managed_state_acquires,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host


class _CloseRecordingLease:
    closed = False

    def close(self) -> None:
        self.closed = True


def _record_thread_failure(
    errors: list[BaseException], operation: Callable[[], None]
) -> None:
    try:
        operation()
    except BaseException as exc:  # noqa: BLE001 - asserted by the caller
        errors.append(exc)


def test_unlink_regular_detects_a_successor_after_open_verification(
    tmp_path, monkeypatch
) -> None:
    """The public remover fails closed when its verified name is replaced."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.managed_state import ManagedPathSecurityError

    state = managed_module.ManagedStateDirectory.acquire(tmp_path / "state")
    state.replace_bytes(PurePath("victim"), b"owned")
    target = state.path / "victim"
    displaced = state.path / "victim.owned"
    verified = threading.Event()
    release = threading.Event()
    real_managed_stat = managed_module._managed_stat
    checks = 0

    def gate_last_open_check(name, **kwargs):
        nonlocal checks
        observed = real_managed_stat(name, **kwargs)
        if os.fspath(name) == "victim" and kwargs.get("role") == "managed file":
            checks += 1
            if checks == 3:
                verified.set()
                assert release.wait(3.0)
        return observed

    monkeypatch.setattr(managed_module, "_managed_stat", gate_last_open_check)
    errors: list[BaseException] = []
    worker = threading.Thread(
        target=lambda: _record_thread_failure(
            errors,
            lambda: state.unlink_regular(PurePath("victim"), missing_ok=False),
        )
    )
    worker.start()
    try:
        assert verified.wait(3.0), "unlink never completed capability verification"
        target.rename(displaced)
        target.write_bytes(b"successor")
        target.chmod(0o600)
        release.set()
        worker.join(3.0)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ManagedPathSecurityError)
        assert errors[0].reason == "identity_changed"
        assert target.read_bytes() == b"successor"
        assert displaced.read_bytes() == b"owned"

        state.replace_bytes(PurePath("ordinary"), b"delete me")
        state.unlink_regular(PurePath("ordinary"), missing_ok=False)
        assert (state.path / "ordinary").exists() is False
    finally:
        release.set()
        worker.join(3.0)
        state.close()


def test_build_default_host_rejects_database_owner_drift_after_connect(
    tmp_path, monkeypatch
) -> None:
    """Connect cannot authorize migration from a stale pre-connect check."""
    from agent_alfred import database as database_module
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedPathSecurityError

    baseline = _open_fd_count()
    real_connect = database_module.sqlite3.connect
    real_fstat = managed_module._managed_fstat
    real_migrate = database_module.schema.migrate
    connected = False
    migration_calls = 0
    opened_connection = None

    def connect_then_drift(path, owner, **kwargs):
        nonlocal connected, opened_connection
        opened_connection = real_connect(path, **kwargs)
        owner.publish(opened_connection)
        connected = True

    def drift_owner(fd: int, *, role: str, path):
        observed = real_fstat(fd, role=role, path=path)
        if connected and role == "SQLite database":
            values = list(observed)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return observed

    def record_migration(conn):
        nonlocal migration_calls
        migration_calls += 1
        return real_migrate(conn)

    monkeypatch.setattr(database_module.sqlite3, "connect", connect_then_drift)
    monkeypatch.setattr(managed_module, "_managed_fstat", drift_owner)
    monkeypatch.setattr(database_module.schema, "migrate", record_migration)
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_standalone_host(tmp_path / "state")
    assert caught.value.reason == "wrong_owner"
    assert migration_calls == 0
    assert opened_connection is not None
    with pytest.raises(sqlite3.ProgrammingError):
        opened_connection.execute("SELECT 1")
    assert _open_fd_count() == baseline


def test_build_default_host_closes_child_capability_when_parent_recheck_fails(
    tmp_path, monkeypatch
) -> None:
    """A freshly minted DB capability stays locally owned until delivery."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedPathSecurityError

    baseline = _open_fd_count()
    real_fstat = managed_module._managed_fstat
    child_was_minted = False
    refused = False

    def refuse_parent_after_child(fd: int, *, role: str, path):
        nonlocal child_was_minted, refused
        observed = real_fstat(fd, role=role, path=path)
        if role == "SQLite database":
            child_was_minted = True
        elif child_was_minted and role != "SQLite database" and not refused:
            refused = True
            values = list(observed)
            values[1] += 1
            return os.stat_result(values)
        return observed

    monkeypatch.setattr(managed_module, "_managed_fstat", refuse_parent_after_child)
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_standalone_host(tmp_path / "state")
    assert caught.value.reason == "identity_changed"
    assert refused is True
    assert _open_fd_count() == baseline


def test_build_default_host_closes_trace_root_when_thread_construction_interrupts(
    tmp_path, monkeypatch
) -> None:
    """The trace root is owned before construction of its drain thread."""
    from agent_alfred import trace as trace_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    control = SystemExit(77)

    def interrupt_thread(*args, **kwargs):
        del args, kwargs
        raise control

    monkeypatch.setattr(trace_module.threading, "Thread", interrupt_thread)
    with pytest.raises(SystemExit) as caught:
        build_standalone_host(tmp_path / "state")
    assert caught.value is control
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("entry", ("state", "standalone"))
def test_trace_root_wrapper_replaces_its_lease_in_the_offered_owner(
    tmp_path, entry: str
) -> None:
    """The public wrapper, not its hidden lease, crosses the return edge."""
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ResumableRollback

    owner = ResumableRollback()
    state = None
    root = None
    try:
        if entry == "state":
            state = ManagedStateDirectory.acquire(tmp_path / "state")
            root = state.ensure_trace_directory(
                PurePath("traces"), _rollback=owner
            )
        else:
            root = ManagedStateDirectory.acquire_trace_root(
                tmp_path / "standalone-traces", _rollback=owner
            )

        owner.transfer(root)
        assert owner.retry() is True
        probe = root.ensure_directory(PurePath("still-live"))
        probe.close()
    finally:
        if root is not None:
            root.close()
        owner.retry()
        if state is not None:
            state.close()


def test_trace_sink_transfer_retires_the_old_trace_root_owner(tmp_path) -> None:
    """A transferred aggregate cannot leave a hidden lease behind it."""
    from agent_alfred.clock import FakeClock
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ResumableRollback
    from agent_alfred.trace import RunBundleTraceSink

    owner = ResumableRollback()
    root = ManagedStateDirectory.acquire_trace_root(
        tmp_path / "traces", _rollback=owner
    )
    sink = None
    try:
        sink = RunBundleTraceSink(
            root=root,
            clock=FakeClock(),
            process_instance_id="trace-owner-test",
            _rollback=owner,
        )
        owner.transfer(sink)
        assert owner.retry() is True

        probe = root.ensure_directory(PurePath("still-live"))
        probe.close()
    finally:
        if sink is not None:
            assert sink.close(timeout=2.0) is True
        else:
            owner.retry()


def test_build_default_host_exposes_incomplete_ordinary_trace_cleanup(
    tmp_path, monkeypatch
) -> None:
    """Standalone construction exposes the same resumable trace-root owner."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred import trace as trace_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.resource_rollback import IncompleteRollback

    baseline = _open_fd_count()
    failure = RuntimeError("trace thread construction failed")
    real_thread = trace_module.threading.Thread
    real_close = managed_module.ManagedTraceRoot.close
    close_attempts = 0

    def fail_trace_thread(*args, **kwargs):
        if kwargs.get("name") == "trace-drain":
            raise failure
        return real_thread(*args, **kwargs)

    def refuse_twice(root):
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts <= 2:
            return False
        return real_close(root)

    monkeypatch.setattr(trace_module.threading, "Thread", fail_trace_thread)
    monkeypatch.setattr(managed_module.ManagedTraceRoot, "close", refuse_twice)
    with pytest.raises(RuntimeError) as caught:
        build_standalone_host(tmp_path / "state")
    assert caught.value is failure
    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert cleanup.retry() is False
    assert cleanup.retry() is True
    assert close_attempts == 3
    assert _open_fd_count() == baseline


def test_dashboard_closes_trace_root_when_drain_start_interrupts(
    tmp_path, monkeypatch
) -> None:
    """Dashboard assembly uses the same trace-root construction owner."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred import trace as trace_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.wiring import build_dashboard

    socket.getfqdn()
    baseline = _open_fd_count()
    control = SystemExit(78)
    real_thread = trace_module.threading.Thread
    real_root_close = managed_module.ManagedTraceRoot.close
    root_close_calls = 0

    def refuse_first_root_close(root):
        nonlocal root_close_calls
        root_close_calls += 1
        if root_close_calls == 1:
            return False
        return real_root_close(root)

    def interrupt_constructor(*args, **kwargs):
        del args, kwargs
        monkeypatch.setattr(trace_module.threading, "Thread", real_thread)
        raise control

    monkeypatch.setattr(
        trace_module.threading,
        "Thread",
        interrupt_constructor,
    )
    monkeypatch.setattr(
        managed_module.ManagedTraceRoot, "close", refuse_first_root_close
    )
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(SystemExit) as caught:
        dashboard.start()
    assert caught.value is control
    assert dashboard.state == "closing"
    assert root_close_calls == 1
    monkeypatch.setattr(trace_module.threading, "Thread", real_thread)
    assert dashboard.close() is True
    assert root_close_calls == 2
    assert _open_fd_count() == baseline


def test_dashboard_trace_init_waits_for_root_cleanup_before_downgrade(
    tmp_path, monkeypatch
) -> None:
    """Ordinary trace failure cannot be downgraded while its root remains owned."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred import trace as trace_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import IncompleteRollback
    from agent_alfred.wiring import build_dashboard

    socket.getfqdn()
    baseline = _open_fd_count()
    failure = RuntimeError("trace thread construction failed")
    real_thread = trace_module.threading.Thread
    real_close = managed_module.ManagedTraceRoot.close
    close_attempts = 0

    def fail_trace_thread(*args, **kwargs):
        if kwargs.get("name") == "trace-drain":
            raise failure
        return real_thread(*args, **kwargs)

    def refuse_twice(root):
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts <= 2:
            return False
        return real_close(root)

    monkeypatch.setattr(trace_module.threading, "Thread", fail_trace_thread)
    monkeypatch.setattr(managed_module.ManagedTraceRoot, "close", refuse_twice)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(RuntimeError) as caught:
        dashboard.start()
    assert caught.value is failure
    assert isinstance(caught.value.__cause__, IncompleteRollback)
    assert dashboard.state == "closing"
    assert dashboard.close() is True
    assert close_attempts == 3
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("entry", ("standalone", "dashboard"))
@pytest.mark.parametrize("cleanup_mode", ("false", "ordinary", "process_control"))
def test_trace_root_is_owned_before_sink_constructor_accepts_it(
    tmp_path, monkeypatch, entry: str, cleanup_mode: str
) -> None:
    """The wiring owner covers the acquire-to-constructor handoff window."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import IncompleteRollback

    socket.getfqdn()
    baseline = _open_fd_count()
    failure = RuntimeError("sink constructor refused before ownership transfer")
    cleanup_failure = RuntimeError("trace root close failed")
    control = SystemExit(72)
    real_close = managed_module.ManagedTraceRoot.close
    attempts = 0

    def refuse_constructor(**kwargs):
        del kwargs
        raise failure

    def scripted_close(root):
        nonlocal attempts
        attempts += 1
        if cleanup_mode == "process_control" and attempts == 1:
            raise control
        if cleanup_mode == "false" and attempts <= 2:
            return False
        if cleanup_mode == "ordinary" and attempts <= 2:
            raise cleanup_failure
        return real_close(root)

    def find_cleanup(exc: BaseException) -> IncompleteRollback | None:
        pending = [exc]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, IncompleteRollback):
                return current
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)
        return None

    monkeypatch.setattr(wiring_module, "RunBundleTraceSink", refuse_constructor)
    monkeypatch.setattr(managed_module.ManagedTraceRoot, "close", scripted_close)
    state = tmp_path / f"{entry}-{cleanup_mode}"
    dashboard = None
    if entry == "standalone":
        def build():
            return build_default_host(
                state_dir=state,
                factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            )
    else:
        dashboard = wiring_module.build_dashboard(
            state_dir=state,
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            clock=FakeClock(),
            port=free_loopback_port(),
            open_database=file_database,
        )
        build = dashboard.start

    dominant = control if cleanup_mode == "process_control" else failure
    with pytest.raises(type(dominant)) as caught:
        build()
    assert caught.value is dominant
    cleanup = find_cleanup(caught.value)
    assert cleanup is not None
    assert cleanup.failure is failure
    if entry == "standalone":
        if cleanup_mode != "process_control":
            assert cleanup.retry() is False
        assert cleanup.retry() is True
    else:
        assert dashboard is not None
        assert dashboard.state == "closing"
        assert dashboard.close() is True
        assert dashboard.close() is True
    assert attempts == (2 if cleanup_mode == "process_control" else 3)
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    successor = wiring_module.build_dashboard(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel(["successor"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


def test_dashboard_database_process_control_rolls_back_prior_resources(
    tmp_path
) -> None:
    """A DB-step control signal cannot bypass the runtime-wide rollback."""
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.wiring import build_dashboard

    socket.getfqdn()
    baseline = _open_fd_count()
    control = SystemExit(79)
    state_dir = tmp_path / "state"
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=state_dir,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=port,
        open_database=lambda state, *, _rollback: (_ for _ in ()).throw(control),
    )
    with pytest.raises(SystemExit) as caught:
        dashboard.start()
    assert caught.value is control
    assert dashboard.state == "failed"
    assert _open_fd_count() == baseline

    successor = build_dashboard(
        state_dir=state_dir,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


def test_dashboard_close_resumes_database_inner_rollback_owner(
    tmp_path, monkeypatch
) -> None:
    """A DB construction owner remains reachable from the Dashboard."""
    from agent_alfred import database as database_module
    from agent_alfred import managed_state as managed_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.wiring import build_dashboard

    socket.getfqdn()
    baseline = _open_fd_count()
    control = SystemExit(81)
    close_calls = 0
    real_close = managed_module.ManagedFileLease.close

    def refuse_first_database_close(lease):
        nonlocal close_calls
        if lease.path.name == "db.sqlite3":
            close_calls += 1
            if close_calls == 1:
                return False
        return real_close(lease)

    monkeypatch.setattr(
        managed_module.ManagedFileLease, "close", refuse_first_database_close
    )
    monkeypatch.setattr(
        database_module.schema,
        "migrate",
        lambda conn: (_ for _ in ()).throw(control),
    )
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
    )
    with pytest.raises(SystemExit) as caught:
        dashboard.start()
    assert caught.value is control
    assert dashboard.state == "closing"
    assert close_calls == 1
    assert dashboard.close() is True
    assert close_calls == 2
    assert _open_fd_count() == baseline

def test_dashboard_retry_propagates_cleanup_control_and_resumes_exact_progress(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import IncompleteRollback

    business_failure = ValueError("assembly failed")
    control = SystemExit(80)

    class PendingResource:
        calls = 0

        def close(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                return False
            if self.calls == 2:
                raise control
            return True

    pending = PendingResource()

    def fail_host(**kwargs):
        kwargs["_rollback"].own(pending)
        raise business_failure

    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    with pytest.raises(SystemExit) as caught:
        dashboard.start()
    assert caught.value is control
    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert cleanup.failure is business_failure
    assert pending.calls == 2
    assert dashboard.state == "closing"
    assert dashboard.close() is True
    assert pending.calls == 3


def test_repeated_same_cleanup_control_keeps_an_acyclic_retry_handle() -> None:
    """Repeated delivery of one control object never makes its cause cyclic."""
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback

    control = KeyboardInterrupt()
    attempts = 0

    class Resource:
        def close(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise control

    rollback = ResumableRollback()
    rollback.own(Resource())
    with pytest.raises(KeyboardInterrupt) as first:
        rollback.close()
    assert first.value is control
    wrapper = control.__cause__
    assert isinstance(wrapper, IncompleteRollback)

    with pytest.raises(KeyboardInterrupt) as second:
        wrapper.retry()
    assert second.value is control
    assert control.__cause__ is wrapper
    assert wrapper.__cause__ is not control
    seen: set[int] = set()
    current: BaseException | None = control
    while current is not None:
        assert id(current) not in seen
        seen.add(id(current))
        current = current.__cause__

    assert wrapper.retry() is True
    assert attempts == 3
    assert control.__cause__ is None
    assert wrapper.retry() is True
    assert attempts == 3
    assert control.__cause__ is None


@pytest.mark.parametrize("business_edge", ("cause", "context"))
@pytest.mark.parametrize("control_relation", ("same", "different"))
def test_direct_rollback_binds_control_without_an_exception_graph_cycle(
    business_edge: str, control_relation: str
) -> None:
    """The first typed retry handle preserves diagnostics without a cycle."""
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback

    dominant = KeyboardInterrupt()
    original_cause = RuntimeError("original control cause")
    dominant.__cause__ = original_cause
    linked_control = dominant if control_relation == "same" else SystemExit(74)
    business = RuntimeError("business failure")
    if business_edge == "cause":
        business.__cause__ = linked_control
    else:
        business.__context__ = linked_control
    attempts = 0

    def close() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise dominant

    def assert_acyclic(root: BaseException) -> None:
        active: set[int] = set()
        complete: set[int] = set()

        def visit(current: BaseException | None) -> None:
            if current is None or id(current) in complete:
                return
            assert id(current) not in active
            active.add(id(current))
            visit(current.__cause__)
            visit(current.__context__)
            active.remove(id(current))
            complete.add(id(current))

        visit(root)

    rollback = ResumableRollback()
    rollback.own(object(), close)
    with pytest.raises(KeyboardInterrupt) as first:
        rollback.raise_failure(business)
    assert first.value is dominant
    wrapper = dominant.__cause__
    assert isinstance(wrapper, IncompleteRollback)
    assert wrapper.owner is rollback
    assert wrapper.failure is business
    assert_acyclic(dominant)

    with pytest.raises(KeyboardInterrupt) as second:
        wrapper.retry()
    assert second.value is dominant
    assert dominant.__cause__ is wrapper
    assert_acyclic(dominant)

    assert wrapper.retry() is True
    assert attempts == 3
    assert dominant.__cause__ is original_cause
    assert_acyclic(dominant)
    assert wrapper.retry() is True
    assert attempts == 3


def test_dashboard_captures_rollback_owners_from_both_exception_branches(
    tmp_path, monkeypatch
) -> None:
    """Cause and context are both traversed without duplicating shared owners."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import ResumableRollback

    socket.getfqdn()
    baseline = _open_fd_count()

    class RefusingDescriptor:
        def __init__(self) -> None:
            self.fd = os.open("/dev/null", os.O_RDONLY)
            self.calls = 0

        def close(self) -> bool:
            self.calls += 1
            if self.calls <= 2:
                return False
            fd, self.fd = self.fd, -1
            os.close(fd)
            return True

    resources = [RefusingDescriptor(), RefusingDescriptor()]

    def failure_with_owner(index: int) -> RuntimeError:
        rollback = ResumableRollback()
        rollback.own(resources[index])
        failure = RuntimeError(f"branch {index}")
        with pytest.raises(RuntimeError) as caught:
            rollback.raise_failure(failure)
        return caught.value

    branch_cause = failure_with_owner(0)
    branch_context = failure_with_owner(1)
    assembly_failure = RuntimeError("assembly graph failed")
    assembly_failure.__cause__ = branch_cause
    assembly_failure.__context__ = branch_context

    def fail_assembly(**kwargs):
        del kwargs
        raise assembly_failure

    monkeypatch.setattr(wiring_module, "build_host", fail_assembly)
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            dashboard.start()
        assert caught.value is assembly_failure
        assert dashboard.state == "closing"
        assert dashboard.close() is True
        assert dashboard.close() is True
        assert [resource.calls for resource in resources] == [3, 3]
        assert [resource.fd for resource in resources] == [-1, -1]
        assert _open_fd_count() == baseline
    finally:
        for resource in resources:
            if resource.fd >= 0:
                os.close(resource.fd)
                resource.fd = -1


def test_dashboard_nested_rollback_continues_independent_owner_and_exposes_slot(
    tmp_path, monkeypatch
) -> None:
    """A nested control cannot skip an independent construction owner."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    socket.getfqdn()
    baseline = _open_fd_count()
    control = KeyboardInterrupt()

    class ControlledDescriptor:
        def __init__(self, *, control_attempts: int = 0) -> None:
            self.fd = os.open("/dev/null", os.O_RDONLY)
            self.calls = 0
            self.control_attempts = control_attempts

        def close(self) -> bool:
            self.calls += 1
            if self.calls <= self.control_attempts:
                raise control
            if self.control_attempts == 0 and self.calls == 1:
                return False
            fd, self.fd = self.fd, -1
            os.close(fd)
            return True

    nested_resource = ControlledDescriptor(control_attempts=3)
    independent_resource = ControlledDescriptor()

    nested_owner = ResumableRollback()
    nested_owner.own(nested_resource)
    nested_slot = RollbackSlot()
    nested_slot.begin(nested_owner)
    nested_business = RuntimeError("nested cleanup")
    nested_slot.capture_failure(nested_business)
    with pytest.raises(KeyboardInterrupt) as nested_caught:
        nested_slot.retry()
    assert nested_caught.value is control

    independent_owner = ResumableRollback()
    independent_owner.own(independent_resource)
    independent_business = RuntimeError("independent cleanup")
    with pytest.raises(RuntimeError) as independent_caught:
        independent_owner.raise_failure(independent_business)

    assembly_failure = RuntimeError("assembly failed")
    assembly_failure.__cause__ = independent_caught.value
    assembly_failure.__context__ = nested_caught.value

    def fail_host(**kwargs):
        del kwargs
        raise assembly_failure

    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    try:
        with pytest.raises(KeyboardInterrupt) as start_caught:
            dashboard.start()
        assert start_caught.value is control
        aggregate = control.__cause__
        assert isinstance(aggregate, IncompleteRollback)
        assert isinstance(aggregate.owner, RollbackSlot)
        assert aggregate.owner.owns(nested_slot)
        assert independent_resource.calls == 2
        assert independent_resource.fd == -1
        assert nested_resource.calls == 2

        with pytest.raises(KeyboardInterrupt) as close_caught:
            dashboard.close()
        assert close_caught.value is control
        assert control.__cause__ is aggregate
        assert independent_resource.calls == 2
        assert nested_resource.calls == 3
        assert dashboard.close() is True
        assert dashboard.close() is True
        assert nested_resource.calls == 4
        assert nested_resource.fd == -1
        assert _open_fd_count() == baseline
    finally:
        for resource in (nested_resource, independent_resource):
            if resource.fd >= 0:
                os.close(resource.fd)
                resource.fd = -1


@pytest.mark.parametrize("control_shape", ("same", "distinct"))
def test_nested_rollback_deduplicates_descendant_and_continues_every_owner(
    control_shape: str,
) -> None:
    """A parent slot advances each logical child at most once per retry."""
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    first_control = KeyboardInterrupt()
    second_control = (
        first_control if control_shape == "same" else SystemExit(75)
    )
    attempts = {"child": 0, "ordinary": 0, "independent": 0}

    def controlled(name: str, failure: BaseException, failures: int = 2):
        def close() -> None:
            attempts[name] += 1
            if attempts[name] <= failures:
                raise failure

        return close

    child = ResumableRollback()
    child.own(object(), controlled("child", first_control))
    ordinary = ResumableRollback()
    ordinary.own(object(), controlled("ordinary", RuntimeError("cleanup"), 1))
    parent = RollbackSlot()
    parent.begin(child)
    parent.begin(ordinary)

    independent = ResumableRollback()
    independent.own(object(), controlled("independent", second_control))
    outer = RollbackSlot()
    outer.begin(child)
    outer.begin(parent)
    outer.begin(independent)
    business = RuntimeError("outer business failure")
    outer.capture_failure(business)

    for expected_attempt in (1, 2):
        with pytest.raises(type(second_control)) as caught:
            outer.retry()
        assert caught.value is second_control
        wrapper = second_control.__cause__
        assert isinstance(wrapper, IncompleteRollback)
        assert wrapper.owner is outer
        assert attempts["child"] == expected_attempt
        assert attempts["independent"] == expected_attempt
        assert attempts["ordinary"] == min(expected_attempt, 2)

    assert outer.retry() is True
    assert attempts == {"child": 3, "ordinary": 2, "independent": 3}
    assert second_control.__cause__ is None
    assert outer.retry() is True
    assert attempts == {"child": 3, "ordinary": 2, "independent": 3}


@pytest.mark.parametrize("failure_kind", ("false", "ordinary", "control"))
@pytest.mark.parametrize("depth", (1, 2))
def test_rollback_slot_shared_sibling_leaf_advances_once_per_outer_retry(
    failure_kind: str, depth: int
) -> None:
    """Overlapping sibling forests share one retry budget per logical leaf."""
    from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

    control = KeyboardInterrupt()
    attempts = 0

    def close() -> bool | None:
        nonlocal attempts
        attempts += 1
        if attempts != 1:
            return None
        if failure_kind == "false":
            return False
        if failure_kind == "ordinary":
            raise RuntimeError("shared leaf cleanup failed")
        raise control

    leaf = ResumableRollback()
    leaf.own(object(), close)
    left = RollbackSlot()
    left.begin(leaf)
    right = RollbackSlot()
    right.begin(leaf)
    if depth == 2:
        nested = RollbackSlot()
        nested.begin(right)
        right = nested
    outer = RollbackSlot()
    outer.begin(left)
    outer.begin(right)

    if failure_kind == "control":
        with pytest.raises(KeyboardInterrupt) as caught:
            outer.retry()
        assert caught.value is control
    else:
        assert outer.retry() is False
    assert attempts == 1
    assert outer.owns(leaf)

    assert outer.retry() is True
    assert attempts == 2
    assert outer.owns(leaf) is False
    assert outer.retry() is True
    assert attempts == 2


@pytest.mark.parametrize("control_shape", ("same", "distinct"))
def test_rollback_slot_shared_siblings_preserve_dominant_control(
    control_shape: str,
) -> None:
    """Shared leaves are deduplicated while independent controls all run."""
    from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

    right_control = KeyboardInterrupt()
    left_control = (
        right_control if control_shape == "same" else SystemExit(76)
    )
    attempts = {"shared": 0, "left": 0, "right": 0}

    def shared_close() -> bool | None:
        attempts["shared"] += 1
        return False if attempts["shared"] == 1 else None

    def controlled_close(name: str, control: BaseException):
        def close() -> None:
            attempts[name] += 1
            if attempts[name] == 1:
                raise control

        return close

    shared = ResumableRollback()
    shared.own(object(), shared_close)
    left_control_owner = ResumableRollback()
    left_control_owner.own(object(), controlled_close("left", left_control))
    left = RollbackSlot()
    left.begin(shared)
    left.begin(left_control_owner)
    right_control_owner = ResumableRollback()
    right_control_owner.own(object(), controlled_close("right", right_control))
    right = RollbackSlot()
    right.begin(shared)
    right.begin(right_control_owner)
    outer = RollbackSlot()
    outer.begin(left)
    outer.begin(right)

    with pytest.raises(KeyboardInterrupt) as caught:
        outer.retry()
    assert caught.value is right_control
    assert attempts == {"shared": 1, "left": 1, "right": 1}

    assert outer.retry() is True
    assert attempts == {"shared": 2, "left": 2, "right": 2}
    assert right_control.__cause__ is None


def test_dashboard_shared_sibling_cleanup_advances_real_fd_once_per_close(
    tmp_path, monkeypatch
) -> None:
    """Dashboard close resumes overlapping assembly owners without stale work."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

    socket.getfqdn()
    baseline = _open_fd_count()

    class SharedDescriptor:
        def __init__(self) -> None:
            self.fd = os.open("/dev/null", os.O_RDONLY)
            self.calls = 0

        def close(self) -> bool:
            self.calls += 1
            if self.calls <= 3:
                return False
            fd, self.fd = self.fd, -1
            os.close(fd)
            return True

    shared_descriptor = SharedDescriptor()
    shared_owner = ResumableRollback()
    shared_owner.own(shared_descriptor)

    def branch_failure(control: BaseException) -> BaseException:
        calls = 0

        def close_controlled() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise control

        controlled = ResumableRollback()
        controlled.own(object(), close_controlled)
        branch = RollbackSlot()
        branch.begin(shared_owner)
        branch.begin(controlled)
        with pytest.raises(type(control)) as caught:
            branch.retry()
        return caught.value

    cause_branch = branch_failure(SystemExit(77))
    context_branch = branch_failure(KeyboardInterrupt())
    assembly_failure = RuntimeError("assembly graph failed")
    assembly_failure.__cause__ = cause_branch
    assembly_failure.__context__ = context_branch

    def fail_host(**kwargs):
        del kwargs
        raise assembly_failure

    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    port = free_loopback_port()
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=port,
        open_database=file_database,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            dashboard.start()
        assert caught.value is assembly_failure
        assert dashboard.state == "closing"
        assert shared_descriptor.calls == 3
        assert dashboard.close() is True
        assert dashboard.close() is True
        assert shared_descriptor.calls == 4
        assert shared_descriptor.fd == -1
        assert _open_fd_count() == baseline

        monkeypatch.undo()
        successor = wiring_module.build_dashboard(
            state_dir=tmp_path / "state",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            clock=FakeClock(),
            port=port,
            open_database=file_database,
        )
        successor.start()
        assert successor.close() is True
        assert _open_fd_count() == baseline
    finally:
        if shared_descriptor.fd >= 0:
            os.close(shared_descriptor.fd)
            shared_descriptor.fd = -1


@pytest.mark.parametrize("control_shape", ("same", "distinct"))
@pytest.mark.parametrize("depth", (1, 2))
def test_shared_prebound_leaf_restores_only_the_real_business_cause(
    control_shape: str, depth: int
) -> None:
    """A completed descendant handle cannot be restored by its aggregate."""
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    business = RuntimeError("original business cause")
    leaf_control = KeyboardInterrupt()
    leaf_control.__cause__ = business
    attempts = {"leaf": 0, "independent": 0}

    def close_leaf() -> None:
        attempts["leaf"] += 1
        if attempts["leaf"] <= 2:
            raise leaf_control

    leaf = ResumableRollback()
    leaf.own(object(), close_leaf)
    with pytest.raises(KeyboardInterrupt) as initial:
        leaf.close()
    assert initial.value is leaf_control
    leaf_handle = leaf_control.__cause__
    assert isinstance(leaf_handle, IncompleteRollback)
    assert leaf_handle.owner is leaf

    dominant = leaf_control if control_shape == "same" else SystemExit(78)
    dominant_original_cause = business if dominant is leaf_control else leaf_control
    if dominant is not leaf_control:
        dominant.__cause__ = dominant_original_cause

    def close_independent() -> None:
        attempts["independent"] += 1
        if attempts["independent"] == 1:
            raise dominant

    independent = ResumableRollback()
    independent.own(object(), close_independent)
    left = RollbackSlot()
    left.begin(leaf)
    right = RollbackSlot()
    right.begin(leaf)
    right.begin(independent)
    if depth == 2:
        nested = RollbackSlot()
        nested.begin(right)
        right = nested
    outer = RollbackSlot()
    outer.begin(left)
    outer.begin(right)

    with pytest.raises(type(dominant)) as interrupted:
        outer.retry()
    assert interrupted.value is dominant
    assert attempts == {"leaf": 2, "independent": 1}
    aggregate = dominant.__cause__
    assert isinstance(aggregate, IncompleteRollback)
    assert aggregate.owner is outer

    assert outer.retry() is True
    assert attempts == {"leaf": 3, "independent": 2}
    assert dominant.__cause__ is dominant_original_cause

    pending = [dominant]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert current is not leaf_handle
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    assert leaf_handle.retry() is True
    assert dominant.__cause__ is dominant_original_cause
    assert attempts == {"leaf": 3, "independent": 2}


@pytest.mark.parametrize("depth", (1, 2))
def test_shared_leaf_contributes_each_executed_cleanup_error_once(
    depth: int,
) -> None:
    """Aggregate errors follow executed leaves, not paths through the forest."""
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    control = KeyboardInterrupt()
    shared_error = RuntimeError("shared leaf")
    repeated_error = RuntimeError("same object from distinct leaves")

    def failing_owner(error: BaseException) -> ResumableRollback:
        owner = ResumableRollback()
        owner.own(object(), lambda: (_ for _ in ()).throw(error))
        return owner

    shared = failing_owner(shared_error)
    left = RollbackSlot()
    left.begin(shared)
    left.begin(failing_owner(repeated_error))
    right = RollbackSlot()
    right.begin(shared)
    right.begin(failing_owner(repeated_error))
    if depth == 2:
        nested = RollbackSlot()
        nested.begin(right)
        right = nested
    outer = RollbackSlot()
    outer.begin(left)
    outer.begin(right)
    outer.begin(failing_owner(control))

    with pytest.raises(KeyboardInterrupt) as caught:
        outer.retry()
    assert caught.value is control
    handle = control.__cause__
    assert isinstance(handle, IncompleteRollback)
    assert handle.errors == (
        control,
        repeated_error,
        shared_error,
        repeated_error,
    )


@pytest.mark.parametrize("failure_kind", ("ordinary", "control"))
@pytest.mark.parametrize("depth", (2, 3))
def test_shared_child_slot_replays_public_result_without_recontributing(
    failure_kind: str, depth: int
) -> None:
    """A shared subtree keeps its result when a sibling revisits it."""
    from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

    child_error: BaseException = (
        RuntimeError("child cleanup")
        if failure_kind == "ordinary"
        else SystemExit(80)
    )
    dominant = KeyboardInterrupt()
    attempts = {"child": 0, "dominant": 0}

    def child_close() -> None:
        attempts["child"] += 1
        if attempts["child"] == 1:
            raise child_error

    def dominant_close() -> None:
        attempts["dominant"] += 1
        if attempts["dominant"] == 1:
            raise dominant

    leaf = ResumableRollback()
    leaf.own(object(), child_close)
    child = RollbackSlot()
    child.begin(leaf)
    left = RollbackSlot()
    left.begin(child)
    right = RollbackSlot()
    right.begin(child)
    if depth == 3:
        nested = RollbackSlot()
        nested.begin(right)
        right = nested
    control_owner = ResumableRollback()
    control_owner.own(object(), dominant_close)
    outer = RollbackSlot()
    outer.begin(left)
    outer.begin(right)
    outer.begin(control_owner)

    with pytest.raises(KeyboardInterrupt) as caught:
        outer.retry()
    assert caught.value is dominant
    assert attempts == {"child": 1, "dominant": 1}
    assert child.errors == (child_error,)
    assert child.process_control is (
        child_error if failure_kind == "control" else None
    )

    assert outer.retry() is True
    assert attempts == {"child": 2, "dominant": 2}
    assert child.errors == ()
    assert child.process_control is None


@pytest.mark.parametrize("first_outcome", ("false", "ordinary"))
@pytest.mark.parametrize("nested", (False, True))
def test_completed_rollback_slot_forgets_failure_before_reuse(
    first_outcome: str, nested: bool
) -> None:
    """A later cleanup cycle reports its own business failure."""
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    first = RuntimeError("first cycle")
    second = RuntimeError("second cycle")
    first_cleanup = RuntimeError("first cleanup")
    attempts = 0

    def close_first() -> bool | None:
        nonlocal attempts
        attempts += 1
        if attempts != 1:
            return None
        if first_outcome == "false":
            return False
        raise first_cleanup

    owner = ResumableRollback()
    owner.own(object(), close_first)
    slot = RollbackSlot()
    slot.begin(owner)
    runner = slot
    if nested:
        parent = RollbackSlot()
        parent.begin(slot)
        runner = parent
    slot.capture_failure(first)
    if nested:
        runner.capture_failure(first)
    assert runner.retry() is False
    assert runner.retry() is True

    control = KeyboardInterrupt()
    next_owner = ResumableRollback()
    next_owner.own(object(), lambda: (_ for _ in ()).throw(control))
    slot.begin(next_owner)
    if nested:
        runner.begin(slot)
    slot.capture_failure(second)
    if nested:
        runner.capture_failure(second)
    with pytest.raises(KeyboardInterrupt) as caught:
        runner.retry()
    assert caught.value is control
    handle = control.__cause__
    assert isinstance(handle, IncompleteRollback)
    assert handle.failure is second


def test_dashboard_does_not_restore_completed_shared_fd_handle(
    tmp_path, monkeypatch
) -> None:
    """Dashboard completion removes a prebound shared FD cleanup handle."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    socket.getfqdn()
    baseline = _open_fd_count()
    business = RuntimeError("original FD business cause")
    control = KeyboardInterrupt()
    control.__cause__ = business

    class SharedDescriptor:
        def __init__(self) -> None:
            self.fd = os.open("/dev/null", os.O_RDONLY)
            self.calls = 0

        def close(self) -> bool:
            self.calls += 1
            if self.calls in (1, 4):
                raise control
            if self.calls in (2, 3):
                return False
            fd, self.fd = self.fd, -1
            os.close(fd)
            return True

    descriptor = SharedDescriptor()
    shared = ResumableRollback()
    shared.own(descriptor)
    shared_child = RollbackSlot()
    shared_child.begin(shared)
    with pytest.raises(KeyboardInterrupt):
        shared_child.retry()
    child_handle = control.__cause__
    assert isinstance(child_handle, IncompleteRollback)
    assert child_handle.owner is shared_child

    def branch_failure(branch_control: BaseException) -> BaseException:
        calls = 0

        def close_controlled() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise branch_control

        controlled = ResumableRollback()
        controlled.own(object(), close_controlled)
        branch = RollbackSlot()
        branch.begin(shared_child)
        branch.begin(controlled)
        with pytest.raises(type(branch_control)) as caught:
            branch.retry()
        return caught.value

    cause_branch = branch_failure(SystemExit(79))
    context_branch = branch_failure(GeneratorExit())
    assembly_failure = RuntimeError("assembly graph failed")
    assembly_failure.__cause__ = cause_branch
    assembly_failure.__context__ = context_branch

    def fail_host(**kwargs):
        del kwargs
        raise assembly_failure

    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    port = free_loopback_port()
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=port,
        open_database=file_database,
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            dashboard.start()
        assert caught.value is control
        cleanup = caught.value.__cause__
        assert isinstance(cleanup, IncompleteRollback)
        assert cleanup.errors == (control,)
        assert shared_child.errors == (control,)
        assert shared_child.process_control is control
        assert dashboard.state == "closing"
        assert descriptor.calls == 4
        assert dashboard.close() is True
        assert dashboard.close() is True
        assert descriptor.calls == 5
        assert descriptor.fd == -1
        assert shared_child.errors == ()
        assert shared_child.process_control is None
        assert control.__cause__ is business
        assert child_handle.retry() is True
        assert control.__cause__ is business
        assert _open_fd_count() == baseline

        monkeypatch.undo()
        successor = wiring_module.build_dashboard(
            state_dir=tmp_path / "state",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            clock=FakeClock(),
            port=port,
            open_database=file_database,
        )
        successor.start()
        assert successor.close() is True
        assert _open_fd_count() == baseline
    finally:
        if descriptor.fd >= 0:
            os.close(descriptor.fd)
            descriptor.fd = -1


@pytest.mark.parametrize("control_shape", ("shared", "distinct"))
@pytest.mark.parametrize("owner_count", (2, 3))
def test_rollback_slot_aggregates_process_control_without_cause_cycles(
    control_shape: str, owner_count: int
) -> None:
    """One slot exposes one typed handle while all owners retain progress."""
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        RollbackSlot,
    )

    shared = KeyboardInterrupt()
    controls: list[BaseException]
    if control_shape == "shared":
        controls = [shared] * owner_count
    else:
        controls = [KeyboardInterrupt(), SystemExit(70), GeneratorExit()][
            :owner_count
        ]
    attempts = [0] * owner_count
    slot = RollbackSlot()

    for index, control in enumerate(controls):
        rollback = ResumableRollback()

        def close(index=index, control=control) -> None:
            attempts[index] += 1
            if attempts[index] <= 2:
                raise control

        resource = object()
        rollback.own(resource, close)
        slot.begin(rollback)

    dominant = controls[-1]
    business = RuntimeError("assembly business failure")
    business.__cause__ = dominant
    slot.capture_failure(business)

    def assert_acyclic(root: BaseException) -> None:
        pending = [root]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            assert id(current) not in seen
            seen.add(id(current))
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)

    with pytest.raises(type(dominant)) as first:
        slot.retry()
    assert first.value is dominant
    aggregate = dominant.__cause__
    assert isinstance(aggregate, IncompleteRollback)
    assert aggregate.owner is slot
    assert aggregate.failure is business
    assert_acyclic(dominant)

    with pytest.raises(type(dominant)) as second:
        slot.retry()
    assert second.value is dominant
    assert dominant.__cause__ is aggregate
    assert_acyclic(dominant)

    assert slot.retry() is True
    assert attempts == [3] * owner_count
    assert dominant.__cause__ is None
    assert slot.retry() is True
    assert attempts == [3] * owner_count
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX managed paths")
def test_build_default_host_rejects_database_fifo_before_open(
    tmp_path, monkeypatch
) -> None:
    """A special file is classified without entering a potentially blocking open."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.managed_state import ManagedPathSecurityError

    state = tmp_path / "state-database-fifo-preflight"
    state.mkdir(mode=0o700)
    database = state / "db.sqlite3"
    os.mkfifo(database)
    real_open = managed_module.os.open

    def refuse_database_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "db.sqlite3" and dir_fd is not None:
            raise AssertionError("managed FIFO reached os.open")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_module.os, "open", refuse_database_open)
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_standalone_host(state)
    assert caught.value.reason == "wrong_type"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX managed paths")
@pytest.mark.parametrize(
    ("role", "kind"),
    (
        ("database", "directory"),
        ("database", "fifo"),
        ("database", "symlink"),
        ("trace_root", "fifo"),
        ("trace_root", "symlink"),
    ),
)
def test_build_default_host_rejects_managed_file_and_trace_root_type_matrix(
    tmp_path, role: str, kind: str
) -> None:
    """The standalone production factory proves DB and trace-root object types."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedPathSecurityError

    baseline = _open_fd_count()
    state = tmp_path / f"state-{role}-{kind}"
    state.mkdir(mode=0o700)
    target = state / ("db.sqlite3" if role == "database" else "traces")
    outside = tmp_path / f"outside-{role}-{kind}"
    if role == "database":
        outside.write_bytes(b"external")
    else:
        outside.mkdir()
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(outside, target_is_directory=outside.is_dir())
    before = outside.stat()
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_standalone_host(state)
    assert caught.value.reason == ("symlink" if kind == "symlink" else "wrong_type")
    after = outside.stat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert _open_fd_count() == baseline


def test_dashboard_resumes_typed_construction_owner_and_restores_cause(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        scripted_factory,
    )
    from agent_alfred.gateway.web.lifecycle import DESCRIPTOR_NAME
    from agent_alfred.resource_rollback import IncompleteRollback

    socket.getfqdn()
    baseline = _open_fd_count()
    sentinel = LookupError("original construction cause")
    failure = ValueError("host assembly failed")
    failure.__cause__ = sentinel

    class PendingResource:
        calls = 0

        def close(self) -> bool:
            self.calls += 1
            return self.calls >= 3

    pending = PendingResource()
    connections: list[sqlite3.Connection] = []
    real_build_host = wiring_module.build_host

    def open_database(state, *, _rollback):
        connection = file_database(state, _rollback=_rollback)
        connections.append(connection)
        return connection

    def fail_host(**kwargs):
        kwargs["_rollback"].own(pending)
        raise failure

    monkeypatch.setattr(wiring_module, "build_host", fail_host)
    state_dir = tmp_path / "state"
    port = free_loopback_port()
    dashboard = wiring_module.build_dashboard(
        state_dir=state_dir,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=port,
        open_database=open_database,
    )
    with pytest.raises(ValueError) as caught:
        dashboard.start()
    assert caught.value is failure
    wrapper = failure.__cause__
    assert isinstance(wrapper, IncompleteRollback)
    assert wrapper.__cause__ is sentinel
    assert pending.calls == 2
    assert dashboard.state == "closing"
    connection = connections[0]
    assert connection.execute("SELECT 1").fetchone() == (1,)
    assert (state_dir / DESCRIPTOR_NAME).exists()

    assert dashboard.close() is True
    assert pending.calls == 3
    assert failure.__cause__ is sentinel
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert not (state_dir / DESCRIPTOR_NAME).exists()
    assert list(state_dir.glob(".*.tmp")) == []

    assert dashboard.close() is True
    assert pending.calls == 3

    # The same public lifecycle reacquires both the process lock and socket.
    monkeypatch.setattr(wiring_module, "build_host", real_build_host)
    successor = wiring_module.build_dashboard(
        state_dir=state_dir,
        factory=scripted_factory(),
        clock=FakeClock(),
        port=port,
        open_database=file_database,
    )
    successor.start()
    assert successor.close() is True
    assert _open_fd_count() == baseline


def test_build_default_host_derives_database_and_trace_from_one_state_lease(
    tmp_path, monkeypatch
) -> None:
    paths = record_managed_state_acquires(monkeypatch)
    state = tmp_path / "state"
    host = build_standalone_host(state)
    host.close()
    assert paths == [state]


def test_build_default_host_attach_failure_closes_every_untransferred_resource(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.runtime.host import RuntimeHost

    baseline = _open_fd_count()
    failure = RuntimeError("injected ownership transfer failure")
    real_attach = RuntimeHost.attach_owned_resources

    def fail_attach(self, conn, state, *, source):
        del self, conn, state, source
        raise failure

    monkeypatch.setattr(RuntimeHost, "attach_owned_resources", fail_attach)
    with pytest.raises(RuntimeError) as caught:
        build_default_host(
            state_dir=tmp_path / "failed",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        )
    assert caught.value is failure
    assert _open_fd_count() == baseline

    monkeypatch.setattr(RuntimeHost, "attach_owned_resources", real_attach)
    host = build_default_host(
        state_dir=tmp_path / "retry",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    assert host.close() is True
    assert _open_fd_count() == baseline


def test_build_default_host_attach_publication_has_one_cleanup_owner(
    tmp_path, monkeypatch
) -> None:
    """Publishing Host ownership cannot leave the outer rollback as a rival."""
    import dis
    import sys

    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        claimed_monitoring_tool,
    )
    from agent_alfred.events import FanOutSink
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.runtime.host import RuntimeHost

    calls = {"host": 0, "connection": 0, "state": 0}
    control = SystemExit("resource owner published before attach returned")

    class State:
        path = tmp_path / "state"

        def close(self) -> None:
            calls["state"] += 1

    class Connection:
        def close(self) -> None:
            calls["connection"] += 1

    state = State()
    connection = Connection()

    def build_runtime_host(**kwargs) -> RuntimeHost:
        return RuntimeHost(
            conn=kwargs["conn"],
            audit_key=AuditKey("test", b"x" * 32),
            factory=kwargs["factory"],
            settings=kwargs["settings"],
            clock=FakeClock(),
            fanout=FanOutSink([], process_instance_id="proc-owned-resources"),
            process_instance_id="proc-owned-resources",
        )

    real_close = RuntimeHost.close

    def count_host_close(self, *args, **kwargs):
        calls["host"] += 1
        return real_close(self, *args, **kwargs)

    monkeypatch.setattr(
        wiring_module.ManagedStateDirectory,
        "acquire",
        classmethod(lambda cls, path, _rollback=None: state),
    )
    monkeypatch.setattr(
        wiring_module, "open_database", lambda lease, _rollback=None: connection
    )
    monkeypatch.setattr(wiring_module, "build_host", build_runtime_host)
    monkeypatch.setattr(RuntimeHost, "close", count_host_close)

    code = RuntimeHost.attach_owned_resources.__code__
    instructions = tuple(dis.get_instructions(code))
    publication = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_owned_resources"
    )
    target = next(
        instruction.offset
        for instruction in instructions[publication + 1 :]
        if instruction.opname == "RETURN_VALUE"
    )
    armed = True

    with claimed_monitoring_tool(
        "host-resource-owner-publication", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code, actual_offset) -> None:
            nonlocal armed
            if armed and actual_code is code and actual_offset == target:
                armed = False
                raise control

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        with pytest.raises(SystemExit) as caught:
            build_default_host(
                state_dir=tmp_path / "state",
                factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            )

    assert armed is False
    assert caught.value is control
    assert calls == {"host": 1, "connection": 1, "state": 1}


@pytest.mark.parametrize(
    ("mutation", "occurrence"),
    (("insert", 1), ("remove", 1), ("insert", 2), ("remove", 2)),
    ids=(
        "connection-after-destination",
        "connection-after-source",
        "state-after-destination",
        "state-after-source",
    ),
)
def test_rollback_handoff_interruption_keeps_one_shared_progress_step(
    mutation: str, occurrence: int
) -> None:
    """Every partial handoff closes resources once in reverse order."""
    import dis
    import sys

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        claimed_monitoring_tool,
    )
    from agent_alfred.resource_rollback import ResumableRollback

    trace: list[str] = []
    state = object()
    connection = object()
    source = ResumableRollback()
    source.own(state, lambda: trace.append("state"))
    source.own(connection, lambda: trace.append("connection"))
    target_owner = ResumableRollback()
    control = SystemExit(f"handoff {mutation} interrupted")

    code = ResumableRollback.transfer_many_to.__code__
    instructions = tuple(dis.get_instructions(code))
    method_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == mutation
    )
    call = next(
        index
        for index, instruction in enumerate(
            instructions[method_load:], method_load
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    target = instructions[call + 1].offset
    remaining = occurrence

    with claimed_monitoring_tool(
        "rollback-owner-handoff", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code, actual_offset) -> None:
            nonlocal remaining
            if actual_code is not code or actual_offset != target:
                return
            remaining -= 1
            if remaining == 0:
                raise control

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        with pytest.raises(SystemExit) as caught:
            source.transfer_many_to(target_owner, (state, connection))

    assert remaining == 0
    assert caught.value is control
    target_owner.close()
    source.close()
    assert trace == ["connection", "state"]


def test_transfer_rejects_a_resource_the_rollback_does_not_own() -> None:
    from agent_alfred.resource_rollback import ResumableRollback

    rollback = ResumableRollback()

    with pytest.raises(RuntimeError, match="resource is not owned"):
        rollback.transfer(object())


def test_rollback_never_closes_an_older_dependency_past_a_newer_refusal() -> None:
    """Strict reverse order pauses at the first cleanup step still pending."""
    from agent_alfred.resource_rollback import ResumableRollback

    trace: list[str] = []
    newer_attempts = 0
    rollback = ResumableRollback()
    rollback.own(object(), lambda: trace.append("older"))

    def close_newer() -> bool:
        nonlocal newer_attempts
        newer_attempts += 1
        trace.append("newer")
        return newer_attempts > 1

    rollback.own(object(), close_newer)

    assert rollback.retry() is False
    assert trace == ["newer"]
    assert rollback.retry() is True
    assert trace == ["newer", "newer", "older"]


def test_rollback_does_not_repeat_a_close_whose_result_store_was_interrupted() -> None:
    """A successful close result is durable before Python can regain control."""
    import dis
    import sys

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        claimed_monitoring_tool,
    )
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback

    calls = 0

    def close() -> None:
        nonlocal calls
        calls += 1

    rollback = ResumableRollback()
    rollback.own(object(), close)
    code = ResumableRollback.retry.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_FAST"
        and instruction.argval in {"closed", "step_complete"}
    )
    control = SystemExit("close returned before progress was stored")
    armed = True
    with claimed_monitoring_tool(
        "rollback-close-result", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code, actual_offset) -> None:
            nonlocal armed
            if armed and actual_code is code and actual_offset == target:
                armed = False
                raise control

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        with pytest.raises(SystemExit) as caught:
            rollback.retry_propagating()

    assert armed is False
    assert caught.value is control
    cleanup = control.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert calls == 1
    assert cleanup.retry() is True
    assert calls == 1


def test_handoff_rejects_a_nonempty_target_before_either_owner_changes() -> None:
    from agent_alfred.resource_rollback import ResumableRollback

    trace: list[str] = []
    older = object()
    state = object()
    connection = object()
    target = ResumableRollback()
    target.own(older, lambda: trace.append("older"))
    source = ResumableRollback()
    source.own(state, lambda: trace.append("state"))
    source.own(connection, lambda: trace.append("connection"))

    with pytest.raises(RuntimeError, match="target rollback must be empty"):
        source.transfer_many_to(target, (state, connection))

    source.close()
    target.close()

    assert trace == ["connection", "state", "older"]


@pytest.mark.parametrize("connection_outcome", (False, RuntimeError("close failed")))
def test_build_default_host_construction_rollback_preserves_original_and_progress(
    tmp_path, monkeypatch, connection_outcome
) -> None:
    from agent_alfred import wiring as wiring_module

    trace: list[str] = []
    construction_failure = ValueError("construction failed")

    class State:
        path = tmp_path / "state"

        def close(self):
            trace.append("state.close")
            return None

    class Connection:
        close_calls = 0

        def close(self):
            self.close_calls += 1
            trace.append("connection.close")
            if self.close_calls == 1:
                if isinstance(connection_outcome, BaseException):
                    raise connection_outcome
                return connection_outcome
            return True

    state = State()
    connection = Connection()
    monkeypatch.setattr(
        wiring_module.ManagedStateDirectory,
        "acquire",
        classmethod(lambda cls, path, _rollback=None: state),
    )
    monkeypatch.setattr(
        wiring_module, "open_database", lambda lease, _rollback=None: connection
    )
    monkeypatch.setattr(
        wiring_module,
        "build_host",
        lambda **kwargs: (_ for _ in ()).throw(construction_failure),
    )

    with pytest.raises(ValueError) as caught:
        build_default_host(
            state_dir=tmp_path / "state",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    assert caught.value is construction_failure
    assert trace == ["connection.close"]
    rollback = caught.value.__cause__
    assert rollback is not None
    assert rollback.retry() is True
    assert trace == ["connection.close", "connection.close", "state.close"]


def test_build_default_host_default_factory_failure_is_inside_rollback_scope(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import wiring as wiring_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    failure = RuntimeError("factory construction failed")
    monkeypatch.setattr(
        wiring_module,
        "OpenCodeGoFactory",
        lambda **kwargs: (_ for _ in ()).throw(failure),
    )

    baseline = _open_fd_count()
    with pytest.raises(RuntimeError) as caught:
        build_default_host(state_dir=tmp_path / "state")
    assert caught.value is failure
    assert _open_fd_count() == baseline


def test_build_default_host_cleanup_process_control_keeps_owner_reachable(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import database as database_module
    from agent_alfred import wiring as wiring_module

    real_connect = sqlite3.connect
    close_attempts = 0
    control = KeyboardInterrupt()
    construction_failure = RuntimeError("factory construction failed")

    class Connection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self) -> None:
            nonlocal close_attempts
            close_attempts += 1
            if close_attempts == 1:
                raise control
            self._connection.close()

    def connect(path, owner, **kwargs):
        owner.publish(Connection(real_connect(path, **kwargs)))

    monkeypatch.setattr(database_module.sqlite3, "connect", connect)
    monkeypatch.setattr(
        wiring_module,
        "OpenCodeGoFactory",
        lambda **kwargs: (_ for _ in ()).throw(construction_failure),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        build_default_host(state_dir=tmp_path / "state")
    assert caught.value is control
    cleanup = caught.value.__cause__
    assert cleanup is not None
    assert cleanup.failure is construction_failure
    assert cleanup.retry() is True
    assert close_attempts == 2
    assert cleanup.retry() is True
    assert close_attempts == 2


def test_dashboard_records_progress_before_cleanup_process_control_propagates(
    tmp_path
) -> None:
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.gateway.web.lifecycle import ProcessLock
    from agent_alfred.managed_state import ManagedPathSecurityError
    from agent_alfred.wiring import build_dashboard

    control = KeyboardInterrupt()
    release_attempts = 0

    class InterruptingLock(ProcessLock):
        def release(self) -> None:
            nonlocal release_attempts
            release_attempts += 1
            if release_attempts == 1:
                raise control
            super().release()

    outside = tmp_path / "outside-traces"
    outside.mkdir()
    unsafe_trace = tmp_path / "unsafe-traces"
    unsafe_trace.symlink_to(outside, target_is_directory=True)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        trace_root=unsafe_trace,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        lock=InterruptingLock,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        dashboard.start()
    assert caught.value is control
    assert isinstance(caught.value.__cause__, ManagedPathSecurityError)
    assert dashboard.state == "closing"
    assert dashboard.close() is True
    assert release_attempts == 2
    assert dashboard.close() is True
    assert release_attempts == 2


def test_dashboard_assembly_cleanup_process_control_keeps_owner_reachable(
    tmp_path,
) -> None:
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.resource_rollback import IncompleteRollback

    socket.getfqdn()
    baseline = _open_fd_count()
    control = KeyboardInterrupt()
    business_failure = RuntimeError("assembly failed")
    close_attempts = 0

    class FailingAssemblySink:
        name = "failing-assembly"
        flush_at_run_end = False

        def bind_projection_boundary(self, boundary) -> None:
            del boundary
            raise business_failure

        def close(self) -> None:
            nonlocal close_attempts
            close_attempts += 1
            if close_attempts == 1:
                raise control

    failing_sink = FailingAssemblySink()
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        extra_sinks=(failing_sink,),
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        dashboard.start()
    assert caught.value is control
    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert cleanup.failure is business_failure
    assert cleanup.__cause__ is business_failure
    assert dashboard.state == "closing"
    assert close_attempts == 1
    assert dashboard.close() is True
    assert close_attempts == 2
    assert caught.value.__cause__ is None
    assert dashboard.close() is True
    assert close_attempts == 2
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("host_outcome", (False, RuntimeError("host close failed")))
def test_build_default_host_attach_failure_retains_host_rollback_owner(
    tmp_path, monkeypatch, host_outcome
) -> None:
    from agent_alfred import wiring as wiring_module

    trace: list[str] = []
    attach_failure = ValueError("attach failed")

    class State:
        path = tmp_path / "state"

        def close(self):
            trace.append("state.close")

    class Connection:
        def close(self):
            trace.append("connection.close")

    class Host:
        close_calls = 0

        def attach_owned_resources(self, conn, state, *, source):
            del conn, state, source
            raise attach_failure

        def close(self):
            self.close_calls += 1
            trace.append("host.close")
            if self.close_calls == 1:
                if isinstance(host_outcome, BaseException):
                    raise host_outcome
                return host_outcome
            return True

    host = Host()
    monkeypatch.setattr(
        wiring_module.ManagedStateDirectory,
        "acquire",
        classmethod(lambda cls, path, _rollback=None: State()),
    )
    monkeypatch.setattr(
        wiring_module, "open_database", lambda lease, _rollback=None: Connection()
    )
    monkeypatch.setattr(wiring_module, "build_host", lambda **kwargs: host)

    with pytest.raises(ValueError) as caught:
        build_default_host(
            state_dir=tmp_path / "state",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    assert caught.value is attach_failure
    assert trace == ["host.close"]
    rollback = caught.value.__cause__
    assert rollback is not None
    assert rollback.retry() is True
    assert host.close_calls == 2
    assert trace == [
        "host.close",
        "host.close",
        "connection.close",
        "state.close",
    ]


@pytest.mark.parametrize("operation", ("fstat", "stat"))
@pytest.mark.parametrize("failure_errno", (errno.EACCES, errno.EPERM))
def test_build_default_host_types_database_identity_permission_denials(
    tmp_path, monkeypatch, operation: str, failure_errno: int
) -> None:
    from agent_alfred import database as database_module
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedPathSecurityError

    state_path = tmp_path / f"state-{operation}-{failure_errno}"
    real_acquire = managed_module.ManagedStateDirectory.acquire.__func__
    real_fstat = managed_module.os.fstat
    real_stat = managed_module.os.stat
    real_connect = database_module.sqlite3.connect

    def deny_fstat(fd):
        opened = real_fstat(fd)
        database = state_path / "db.sqlite3"
        if database.exists():
            named = real_stat(database, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
                raise OSError(failure_errno, os.strerror(failure_errno))
        return opened

    def deny_stat(path, *args, **kwargs):
        if os.fspath(path) in {
            "db.sqlite3",
            os.fspath(state_path / "db.sqlite3"),
        }:
            raise OSError(failure_errno, os.strerror(failure_errno))
        return real_stat(path, *args, **kwargs)

    def acquire_then_inject(cls, path, _rollback=None):
        lease = real_acquire(cls, path)
        if operation == "fstat":
            monkeypatch.setattr(managed_module.os, "fstat", deny_fstat)
        return lease

    def connect_then_inject(path, owner, **kwargs):
        connection = real_connect(path, **kwargs)
        owner.publish(connection)
        monkeypatch.setattr(managed_module.os, "stat", deny_stat)

    monkeypatch.setattr(
        managed_module.ManagedStateDirectory,
        "acquire",
        classmethod(acquire_then_inject),
    )
    if operation == "stat":
        monkeypatch.setattr(database_module.sqlite3, "connect", connect_then_inject)
    baseline = _open_fd_count()
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_default_host(
            state_dir=state_path,
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        )
    assert caught.value.reason == "permission_denied"
    assert caught.value.operation == operation
    assert caught.value.errno == failure_errno
    assert "/bin/ls -ld --" in caught.value.repair_hint
    assert _open_fd_count() == baseline

    monkeypatch.undo()
    host = build_default_host(
        state_dir=tmp_path / f"retry-{operation}-{failure_errno}",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    assert host.close() is True
    assert _open_fd_count() == baseline

def test_child_rollback_retry_never_closes_a_reused_descriptor(
    tmp_path, monkeypatch
) -> None:
    """An uncertain close consumes its descriptor number before the syscall."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.managed_state import ManagedPathSecurityError
    from agent_alfred.resource_rollback import IncompleteRollback

    real_fstat = managed_module._managed_fstat
    real_close = managed_module.os.close
    child_seen = False
    parent_refused = False
    close_failed = False
    closed_number: int | None = None

    def refuse_parent(fd: int, *, role: str, path):
        nonlocal child_seen, parent_refused
        observed = real_fstat(fd, role=role, path=path)
        if role == "SQLite database":
            child_seen = True
        elif child_seen and not parent_refused:
            parent_refused = True
            values = list(observed)
            values[1] += 1
            return os.stat_result(values)
        return observed

    def close_then_raise(fd: int) -> None:
        nonlocal close_failed, closed_number
        real_close(fd)
        if parent_refused and not close_failed:
            close_failed = True
            closed_number = fd
            raise RuntimeError("close result is uncertain")

    monkeypatch.setattr(managed_module, "_managed_fstat", refuse_parent)
    monkeypatch.setattr(managed_module.os, "close", close_then_raise)
    with pytest.raises(ManagedPathSecurityError) as caught:
        build_standalone_host(tmp_path / "state")
    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert closed_number is not None

    monkeypatch.setattr(managed_module.os, "close", real_close)
    replacements: list[int] = []
    while not replacements or replacements[-1] != closed_number:
        replacements.append(os.open(os.devnull, os.O_RDONLY))
    reused = replacements[-1]
    try:
        assert cleanup.retry() is True
        os.fstat(reused)
    finally:
        for descriptor in replacements:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _return_offset(code, *, after: str | None = None, argval: str | None = None):
    """The offset of the RETURN_VALUE a public seam hands its resource back on."""
    import dis

    instructions = tuple(dis.get_instructions(code))
    start = 0
    if after is not None:
        start = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == after
            and (argval is None or instruction.argval == argval)
        )
    return next(
        instruction.offset
        for instruction in instructions[start:]
        if instruction.opname == "RETURN_VALUE"
    )


def test_state_lease_return_edge_keeps_one_reachable_capability_owner(
    tmp_path,
) -> None:
    """``acquire`` hands its lease to an owner before its own return edge."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateDirectory

    baseline = _open_fd_count()
    control = SystemExit("state lease returned to nobody")
    code = ManagedStateDirectory.acquire.__func__.__code__
    offset = _return_offset(code)

    with interrupt_instruction_once(code, offset, control) as state:
        with pytest.raises(SystemExit) as caught:
            build_default_host(
                state_dir=tmp_path / "state",
                factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            )

    assert state[0] is False
    assert caught.value is control
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("interrupt_initial_return", [False, True])
def test_standalone_database_cleans_its_initial_file_return(
    tmp_path, interrupt_initial_return,
) -> None:
    """A failed standalone opener releases its file and anchor descriptors."""
    from agent_alfred import database as database_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateDirectory, ManagedStateLease

    baseline = _open_fd_count()
    state = ManagedStateDirectory.acquire(tmp_path / "state")
    control = SystemExit("initial database file return interrupted")
    try:
        if interrupt_initial_return:
            with interrupt_py_return_once(
                "standalone-database-file-return",
                ManagedStateLease.open_regular.__code__,
                control,
            ) as armed:
                with pytest.raises(SystemExit) as caught:
                    database_module.open_database(state)
            assert armed == [False], "initial file return was not reached"
            assert caught.value is control
        else:
            connection = database_module.open_database(state)
            try:
                assert connection.execute("SELECT 1").fetchone() == (1,)
            finally:
                connection.close()
    finally:
        state.close()
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("interrupt_initial_return", [False, True])
def test_standalone_directory_cleans_its_initial_duplicate_return(
    tmp_path, interrupt_initial_return,
) -> None:
    """An interrupted directory opener releases its initial duplicate."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    state = managed_module.ManagedStateDirectory.acquire(tmp_path / "state")
    control = SystemExit("initial directory duplicate return interrupted")
    try:
        if interrupt_initial_return:
            with interrupt_py_return_once(
                "standalone-directory-duplicate-return",
                managed_module._managed_dup.__code__,
                control,
            ) as armed:
                with pytest.raises(SystemExit) as caught:
                    state.ensure_directory(PurePath("child"))
            assert armed == [False], "initial duplicate return was not reached"
            assert caught.value is control
        else:
            directory = state.ensure_directory(PurePath("child"))
            try:
                directory.verify_identity()
            finally:
                directory.close()
    finally:
        state.close()
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("interrupt_initial_return", [False, True])
def test_standalone_state_cleans_its_initial_parent_return(
    tmp_path, interrupt_initial_return,
) -> None:
    """An interrupted state acquisition releases its initial parent FD."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import OwnedDescriptor

    baseline = _open_fd_count()
    control = SystemExit("initial state parent return interrupted")
    if interrupt_initial_return:
        with interrupt_py_return_once(
            "standalone-state-parent-return",
            OwnedDescriptor.open.__func__.__code__,
            control,
        ) as armed:
            with pytest.raises(SystemExit) as caught:
                ManagedStateDirectory.acquire(tmp_path / "state")
        assert armed == [False], "initial parent return was not reached"
        assert caught.value is control
    else:
        state = ManagedStateDirectory.acquire(tmp_path / "state")
        try:
            state.verify_identity()
        finally:
            state.close()
    assert _open_fd_count() == baseline


def test_database_return_edge_keeps_the_connection_owned(tmp_path) -> None:
    """A connection that never reaches its caller is still closed once."""
    from agent_alfred import database as database_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    closes: list[int] = []
    real_connect = database_module.sqlite3.connect

    class CountingConnection(database_module.sqlite3.Connection):
        def close(self) -> None:
            closes.append(id(self))
            super().close()

    def counted_connect(path, owner, **kwargs):
        owner.publish(real_connect(path, factory=CountingConnection, **kwargs))

    database_module.sqlite3.connect = counted_connect
    control = SystemExit("database connection returned to nobody")
    code = database_module.open_database.__code__
    offset = _return_offset(code)
    try:
        with interrupt_instruction_once(code, offset, control) as state:
            with pytest.raises(SystemExit) as caught:
                build_default_host(
                    state_dir=tmp_path / "state",
                    factory=ScriptedModelFactory(ScriptedModel(["unused"])),
                )
    finally:
        database_module.sqlite3.connect = real_connect

    assert state[0] is False
    assert caught.value is control
    assert len(closes) == 1, "the unreachable connection was closed twice"
    assert _open_fd_count() == baseline


def test_connection_token_owns_the_connection_before_its_return_edge(
    tmp_path, monkeypatch
) -> None:
    """The path-fenced opener registers its result before handing it upward."""
    from agent_alfred import database as database_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import (
        ManagedConnectionToken,
        ManagedStateDirectory,
    )

    created = []

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    def connect(_path, owner, **_kwargs):
        connection = Connection()
        created.append(connection)
        owner.publish(connection)

    monkeypatch.setattr(database_module.sqlite3, "connect", connect)
    state_lease = ManagedStateDirectory.acquire(tmp_path / "state")
    control = SystemExit("connection escaped its path-fenced opener")
    code = ManagedConnectionToken.connect.__code__
    offset = _return_offset(code)
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                database_module.open_database(state_lease)

        assert armed[0] is False
        assert caught.value is control
        assert len(created) == 1
        assert created[0].closed is True
    finally:
        state_lease.close()


def test_connection_token_owns_the_raw_result_before_its_store(
    tmp_path, monkeypatch
) -> None:
    """The connection cannot escape between its opener call and local store."""
    import dis

    from agent_alfred import database as database_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import (
        ManagedConnectionToken,
        ManagedStateDirectory,
    )

    created = []

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    def connect(_path, owner, **_kwargs):
        connection = Connection()
        created.append(connection)
        owner.publish(connection)

    monkeypatch.setattr(database_module.sqlite3, "connect", connect)
    state_lease = ManagedStateDirectory.acquire(tmp_path / "state")
    control = SystemExit("connection escaped before its local store")
    code = ManagedConnectionToken.connect.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "connection"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                database_module.open_database(state_lease)

        assert armed[0] is False
        assert caught.value is control
        assert len(created) == 1
        assert created[0].closed is True
    finally:
        state_lease.close()


def test_owned_descriptor_factory_keeps_its_token_owned_at_return_edge() -> None:
    """Every managed ``open``/``dup`` returns through one pre-owned token."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.resource_rollback import OwnedDescriptor, ResumableRollback

    rollback = ResumableRollback()
    control = SystemExit("owned descriptor returned to nobody")
    code = OwnedDescriptor.open.__func__.__code__
    offset = _return_offset(code)
    minted = -1
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                OwnedDescriptor.open(rollback, os.devnull, os.O_RDONLY)

        assert armed[0] is False
        assert caught.value is control
        owner = rollback._steps[-1].resource  # noqa: SLF001 - ownership assertion
        assert isinstance(owner, OwnedDescriptor)
        minted = owner.fd
        assert minted >= 0
        assert rollback.retry() is True
        with pytest.raises(OSError) as closed:
            os.fstat(minted)
        assert closed.value.errno == errno.EBADF
    finally:
        if minted >= 0:
            try:
                os.close(minted)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_owned_descriptor_refuses_a_python_opener_before_its_py_return() -> None:
    """A caller cannot capture a resource before its Python factory returns."""
    import sys

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        claimed_monitoring_tool,
    )
    from agent_alfred.resource_rollback import OwnedDescriptor, ResumableRollback

    rollback = ResumableRollback()
    minted: list[int] = []

    def open_descriptor() -> int:
        descriptor = os.open(os.devnull, os.O_RDONLY)
        minted.append(descriptor)
        return descriptor

    control = SystemExit("python opener reached its unowned return edge")
    armed = True

    def interrupt(code, offset, result) -> None:
        del offset, result
        nonlocal armed
        if armed and code is open_descriptor.__code__:
            armed = False
            raise control

    try:
        with claimed_monitoring_tool(
            "descriptor-python-opener", local_codes=(open_descriptor.__code__,)
        ) as tool_id:
            sys.monitoring.register_callback(
                tool_id, sys.monitoring.events.PY_RETURN, interrupt
            )
            sys.monitoring.set_local_events(
                tool_id, open_descriptor.__code__, sys.monitoring.events.PY_RETURN
            )
            with pytest.raises(TypeError):
                OwnedDescriptor.open(rollback, open_descriptor, os.O_RDONLY)

        assert armed is True
        assert minted == []
        assert rollback.retry() is True
    finally:
        for descriptor in minted:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_owned_resource_factory_publishes_before_its_py_return() -> None:
    """A Python factory must place its result in the pre-owned holder."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.resource_rollback import OwnedResource, ResumableRollback

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    rollback = ResumableRollback()
    created: list[Resource] = []

    def create(owner=None):
        resource = Resource()
        created.append(resource)
        if owner is not None:
            owner.publish(resource)
        return resource

    failure = SystemExit("resource factory return interrupted")
    with interrupt_py_return_once(
        "owned-resource-factory-return", create.__code__, failure
    ) as armed:
        with pytest.raises(SystemExit) as caught:
            OwnedResource.acquire(rollback, create)

    assert armed == [False]
    assert caught.value is failure
    assert len(created) == 1
    assert rollback.retry() is True
    assert created[0].closed is True


def test_connection_factory_publishes_before_its_py_return(tmp_path) -> None:
    """The path-fenced factory leaves its connection in the caller's owner."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ResumableRollback

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    lease = state.open_regular(
        PurePath("db.sqlite3"),
        access="read_write",
        create=True,
        role="SQLite database",
    )
    token = lease.connection_token()
    rollback = ResumableRollback()
    created: list[Connection] = []

    def connect(_path, owner=None, **_kwargs):
        connection = Connection()
        created.append(connection)
        if owner is not None:
            owner.publish(connection)
        return connection

    failure = SystemExit("connection factory return interrupted")
    try:
        with interrupt_py_return_once(
            "owned-connection-factory-return", connect.__code__, failure
        ) as armed:
            with pytest.raises(SystemExit) as caught:
                token.connect(connect, _rollback=rollback)

        assert armed == [False]
        assert caught.value is failure
        assert len(created) == 1
        assert rollback.retry() is True
        assert created[0].closed is True
    finally:
        rollback.retry()
        lease.close()
        state.close()


def test_owned_descriptor_factory_owns_the_raw_result_before_its_store() -> None:
    """The descriptor cannot escape between its opener call and token store."""
    import dis

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.resource_rollback import OwnedDescriptor, ResumableRollback

    rollback = ResumableRollback()
    control = SystemExit("raw descriptor escaped before its token store")
    code = OwnedDescriptor.open.__func__.__code__
    instructions = tuple(dis.get_instructions(code))
    call_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "CALL"
    )
    offset = instructions[call_index + 1].offset
    minted = -1
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                OwnedDescriptor.open(rollback, os.devnull, os.O_RDONLY)

        assert armed[0] is False
        assert caught.value is control
        owner = rollback._steps[-1].resource  # noqa: SLF001 - ownership assertion
        assert isinstance(owner, OwnedDescriptor)
        minted = owner.fd
        assert minted >= 0
        assert rollback.retry() is True
        with pytest.raises(OSError) as closed:
            os.fstat(minted)
        assert closed.value.errno == errno.EBADF
    finally:
        if minted >= 0:
            try:
                os.close(minted)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_owned_descriptor_close_has_no_interruptible_consume_before_close_gap() -> None:
    """Consuming the token and attempting close expose no Python edge."""
    import dis

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.resource_rollback import OwnedDescriptor, ResumableRollback

    rollback = ResumableRollback()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    owner = OwnedDescriptor(descriptor)
    rollback.own(owner)
    instructions = tuple(dis.get_instructions(OwnedDescriptor.close))
    close_call = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    offset = instructions[close_call + 1].offset
    control = SystemExit("descriptor close returned before rollback progress")
    try:
        with interrupt_instruction_once(
            OwnedDescriptor.close.__code__, offset, control,
        ) as armed:
            with pytest.raises(SystemExit) as caught:
                rollback.retry_propagating()

        assert armed[0] is False
        assert caught.value is control
        assert rollback.retry() is True
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def test_owned_resource_close_does_not_repeat_a_captured_success() -> None:
    """A resource close result is owned before Python regains control."""
    import dis

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.resource_rollback import OwnedResource, ResumableRollback

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    resource = Resource()
    owner: OwnedResource[Resource] = OwnedResource()
    owner.publish(resource)
    rollback = ResumableRollback()
    rollback.own(owner)
    code = OwnedResource.close.__code__
    instructions = tuple(dis.get_instructions(code))
    capture_load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL"
        and instruction.argval == "capture_call_result"
    )
    capture_call = next(
        index
        for index, instruction in enumerate(
            instructions[capture_load:], capture_load
        )
        if instruction.opname in {"CALL", "CALL_KW"}
    )
    offset = instructions[capture_call + 1].offset
    control = SystemExit("resource close returned before local progress")

    with interrupt_instruction_once(code, offset, control) as armed:
        with pytest.raises(SystemExit) as caught:
            rollback.retry_propagating()

    assert armed[0] is False
    assert caught.value is control
    assert resource.close_calls == 1
    assert rollback.retry() is True
    assert resource.close_calls == 1


def test_rollback_step_observes_close_completion_after_its_py_return() -> None:
    """A published close fact prevents replay after the callee return edge."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.resource_rollback import ResumableRollback

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0
            self.closed = False

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True

        def close_completed(self) -> bool:
            return self.closed

    resource = Resource()
    rollback = ResumableRollback()
    rollback.own(resource)
    failure = SystemExit("rollback close return interrupted")

    with interrupt_py_return_once(
        "rollback-close-return", resource.close.__code__, failure
    ) as armed:
        with pytest.raises(SystemExit) as caught:
            rollback.retry_propagating()

    assert armed == [False]
    assert caught.value is failure
    assert resource.close_calls == 1
    assert rollback.retry() is True
    assert resource.close_calls == 1


def test_owned_resource_observes_close_completion_after_its_py_return() -> None:
    """The holder cannot make an already completed close repeat."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.resource_rollback import OwnedResource, ResumableRollback

    class Resource:
        def __init__(self) -> None:
            self.close_calls = 0
            self.closed = False

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True

        def close_completed(self) -> bool:
            return self.closed

    resource = Resource()
    owner: OwnedResource[Resource] = OwnedResource()
    owner.publish(resource)
    rollback = ResumableRollback()
    rollback.own(owner)
    failure = SystemExit("owned close return interrupted")

    with interrupt_py_return_once(
        "owned-close-return", resource.close.__code__, failure
    ) as armed:
        with pytest.raises(SystemExit) as caught:
            rollback.retry_propagating()

    assert armed == [False]
    assert caught.value is failure
    assert resource.close_calls == 1
    assert rollback.retry() is True
    assert resource.close_calls == 1


def test_managed_lease_close_retains_its_fd_across_the_clear_edge(tmp_path) -> None:
    """A lease retry must still reach a descriptor whose first close was cut off."""
    import dis

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedFileLease

    path = tmp_path / "state-file"
    path.write_bytes(b"")
    descriptor = os.open(path, os.O_RDONLY)
    lease = ManagedFileLease(path, descriptor)
    close_code = lease.close.__func__.__code__
    instructions = tuple(dis.get_instructions(close_code))
    cleared = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_fd"
    )
    offset = instructions[cleared + 1].offset
    control = SystemExit("managed lease identity cleared before close")
    try:
        with interrupt_instruction_once(close_code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                lease.close()

        assert armed[0] is False
        assert caught.value is control
        lease.close()
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def test_staging_entry_return_edge_keeps_its_temporary_lease_owned(
    tmp_path, monkeypatch
) -> None:
    """A staging entry is owned before it enters the local lease mapping."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    staging_name = f".staging-{'a' * 32}"
    staging = state.create_directory(
        PurePath(staging_name), role="bundle staging directory"
    )
    meta = staging.path / "meta.json"
    meta.write_bytes(b"{}")
    meta.chmod(0o600)
    opened = []

    def open_regular(*args, _rollback=None, **kwargs):
        del args, kwargs
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_regular", open_regular
    )
    control = SystemExit("staging entry escaped before its mapping store")
    code = managed_module.ManagedDirectoryLease.remove_managed_staging.__code__
    instructions = tuple(dis.get_instructions(code))
    load_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.argval == "_open_regular"
    )
    store_index = next(
        index
        for index, instruction in enumerate(instructions[load_index:], load_index)
        if instruction.opname == "STORE_SUBSCR"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                staging.remove_managed_staging()

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        staging.close()
        state.close()


def test_staging_reclaimer_owns_the_trace_root_before_its_local_store(
    tmp_path, monkeypatch
) -> None:
    """Startup reclamation owns its trace-root lease across the call edge."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    traces = state.path / "traces"
    traces.mkdir(mode=0o700)
    opened = []

    def open_directory(*args, _rollback=None, **kwargs):
        del args, kwargs
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_directory", open_directory
    )
    control = SystemExit("trace root escaped before its local store")
    code = managed_module.ManagedDirectoryLease.reclaim_stale_trace_staging.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "traces"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.reclaim_stale_trace_staging()

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_staging_reclaimer_owns_a_date_lease_before_its_local_store(
    tmp_path, monkeypatch
) -> None:
    """Each scanned date lease has an owner before the loop stores it."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    traces = state.path / "traces"
    traces.mkdir(mode=0o700)
    (traces / "2026-09-05").mkdir(mode=0o700)
    opened = []
    real_open_directory = managed_module.ManagedDirectoryLease._open_directory

    def open_directory(self, *args, role, _rollback=None, **kwargs):
        if role != "trace date directory":
            return real_open_directory(
                self, *args, role=role, _rollback=_rollback, **kwargs
            )
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_directory", open_directory
    )
    control = SystemExit("trace date escaped before its local store")
    code = managed_module.ManagedDirectoryLease.reclaim_stale_trace_staging.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "date"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.reclaim_stale_trace_staging()

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_staging_reclaimer_owns_a_staging_lease_before_its_local_store(
    tmp_path, monkeypatch
) -> None:
    """Each candidate staging lease has an owner before the loop stores it."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    traces = state.path / "traces"
    date = traces / "2026-09-05"
    staging_name = f".staging-{'b' * 32}"
    traces.mkdir(mode=0o700)
    date.mkdir(mode=0o700)
    (date / staging_name).mkdir(mode=0o700)
    opened = []
    real_open_directory = managed_module.ManagedDirectoryLease._open_directory

    def open_directory(self, *args, role, _rollback=None, **kwargs):
        if role != "bundle staging directory":
            return real_open_directory(
                self, *args, role=role, _rollback=_rollback, **kwargs
            )
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_directory", open_directory
    )
    control = SystemExit("staging lease escaped before its local store")
    code = managed_module.ManagedDirectoryLease.reclaim_stale_trace_staging.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "staging"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.reclaim_stale_trace_staging()

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_replace_bytes_owns_its_temporary_lease_before_the_local_store(
    tmp_path, monkeypatch
) -> None:
    """Atomic replacement owns its temporary lease across the open call."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    opened = []

    def open_regular(*args, _rollback=None, **kwargs):
        del args, kwargs
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "open_regular", open_regular
    )
    control = SystemExit("replacement lease escaped before its local store")
    code = managed_module.ManagedDirectoryLease.replace_bytes.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "lease"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.replace_bytes(PurePath("entry.json"), b"{}")

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_unlink_regular_owns_its_lease_before_the_local_store(
    tmp_path, monkeypatch
) -> None:
    """Managed unlink owns the verified file lease across the open call."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    victim = state.path / "victim"
    victim.write_bytes(b"owned")
    victim.chmod(0o600)
    opened = []

    def open_regular(*args, _rollback=None, **kwargs):
        del args, kwargs
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_regular", open_regular
    )
    control = SystemExit("unlink lease escaped before its local store")
    code = managed_module.ManagedDirectoryLease.unlink_regular.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "lease"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.unlink_regular(PurePath("victim"), missing_ok=False)

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_identity_guarded_unlink_owns_its_lease_before_the_local_store(
    tmp_path, monkeypatch
) -> None:
    """Published-target rollback owns its lease across the verified open."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    victim = state.path / "published"
    victim.write_bytes(b"owned")
    victim.chmod(0o600)
    info = victim.stat()
    opened = []

    def open_regular(*args, _rollback=None, **kwargs):
        del args, kwargs
        lease = _CloseRecordingLease()
        opened.append(lease)
        if _rollback is not None:
            _rollback.own(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease, "_open_regular", open_regular
    )
    control = SystemExit("identity-guarded lease escaped before its local store")
    code = managed_module.ManagedDirectoryLease._unlink_regular_identity.__code__
    instructions = tuple(dis.get_instructions(code))
    store_index = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "lease"
    )
    call_index = max(
        index
        for index, instruction in enumerate(instructions[:store_index])
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state._unlink_regular_identity(
                    PurePath("published"), (info.st_dev, info.st_ino)
                )

        assert armed[0] is False
        assert caught.value is control
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        state.close()


def test_create_directory_interruption_before_mkdir_leaves_no_entry(
    tmp_path,
) -> None:
    """An interruption before the mkdir call creates no child entry."""
    import dis

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.managed_state import ManagedDirectoryLease, ManagedStateDirectory

    state = ManagedStateDirectory.acquire(tmp_path / "state")
    created = state.path / "new-child"
    control = SystemExit("interrupted before the mkdir call")
    code = ManagedDirectoryLease.create_directory.__code__
    instructions = tuple(dis.get_instructions(code))
    load_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.argval == "mkdir"
    )
    call_index = next(
        index
        for index, instruction in enumerate(instructions[load_index:], load_index)
        if instruction.opname in {"CALL", "CALL_FUNCTION_EX", "CALL_KW"}
    )
    offset = instructions[call_index + 1].offset
    try:
        with interrupt_instruction_once(code, offset, control) as armed:
            with pytest.raises(SystemExit) as caught:
                state.create_directory(PurePath("new-child"), role="test child")

        assert armed[0] is False
        assert caught.value is control
        assert created.exists() is False
    finally:
        if created.exists():
            created.rmdir()
        state.close()


def test_state_lease_retains_only_its_target_and_direct_parent_descriptors(
    tmp_path,
) -> None:
    """The excluded local-adversary model needs no filesystem-root fence."""
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateDirectory

    parent = tmp_path / "one" / "two" / "three"
    parent.mkdir(parents=True)
    baseline = _open_fd_count()

    state = ManagedStateDirectory.acquire(parent / "state")
    try:
        assert _open_fd_count() == baseline + 2
    finally:
        state.close()

    assert _open_fd_count() == baseline


def test_regular_capability_return_edge_keeps_both_descriptors_owned(
    tmp_path,
) -> None:
    """A minted file capability is owned across the private mint return."""
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    control = SystemExit("minted capability returned to nobody")
    code = managed_module._mint_named_capability.__code__
    offset = _return_offset(code)
    minted = 0
    closed: list[int] = []

    real_open = managed_module.os.open
    real_close = managed_module.os.close

    def count_regular_open(*args, **kwargs):
        nonlocal minted
        fd = real_open(*args, **kwargs)
        minted += 1
        return fd

    def record_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    managed_module.os.open = count_regular_open
    managed_module.os.close = record_close
    try:
        with interrupt_instruction_once(code, offset, control) as state:
            # The state root is the first capability the standalone factory
            # mints, so one armed return covers the deepest private seam.
            with pytest.raises(SystemExit) as caught:
                build_default_host(
                    state_dir=tmp_path / "state",
                    factory=ScriptedModelFactory(ScriptedModel(["unused"])),
                )
    finally:
        managed_module.os.open = real_open
        managed_module.os.close = real_close

    assert state[0] is False
    assert caught.value is control
    assert minted > 0
    assert len(closed) == len(set(closed)), "a descriptor was closed twice"
    assert _open_fd_count() == baseline


def test_trace_directory_return_edge_keeps_its_lease_owned(tmp_path) -> None:
    """``ensure_directory`` hands the trace lease to a reachable owner."""
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateLease

    baseline = _open_fd_count()
    control = SystemExit("trace lease returned to nobody")
    code = ManagedStateLease.ensure_directory.__code__
    offset = _return_offset(code)

    with interrupt_instruction_once(code, offset, control) as state:
        with pytest.raises(SystemExit) as caught:
            build_default_host(
                state_dir=tmp_path / "state",
                factory=ScriptedModelFactory(ScriptedModel(["unused"])),
            )

    assert state[0] is False
    assert caught.value is control
    assert _open_fd_count() == baseline


@contextmanager
def _released_on_exit(module, name: str):
    """Record every aggregate a seam builds and release the unreachable ones.

    A construction owner that loses its aggregate leaks a non-daemon drain
    thread as well as descriptors, so the recorded objects are closed after
    the ownership assertion instead of being left to hang the interpreter.
    """
    real = getattr(module, name)
    built: list = []

    def record(*args, **kwargs):
        instance = real(*args, **kwargs)
        built.append(instance)
        return instance

    setattr(module, name, record)
    try:
        yield built
    finally:
        setattr(module, name, real)
        for instance in built:
            try:
                instance.close()
            except BaseException:  # noqa: BLE001 - best-effort test release
                pass


def test_trace_sink_return_edge_keeps_its_root_owned(tmp_path) -> None:
    """The sink is owned by the construction rollback before it is returned."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    control = SystemExit("trace sink returned to nobody")
    code = wiring_module._trace_sink.__code__
    offset = _return_offset(code, after="LOAD_GLOBAL", argval="RunBundleTraceSink")

    with _released_on_exit(wiring_module, "RunBundleTraceSink"):
        with interrupt_instruction_once(code, offset, control) as state:
            with pytest.raises(SystemExit) as caught:
                build_default_host(
                    state_dir=tmp_path / "state",
                    factory=ScriptedModelFactory(ScriptedModel(["unused"])),
                )

        assert state[0] is False
        assert caught.value is control
        assert _open_fd_count() == baseline


def test_build_host_return_edge_keeps_the_host_owned(tmp_path) -> None:
    """A Host that never reaches its caller is still closed exactly once."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.runtime.host import RuntimeHost

    baseline = _open_fd_count()
    control = SystemExit("host returned to nobody")
    closes = 0
    real_close = RuntimeHost.close

    def count_close(self, *args, **kwargs):
        nonlocal closes
        closes += 1
        return real_close(self, *args, **kwargs)

    code = wiring_module.build_host.__code__
    offset = _return_offset(code, after="STORE_FAST", argval="host")
    RuntimeHost.close = count_close  # type: ignore[method-assign]
    try:
        with _released_on_exit(wiring_module, "RuntimeHost"):
            with interrupt_instruction_once(code, offset, control) as state:
                with pytest.raises(SystemExit) as caught:
                    build_default_host(
                        state_dir=tmp_path / "state",
                        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
                    )

            assert state[0] is False
            assert caught.value is control
            assert closes == 1, "the unreachable Host was not closed exactly once"
            assert _open_fd_count() == baseline
    finally:
        RuntimeHost.close = real_close  # type: ignore[method-assign]


def test_build_default_host_return_edge_keeps_the_host_owned(tmp_path) -> None:
    """A caller-supplied construction owner spans the outermost return edge."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.resource_rollback import ResumableRollback

    baseline = _open_fd_count()
    control = SystemExit("standalone host returned to nobody")
    code = wiring_module.build_default_host.__code__
    offset = _return_offset(code)
    owner = ResumableRollback()

    with _released_on_exit(wiring_module, "RuntimeHost"):
        with interrupt_instruction_once(code, offset, control) as state:
            with pytest.raises(SystemExit) as caught:
                build_default_host(
                    state_dir=tmp_path / "state",
                    factory=ScriptedModelFactory(ScriptedModel(["unused"])),
                    _rollback=owner,
                )

        assert state[0] is False
        assert caught.value is control
        assert owner.retry() is True
        assert _open_fd_count() == baseline


def test_dashboard_assemble_return_edge_keeps_host_and_broker_owned(
    tmp_path,
) -> None:
    """The construction owner is retired only after ``_host``/``_broker``."""
    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )

    socket.getfqdn()
    baseline = _open_fd_count()
    control = SystemExit("assembled runtime returned to nobody")
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    code = dashboard._assemble.__code__
    offset = _return_offset(code)

    with _released_on_exit(wiring_module, "RuntimeHost"):
        with interrupt_instruction_once(code, offset, control) as state:
            with pytest.raises(SystemExit) as caught:
                dashboard.start()

        assert state[0] is False
        assert caught.value is control
        assert dashboard.close() is True
        assert _open_fd_count() == baseline


def test_trace_downgrade_keeps_the_callers_other_resources_owned(
    tmp_path, monkeypatch
) -> None:
    """An ordinary trace failure downgrades the sink, not the whole build.

    Trace initialization is the one construction step that is allowed to fail
    and continue. Its cleanup scope must therefore stop at what it acquired:
    retrying the caller's owner would close the connection and the state lease
    the Host is about to be handed, and mark both complete so its own close
    reports success over resources it never released.
    """
    import errno

    from agent_alfred import managed_state as managed_module
    from agent_alfred.wiring import UnavailableTraceSink

    real_mkdir = managed_module.os.mkdir

    def refuse_traces(name, mode=0o777, *, dir_fd=None):
        if name == "traces":
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_module.os, "mkdir", refuse_traces)
    host = build_default_host(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
    )
    monkeypatch.setattr(managed_module.os, "mkdir", real_mkdir)
    try:
        assert any(
            isinstance(sink, UnavailableTraceSink)
            for sink in host._fanout._sinks  # noqa: SLF001 - degraded sink under test
        )
        # The write connection is the resource the caller still owns.
        session_id = host.create_session()
        assert session_id
        owned = host._owned_resources  # noqa: SLF001 - ownership under test
        assert owned is not None
        assert [step.completed for step in owned._steps] == [False, False]  # noqa: SLF001
    finally:
        assert host.close(timeout=1.0) is True


def test_dashboard_owns_the_assembled_pair_before_construction_retires(
    tmp_path,
) -> None:
    """``settle()`` runs only once this runtime can actually close the pair.

    ``_host_stopped``/``_broker_stopped`` start "stopped", and the close path
    skips whichever is still set. Retiring the construction owner before those
    bits fall would leave a Host -- and its non-daemon trace drain -- with no
    owner on either side.
    """
    import dis

    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.gateway.web.server import DashboardRuntime

    socket.getfqdn()
    baseline = _open_fd_count()
    control = SystemExit("construction owner retired before the runtime owned")
    code = DashboardRuntime._start_locked.__code__
    offset = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_host_stopped"
    )
    dashboard = wiring_module.build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )

    with _released_on_exit(wiring_module, "RuntimeHost"):
        with interrupt_instruction_once(code, offset, control) as state:
            with pytest.raises(SystemExit) as caught:
                dashboard.start()

        assert state[0] is False
        assert caught.value is control
        assert dashboard.close() is True
        assert _open_fd_count() == baseline


def test_trace_sink_closes_before_the_resources_it_was_built_after(
    tmp_path,
) -> None:
    """The handed-over sink is newer than the state lease, so it closes first."""
    from pathlib import PurePath

    from agent_alfred import wiring as wiring_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ResumableRollback
    from agent_alfred.trace import RunBundleTraceSink

    trace: list[str] = []
    real_close = RunBundleTraceSink.close

    def note_close(self, timeout=None):
        trace.append("sink")
        return real_close(self, timeout)

    rollback = ResumableRollback()
    state = ManagedStateDirectory.acquire(tmp_path / "state", _rollback=rollback)
    older = object()
    rollback.own(older, lambda: trace.append("older"))
    RunBundleTraceSink.close = note_close  # type: ignore[method-assign]
    try:
        sink = wiring_module._trace_sink(
            (state, PurePath("traces")),
            FakeClock(),
            "proc-trace-order",
            _rollback=rollback,
        )
        assert rollback.retry() is True
    finally:
        RunBundleTraceSink.close = real_close  # type: ignore[method-assign]
        sink.close()
        state.close()

    assert trace[:2] == ["sink", "older"]


def test_dashboard_state_return_edge_is_owned_before_the_runtime_store(
    tmp_path, monkeypatch
) -> None:
    """Dashboard start owns the state lease before ``_managed_state`` stores it."""
    import dis

    from agent_alfred import managed_state as managed_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.gateway.web.lifecycle import DashboardService
    from agent_alfred.wiring import build_dashboard

    baseline = _open_fd_count()
    captured = []
    real_acquire = managed_module.ManagedStateDirectory.acquire.__func__

    def record_acquire(cls, path, *, _rollback=None):
        lease = real_acquire(cls, path, _rollback=_rollback)
        captured.append(lease)
        return lease

    monkeypatch.setattr(
        managed_module.ManagedStateDirectory,
        "acquire",
        classmethod(record_acquire),
    )
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=file_database,
    )
    control = SystemExit("state lease returned before Dashboard stored it")
    code = DashboardService.start.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == "_managed_state"
    )

    try:
        with interrupt_instruction_once(code, target, control) as state:
            with pytest.raises(SystemExit) as caught:
                dashboard.start()

        assert state[0] is False
        assert caught.value is control
        assert dashboard.close() is True
        assert _open_fd_count() == baseline
    finally:
        for lease in captured:
            lease.close()


def test_dashboard_database_return_edge_is_owned_before_the_runtime_store(
    tmp_path, monkeypatch
) -> None:
    """Dashboard start owns its connection before ``_conn`` can miss the store."""
    import dis

    from agent_alfred import database as database_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.gateway.web.server import DashboardRuntime
    from agent_alfred.wiring import build_dashboard

    # ``ThreadingHTTPServer`` resolves its server name on first construction;
    # macOS keeps that resolver channel process-wide. Warm that unrelated
    # one-time descriptor before measuring the construction rollback.
    socket.getfqdn()
    baseline = _open_fd_count()
    connections = []
    closes: list[int] = []
    real_connect = database_module.sqlite3.connect

    class CountingConnection(database_module.sqlite3.Connection):
        def close(self) -> None:
            closes.append(id(self))
            super().close()

    def counted_connect(path, owner, **kwargs):
        owner.publish(real_connect(path, factory=CountingConnection, **kwargs))

    def capture_database(state, _rollback=None):
        connection = file_database(state, _rollback=_rollback)
        connections.append(connection)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", counted_connect)
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
        open_database=capture_database,
    )
    control = SystemExit("database returned before Dashboard stored it")
    code = DashboardRuntime._start_locked.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_FAST" and instruction.argval == "conn"
    )

    try:
        with interrupt_instruction_once(code, target, control) as state:
            with pytest.raises(SystemExit) as caught:
                dashboard.start()

        assert state[0] is False
        assert caught.value is control
        assert dashboard.close() is True
        assert closes == [id(connections[0])]
        assert _open_fd_count() == baseline
    finally:
        for connection in connections:
            connection.close()


# --- environment layer -------------------------------------------------------
