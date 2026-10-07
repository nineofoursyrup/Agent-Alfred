"""Local adapter fault injection; no real process signals or native launches."""

import errno
import json
import os
import signal
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import (
    local,
    local_persistence,
    local_runtime,
    native,
)
from agent_alfred.resource_rollback import (
    IncompleteRollback,
    OwnedDescriptor,
    ResumableRollback,
)

from ._monitoring_test_helpers import interrupt_py_return_once
from .test_controlled_resource_handoffs import interrupt_caller_after_return
from .test_controlled_transport_boundary import exception_graph
from .test_local_persistence import protected


def _local_constructor(monkeypatch, kind):
    aggregate, backend = {
        "store": (
            local_persistence.LocalExecutionStore,
            local_persistence.OwnerSQLiteDocuments,
        ),
        "witness": (
            local_persistence.LocalExecutionWitness,
            local_persistence.AppendWitnessDocuments,
        ),
    }[kind]
    original = backend.__init__
    captured = []

    def observed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(backend, "__init__", observed)
    return aggregate, backend, captured


def _assert_local_backend_closed(backend):
    if isinstance(backend, local_persistence.AppendWitnessDocuments):
        assert backend.fd is None
    else:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            backend.db.execute("SELECT 1")


@pytest.fixture
def offline_runtime_resources(tmp_path, monkeypatch):
    """Actual constructors and storage; only installation/clock are synthetic."""
    directory = protected(tmp_path)
    config = {
        "ledger_path": "ledger", "witness_path": "witness",
        "decisions_path": "decisions",
    }
    monkeypatch.setattr(local_runtime, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        local_runtime, "verify_installation",
        lambda path: (directory, config, None, None, None),
    )
    monkeypatch.setattr(
        local_runtime, "get_clock_info",
        lambda kind: SimpleNamespace(implementation="mach_absolute_time"),
    )
    monkeypatch.setattr(local_runtime, "MacClock", SimpleNamespace)
    captured, originals, forbidden_calls = [], {}, []

    def observe(backend_type):
        original = backend_type.__init__
        originals[backend_type] = (original, backend_type.close)

        def observed(backend, *args, **kwargs):
            captured.append(backend)
            original(backend, *args, **kwargs)

        monkeypatch.setattr(backend_type, "__init__", observed)

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("offline ownership check reached decision/auth/send")

    observe(local_persistence.OwnerSQLiteDocuments)
    observe(local_persistence.AppendWitnessDocuments)
    monkeypatch.setattr(native, "authorize_owner", forbidden)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "record", forbidden)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "revoke", forbidden)
    monkeypatch.setattr(local_runtime.LocalInstalledRuntime, "send", forbidden)
    resources = SimpleNamespace(
        captured=captured, originals=originals, forbidden_calls=forbidden_calls,
    )
    try:
        yield resources
    finally:
        for backend in captured:
            originals[type(backend)][1](backend)


def _interrupt_cleanup_once(monkeypatch, resources, failure, *, filename=None):
    calls = []
    armed = True

    def replace(backend_type, original):
        def close(backend):
            nonlocal armed
            calls.append(backend.path.name)
            if armed and (filename is None or backend.path.name == filename):
                armed = False
                if failure is not None:
                    raise failure
            return original(backend)

        monkeypatch.setattr(backend_type, "close", close)

    for backend_type, (_, original) in resources.originals.items():
        replace(backend_type, original)
    return calls


def _assert_return_cleanup(resources, first, cleanup_failure, calls):
    assert resources.forbidden_calls == []
    if cleanup_failure is not None:
        recovery = first.__cause__
        assert isinstance(recovery, IncompleteRollback)
        assert recovery.failure is first
        assert cleanup_failure in recovery.errors
        assert recovery.retry() is True
        completed_calls = list(calls)
        assert recovery.retry() is True
        assert calls == completed_calls
    for backend in resources.captured:
        _assert_local_backend_closed(backend)


@pytest.mark.parametrize("dependency", ["store", "witness", "decisions"])
@pytest.mark.parametrize("close_failure_type", [None, OSError, KeyboardInterrupt])
def test_local_runtime_dependency_return_keeps_actual_caller_ownership(
    monkeypatch, offline_runtime_resources, dependency, close_failure_type
):
    resources = offline_runtime_resources
    first = KeyboardInterrupt("dependency PY_RETURN before runtime storage")
    cause = ValueError("original cause remains reachable")
    first.__cause__ = cause
    cleanup_failure = (
        close_failure_type("cleanup before effect") if close_failure_type else None
    )
    calls = _interrupt_cleanup_once(monkeypatch, resources, cleanup_failure)
    constructor = {
        "store": local_persistence.LocalExecutionStore.__init__,
        "witness": local_persistence.LocalExecutionWitness.__init__,
        "decisions": resources.originals[local_persistence.AppendWitnessDocuments][0],
    }[dependency]
    with interrupt_py_return_once(
        "local-runtime-dependency-return", constructor.__code__, first,
        when=lambda frame: dependency != "decisions"
        or frame.f_locals["filename"] == "decisions",
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as caught:
            local_runtime.LocalInstalledRuntime(
                "offline-fixture-only", token=local_runtime._LOCAL_INSTALL
            )
    assert armed == [False]
    assert caught.value is first
    assert cause in exception_graph(first)
    expected_count = {"store": 1, "witness": 2, "decisions": 3}[dependency]
    assert len(resources.captured) == expected_count
    _assert_return_cleanup(resources, first, cleanup_failure, calls)
    assert first.__cause__ is cause


def _owner_cli_arguments(tmp_path, command):
    common = [
        command, "--config", "offline-fixture-only", "--reason", "offline fixture"
    ]
    if command == "revoke":
        return [*common, "--source-ref", "synthetic:unverified"]
    request = tmp_path / "request.json"
    request.write_text('{"offline":"ownership only"}')
    return [
        *common, "--request", str(request), "--decision", "approved",
        "--evidence", "synthetic:unverified",
    ]


@pytest.mark.parametrize("entry", ["install", "owner-decision", "revoke"])
@pytest.mark.parametrize("close_failure_type", [None, OSError, KeyboardInterrupt])
def test_local_install_and_owner_cli_returns_keep_cleanup_reachable(
    tmp_path, monkeypatch, offline_runtime_resources, entry, close_failure_type
):
    resources = offline_runtime_resources
    first = KeyboardInterrupt("installed runtime PY_RETURN before caller storage")
    cleanup_failure = (
        close_failure_type("cleanup before effect") if close_failure_type else None
    )
    calls = _interrupt_cleanup_once(monkeypatch, resources, cleanup_failure)
    if entry == "install":
        factory = local_runtime.LocalInstalledRuntime.__init__

        def action():
            return local_runtime.install_local_runtime("offline-fixture-only")

    else:
        factory = local_runtime.install_local_runtime
        arguments = _owner_cli_arguments(tmp_path, entry)

        def action():
            return local.main(arguments)

    with interrupt_py_return_once(
        "local-install-return", factory.__code__, first
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as caught:
            action()
    assert armed == [False]
    assert caught.value is first
    assert len(resources.captured) == 3
    _assert_return_cleanup(resources, first, cleanup_failure, calls)


@pytest.mark.parametrize("command", ["owner-decision", "revoke"])
@pytest.mark.parametrize("failure_stage", [None, "cleanup", "operation_and_cleanup"])
def test_local_owner_cli_retains_first_failure_and_completed_close_progress(
    tmp_path, monkeypatch, offline_runtime_resources, command, failure_stage
):
    resources = offline_runtime_resources
    first = ValueError("offline_decision_failed")
    cleanup_failure = OSError("witness close temporarily refused")
    calls = _interrupt_cleanup_once(
        monkeypatch, resources, cleanup_failure if failure_stage else None,
        filename="witness",
    )
    operation_calls = []

    def decision(*args, **kwargs):
        operation_calls.append((args, kwargs))
        if failure_stage == "operation_and_cleanup":
            raise first
        return {"source_ref": "synthetic:unverified"}

    monkeypatch.setattr(local_runtime.LocalDecisionControl, "record", decision)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "revoke", decision)
    _owner_cli_arguments(tmp_path, command)
    arguments = SimpleNamespace(
        command=command, config="offline-fixture-only", reason="offline fixture",
        request=tmp_path / "request.json", decision="approved",
        evidence=["synthetic:unverified"], source_ref="synthetic:unverified",
        carry_forward_record=None,
    )
    if failure_stage is None:
        assert local.owner_decision(arguments) == {
            "status": "RECORDED", "source_ref": "synthetic:unverified",
            "execution_started": False, "real_requests": 0,
        }
        assert calls == ["decisions", "witness", "ledger"]
    else:
        with pytest.raises(Exception) as caught:
            local.owner_decision(arguments)
        error = caught.value
        expected = (
            first if failure_stage == "operation_and_cleanup" else cleanup_failure
        )
        assert error is expected
        assert calls == ["decisions", "witness"]
        recovery = error.__cause__
        assert isinstance(recovery, IncompleteRollback)
        assert recovery.retry() is True
        assert calls == ["decisions", "witness", "witness", "ledger"]
        assert recovery.retry() is True
        assert calls == ["decisions", "witness", "witness", "ledger"]
    assert len(operation_calls) == 1
    assert resources.forbidden_calls == []
    for backend in resources.captured:
        _assert_local_backend_closed(backend)


@pytest.mark.parametrize("command", ["owner-decision", "revoke"])
@pytest.mark.parametrize("operation_fails", [False, True])
@pytest.mark.parametrize("sustained", [False, True])
def test_local_main_preserves_unfinished_cleanup_until_caller_retry(
    tmp_path, monkeypatch, capsys, offline_runtime_resources,
    command, operation_fails, sustained,
):
    resources = offline_runtime_resources
    first = ValueError("offline_decision_failed")
    original_cause = RuntimeError("original offline operation cause")
    first.__cause__ = original_cause
    cleanup_failure = OSError("witness close refused before effect")
    refusing = True
    calls, operation_calls = [], []

    def close(backend):
        nonlocal refusing
        calls.append(backend.path.name)
        if backend.path.name == "witness" and refusing:
            refusing = sustained
            raise cleanup_failure
        return resources.originals[type(backend)][1](backend)

    def decision(*args, **kwargs):
        operation_calls.append((args, kwargs))
        if operation_fails:
            raise first
        return {"source_ref": "synthetic:unverified"}

    for backend_type in resources.originals:
        monkeypatch.setattr(backend_type, "close", close)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "record", decision)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "revoke", decision)
    with pytest.raises(Exception) as caught:
        local.main(_owner_cli_arguments(tmp_path, command))
    escaped = caught.value
    assert escaped is (first if operation_fails else cleanup_failure)
    assert capsys.readouterr().out == ""
    assert len(operation_calls) == 1
    assert resources.forbidden_calls == []
    assert calls == ["decisions", "witness"]
    backends = {backend.path.name: backend for backend in resources.captured}
    assert backends["decisions"].fd is None
    assert backends["witness"].fd is not None
    assert backends["ledger"].db.execute("SELECT 1").fetchone() == (1,)
    recovery = next(
        error for error in exception_graph(escaped)
        if isinstance(error, IncompleteRollback)
    )
    assert recovery.failure is escaped
    if operation_fails:
        assert original_cause in exception_graph(escaped)
    expected_calls = ["decisions", "witness"]
    if sustained:
        for _ in range(2):
            assert recovery.retry() is False
            expected_calls.append("witness")
            assert calls == expected_calls
            assert recovery in exception_graph(escaped)
            assert backends["witness"].fd is not None
            assert backends["ledger"].db.execute("SELECT 1").fetchone() == (1,)
        refusing = False
    assert recovery.retry() is True
    expected_calls.extend(["witness", "ledger"])
    assert calls == expected_calls
    assert recovery.retry() is True
    assert calls == expected_calls
    for backend in resources.captured:
        _assert_local_backend_closed(backend)


@pytest.mark.parametrize("command", ["owner-decision", "revoke"])
@pytest.mark.parametrize("result", ["success", "business_failure", "process_control"])
def test_local_main_reports_only_after_cleanup_and_preserves_process_control(
    tmp_path, monkeypatch, capsys, offline_runtime_resources, command, result
):
    resources = offline_runtime_resources
    calls = _interrupt_cleanup_once(monkeypatch, resources, None)
    failure = (
        KeyboardInterrupt("offline process control")
        if result == "process_control" else ValueError("offline_decision_failed")
    )
    operation_calls = []

    def decision(*args, **kwargs):
        operation_calls.append((args, kwargs))
        if result != "success":
            raise failure
        return {"source_ref": "synthetic:unverified"}

    monkeypatch.setattr(local_runtime.LocalDecisionControl, "record", decision)
    monkeypatch.setattr(local_runtime.LocalDecisionControl, "revoke", decision)
    arguments = _owner_cli_arguments(tmp_path, command)
    if result == "process_control":
        with pytest.raises(KeyboardInterrupt) as caught:
            local.main(arguments)
        assert caught.value is failure
        assert capsys.readouterr().out == ""
    else:
        assert local.main(arguments) == (0 if result == "success" else 2)
        report = json.loads(capsys.readouterr().out)
        assert report["real_requests"] == 0
        if result == "success":
            assert report["status"] == "RECORDED"
            assert report["source_ref"] == "synthetic:unverified"
            assert report["execution_started"] is False
        else:
            assert report["status"] == "BLOCKED"
            assert report["blockers"] == ["offline_decision_failed"]
            assert report["run_grant"] is False
    assert len(operation_calls) == 1
    assert resources.forbidden_calls == []
    assert calls == ["decisions", "witness", "ledger"]
    for backend in resources.captured:
        _assert_local_backend_closed(backend)


@pytest.mark.parametrize("kind", ["store", "witness"])
@pytest.mark.parametrize("close_failure_type", [None, OSError, KeyboardInterrupt])
def test_local_aggregate_backend_handoff_keeps_cleanup_and_first_failure(
    tmp_path, monkeypatch, kind, close_failure_type
):
    directory = protected(tmp_path)
    aggregate, backend_type, captured = _local_constructor(monkeypatch, kind)
    first = KeyboardInterrupt("backend returned before aggregate took ownership")
    original_close = backend_type.close
    close_calls = []
    close_failure = (
        close_failure_type("cleanup refused before effect")
        if close_failure_type else None
    )

    def close(backend):
        close_calls.append(backend)
        if len(close_calls) == 1 and close_failure is not None:
            raise close_failure
        original_close(backend)

    monkeypatch.setattr(backend_type, "close", close)
    try:
        interrupt_caller_after_return(
            backend_type.__init__,
            lambda: aggregate(directory, kind, token=local_persistence._LOCAL_STORAGE),
            first,
        )
        assert len(captured) == 1
        assert close_calls == captured
        if close_failure is not None:
            recovery = first.__cause__
            assert isinstance(recovery, IncompleteRollback)
            assert recovery.failure is first
            assert close_failure in recovery.errors
            assert recovery.retry() is True
            calls_after_close = len(close_calls)
            assert recovery.retry() is True
            assert len(close_calls) == calls_after_close
        _assert_local_backend_closed(captured[0])
    finally:
        for backend in captured:
            original_close(backend)


@pytest.mark.parametrize("kind", ["store", "witness"])
def test_local_aggregate_return_remains_owned_by_callers_rollback(
    tmp_path, monkeypatch, kind
):
    directory = protected(tmp_path)
    aggregate, _, captured = _local_constructor(monkeypatch, kind)
    rollback = ResumableRollback()
    first = KeyboardInterrupt("aggregate return before caller storage")
    try:
        interrupt_caller_after_return(
            aggregate.__init__,
            lambda: aggregate(
                directory, kind, token=local_persistence._LOCAL_STORAGE,
                _rollback=rollback,
            ),
            first,
        )
        assert len(captured) == 1
        assert rollback.retry() is True
        _assert_local_backend_closed(captured[0])
        assert rollback.retry() is True
    finally:
        for backend in captured:
            backend.close()


@pytest.mark.parametrize("kind", ["store", "witness"])
def test_local_parent_construction_failure_releases_backend_without_losing_cause(
    tmp_path, monkeypatch, kind
):
    directory = protected(tmp_path)
    aggregate, _, captured = _local_constructor(monkeypatch, kind)
    parent = aggregate.__bases__[0]
    initialize = parent.__init__
    first = ValueError("parent construction failed")
    cause = RuntimeError("original cause")
    first.__cause__ = cause

    def failed(self, backend):
        initialize(self, backend)
        raise first

    monkeypatch.setattr(parent, "__init__", failed)
    try:
        with pytest.raises(ValueError) as caught:
            aggregate(directory, kind, token=local_persistence._LOCAL_STORAGE)
        assert caught.value is first
        assert caught.value.__cause__ is cause
        assert len(captured) == 1
        _assert_local_backend_closed(captured[0])
    finally:
        for backend in captured:
            backend.close()


def test_witness_fstat_failure_releases_the_already_open_descriptor(
    tmp_path, monkeypatch
):
    directory = protected(tmp_path)
    directory.file("witness", create=True)
    original_open, original_fstat = os.open, os.fstat
    descriptors = []

    def opened(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def failed(descriptor):
        raise OSError("offline fstat failure")

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fstat", failed)
    try:
        with pytest.raises(OSError, match="offline fstat failure"):
            local_persistence.AppendWitnessDocuments(
                directory, "witness", token=local_persistence._LOCAL_STORAGE
            )
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            original_fstat(descriptors[0])
    finally:
        for descriptor in descriptors:
            try:
                original_fstat(descriptor)
            except OSError:
                continue
            os.close(descriptor)


def test_failed_witness_construction_retains_incomplete_release_for_retry(
    tmp_path, monkeypatch
):
    directory = protected(tmp_path)
    directory.file("witness", create=True)
    original_close = OwnedDescriptor.close
    pending = []

    def close_once_failed(descriptor):
        if not pending:
            pending.append(descriptor)
            raise OSError("offline release refused before effect")
        return original_close(descriptor)

    def failed(descriptor):
        raise ValueError("original fstat observation failure")

    monkeypatch.setattr(os, "fstat", failed)
    monkeypatch.setattr(OwnedDescriptor, "close", close_once_failed)
    with pytest.raises(ValueError, match="original fstat observation") as caught:
        local_persistence.AppendWitnessDocuments(
            directory, "witness", token=local_persistence._LOCAL_STORAGE
        )
    recovery = caught.value.__cause__
    assert isinstance(recovery, IncompleteRollback)
    assert recovery.failure is caught.value
    assert pending[0].fd >= 0
    assert recovery.retry() is True
    assert pending[0].fd == -1
    assert recovery.retry() is True


def test_native_group_kill_return_edge_is_not_repeated(monkeypatch):
    slot = object.__new__(native.NativeRunnerSlot)
    slot.process = SimpleNamespace(
        stdin=None,
        stdout=None,
        _child_created=True,
        pid=987654,
        wait=lambda timeout: 0,
    )
    slot._group_closed = False
    slot._lease_fd = -1
    effects = []

    def killed(*args):
        effects.append(args)

    monkeypatch.setattr(native.os, "killpg", killed)
    interrupt_caller_after_return(
        killed, slot._close_resources, KeyboardInterrupt("offline kill return edge")
    )
    slot._close_resources()
    assert len(effects) == 1


@pytest.mark.parametrize("interrupt_absence", [False, True])
def test_native_group_absence_has_one_captured_or_unresolved_release(
    monkeypatch, interrupt_absence
):
    slot = object.__new__(native.NativeRunnerSlot)
    slot.process = SimpleNamespace(
        stdin=None, stdout=None, _child_created=True, pid=987654,
        wait=lambda timeout: 0,
    )
    slot._group_closed = False
    slot._lease_fd = -1
    calls = []

    def absent(*args):
        calls.append(args)
        raise ProcessLookupError("offline group is absent")

    original = native.capture_call_result

    def interrupted(receiver, attribute, operation):
        if operation is sys.exception:
            raise KeyboardInterrupt("offline before absence was captured")
        return original(receiver, attribute, operation)

    monkeypatch.setattr(native.os, "killpg", absent)
    if interrupt_absence:
        monkeypatch.setattr(native, "capture_call_result", interrupted)
        with pytest.raises(KeyboardInterrupt):
            slot._close_resources()
        with pytest.raises(ValueError, match="native_group_cleanup_outcome_unknown"):
            slot._close_resources()
    else:
        slot._close_resources()
        assert isinstance(slot._group_closed, ProcessLookupError)
        slot._close_resources()
    assert len(calls) == 1


def test_native_zombie_permission_error_requires_reap_and_group_absence(monkeypatch):
    slot = object.__new__(native.NativeRunnerSlot)
    calls = []
    reaped = []
    first = PermissionError(errno.EPERM, "offline zombie group")
    slot.process = SimpleNamespace(
        stdin=None, stdout=None, _child_created=True, pid=987654,
        wait=lambda timeout: reaped.append(timeout) or 0,
    )
    slot._group_closed = False
    slot._lease_fd = -1

    def signal_group(pid, number):
        calls.append((pid, number))
        if number == signal.SIGKILL:
            raise first
        assert reaped, "absence may only be checked after reaping the own child"
        raise ProcessLookupError(errno.ESRCH, "offline original group absent")

    monkeypatch.setattr(native.os, "killpg", signal_group)
    slot._close_resources()
    slot._close_resources()
    assert calls == [(987654, signal.SIGKILL), (987654, 0)]
    assert slot._group_signal_failure is first
    assert isinstance(slot._group_closed, ProcessLookupError)


@pytest.mark.parametrize("probe_denied", [False, True])
def test_native_permission_error_with_remaining_group_cannot_complete(
    monkeypatch, probe_denied
):
    slot = object.__new__(native.NativeRunnerSlot)
    calls = []
    first = PermissionError(errno.EPERM, "offline signal refused")
    slot.process = SimpleNamespace(
        stdin=None, stdout=None, _child_created=True, pid=987654,
        wait=lambda timeout: 0,
    )
    slot._group_closed = False
    slot._lease_fd = -1

    def signal_group(pid, number):
        calls.append((pid, number))
        if number == signal.SIGKILL:
            raise first
        if probe_denied:
            raise PermissionError(errno.EPERM, "offline group query refused")

    monkeypatch.setattr(native.os, "killpg", signal_group)
    for _ in range(2):
        with pytest.raises(PermissionError) as failure:
            slot._close_resources()
        assert failure.value is first
        assert slot._group_closed is False
    assert calls == [(987654, signal.SIGKILL), (987654, 0), (987654, 0)]


def test_native_signal_interruption_never_retries_destructive_effect(monkeypatch):
    slot = object.__new__(native.NativeRunnerSlot)
    calls = []
    slot.process = SimpleNamespace(
        stdin=None, stdout=None, _child_created=True, pid=987654,
        wait=lambda timeout: pytest.fail("unknown signal cannot certify cleanup"),
    )
    slot._group_closed = False
    slot._lease_fd = -1

    def interrupted(pid, number):
        calls.append((pid, number))
        raise KeyboardInterrupt("offline signal outcome unknown")

    monkeypatch.setattr(native.os, "killpg", interrupted)
    with pytest.raises(KeyboardInterrupt):
        slot._close_resources()
    with pytest.raises(ValueError, match="native_group_cleanup_outcome_unknown"):
        slot._close_resources()
    assert calls == [(987654, signal.SIGKILL)]


def test_native_group_query_retries_preserve_all_earlier_error_edges(monkeypatch):
    slot = object.__new__(native.NativeRunnerSlot)
    cause, context = RuntimeError("earlier cause"), RuntimeError("earlier context")
    first = PermissionError(errno.EPERM, "first signal error")
    first.__cause__, first.__context__ = cause, context
    queries = [PermissionError(errno.EPERM, "query one"),
               PermissionError(errno.EPERM, "query two")]
    slot.process = SimpleNamespace(
        stdin=None, stdout=None, _child_created=True, pid=987654,
        wait=lambda timeout: 0,
    )
    slot._group_closed = False
    slot._lease_fd = -1
    pending = iter(queries)

    def refused(pid, number):
        if number == signal.SIGKILL:
            raise first
        raise next(pending)

    monkeypatch.setattr(native.os, "killpg", refused)
    for index in range(2):
        with pytest.raises(PermissionError) as failure:
            slot._close_resources()
        assert failure.value is first
        graph = list(exception_graph(first))
        assert cause in graph and context in graph
        assert all(query in graph for query in queries[:index + 1])
