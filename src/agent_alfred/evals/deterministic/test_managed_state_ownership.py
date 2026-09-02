"""Managed-state capability and construction ownership through public seams."""

from __future__ import annotations

import errno
import os
import socket
import sqlite3
import stat

import pytest

from agent_alfred.evals.deterministic._managed_state_ownership_test_helpers import (
    build_standalone_host,
)
from agent_alfred.evals.deterministic._web_startup_test_helpers import (
    record_managed_state_acquires,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host


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

    def connect_then_drift(*args, **kwargs):
        nonlocal connected, opened_connection
        opened_connection = real_connect(*args, **kwargs)
        connected = True
        return opened_connection

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
            values[0] = stat.S_IFREG | 0o600
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


def test_build_default_host_closes_first_trace_dup_when_fence_derivation_fails(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    failure = RuntimeError("fence derivation failed")
    real_fence = managed_module.ManagedDirectoryLease._fence_for_child

    def fail_state_fence(self, *, role, path):
        if path.name == "state":
            raise failure
        return real_fence(self, role=role, path=path)

    monkeypatch.setattr(
        managed_module.ManagedDirectoryLease,
        "_fence_for_child",
        fail_state_fence,
    )
    host = build_standalone_host(tmp_path / "state")
    assert host.close() is True
    assert _open_fd_count() == baseline


def test_nested_directory_fence_handoff_closes_both_sides_after_close_failure(
    tmp_path, monkeypatch
) -> None:
    """The atomic old-to-new fence handoff retains both owners on failure."""
    from pathlib import PurePath

    from agent_alfred import managed_state as managed_module
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count

    baseline = _open_fd_count()
    failure = SystemExit(76)
    real_close = managed_module._LexicalAncestorFence.close
    close_calls = 0

    def interrupt_first_close(fence):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise failure
        return real_close(fence)

    monkeypatch.setattr(
        managed_module._LexicalAncestorFence, "close", interrupt_first_close
    )
    state = managed_module.ManagedStateDirectory.acquire(tmp_path / "state")
    try:
        with pytest.raises(SystemExit) as caught:
            state.ensure_directory(PurePath("first/second"))
        assert caught.value is failure
        assert close_calls >= 3
    finally:
        state.close()
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
        open_database=lambda state: (_ for _ in ()).throw(control),
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        file_database,
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
        file_database,
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

    def open_database(state):
        connection = file_database(state)
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

    def fail_attach(self, conn, state):
        del self, conn, state
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
        classmethod(lambda cls, path: state),
    )
    monkeypatch.setattr(wiring_module, "open_database", lambda lease: connection)
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
    assert trace == ["connection.close", "state.close"]
    rollback = caught.value.__cause__
    assert rollback is not None
    assert rollback.retry() is True
    assert trace == ["connection.close", "state.close", "connection.close"]


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

    monkeypatch.setattr(
        database_module.sqlite3,
        "connect",
        lambda *args, **kwargs: Connection(real_connect(*args, **kwargs)),
    )
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


def test_dashboard_refuses_a_replaced_state_ancestor_and_releases_everything(
    tmp_path, monkeypatch
) -> None:
    from agent_alfred import database as database_module
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.managed_state import ManagedPathSecurityError
    from agent_alfred.wiring import build_dashboard

    socket.getfqdn()
    baseline = _open_fd_count()
    container = tmp_path / "container"
    container.mkdir(mode=0o700)
    state_path = container / "state"
    replacement = tmp_path / "container-replacement"
    replacement_state = replacement / "state"
    replacement_state.mkdir(parents=True, mode=0o700)
    replacement_database = replacement_state / "db.sqlite3"
    seed = sqlite3.connect(replacement_database)
    seed.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    seed.execute("INSERT INTO sentinel VALUES ('untouched')")
    seed.commit()
    seed.close()
    before = replacement_database.read_bytes()
    displaced = tmp_path / "container-displaced"
    real_connect = sqlite3.connect

    def replace_ancestor_then_connect(path, **kwargs):
        container.rename(displaced)
        replacement.rename(container)
        return real_connect(path, **kwargs)

    monkeypatch.setattr(
        database_module.sqlite3, "connect", replace_ancestor_then_connect
    )
    dashboard = build_dashboard(
        state_dir=state_path,
        factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        clock=FakeClock(),
        port=free_loopback_port(),
    )
    with pytest.raises(ManagedPathSecurityError) as caught:
        dashboard.start()
    assert caught.value.reason == "identity_changed"
    assert (state_path / "db.sqlite3").read_bytes() == before
    assert dashboard.state == "failed"
    assert dashboard.close() is True
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

        def attach_owned_resources(self, conn, state):
            del conn, state
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
        classmethod(lambda cls, path: State()),
    )
    monkeypatch.setattr(wiring_module, "open_database", lambda lease: Connection())
    monkeypatch.setattr(wiring_module, "build_host", lambda **kwargs: host)

    with pytest.raises(ValueError) as caught:
        build_default_host(
            state_dir=tmp_path / "state",
            factory=ScriptedModelFactory(ScriptedModel(["unused"])),
        )
    assert caught.value is attach_failure
    assert trace == ["host.close", "connection.close", "state.close"]
    rollback = caught.value.__cause__
    assert rollback is not None
    assert rollback.retry() is True
    assert host.close_calls == 2
    assert trace == ["host.close", "connection.close", "state.close", "host.close"]


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

    def acquire_then_inject(cls, path):
        lease = real_acquire(cls, path)
        if operation == "fstat":
            monkeypatch.setattr(managed_module.os, "fstat", deny_fstat)
        return lease

    def connect_then_inject(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        monkeypatch.setattr(managed_module.os, "stat", deny_stat)
        return connection

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
            values[0] = stat.S_IFREG | 0o600
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


# --- environment layer -------------------------------------------------------
