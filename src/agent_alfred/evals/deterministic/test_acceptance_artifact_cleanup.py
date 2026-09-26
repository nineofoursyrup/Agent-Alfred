"""Acceptance readback retains real resources until cleanup is confirmed."""

import os

import pytest

from agent_alfred.evals.acceptance.artifacts import local_business
from agent_alfred.managed_state import ManagedDirectoryLease
from agent_alfred.resource_rollback import IncompleteRollback
from agent_alfred.settings import Settings


def rollback_handle(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, IncompleteRollback):
            return current
        pending.extend(x for x in (current.__cause__, current.__context__) if x)
    pytest.fail("pending cleanup has no reachable IncompleteRollback")


def test_business_readback_cannot_report_success_with_open_persona_descriptor(
    tmp_path,
    monkeypatch,
):
    original_close = ManagedDirectoryLease.close
    held = []
    calls = []
    blocked = True

    def fail_persona_close(lease):
        if lease.path.name == "persona":
            if lease not in held:
                held.append(lease)
            calls.append(lease.fd)
            if blocked:
                raise OSError("synthetic release failure before descriptor close")
        return original_close(lease)

    monkeypatch.setattr(ManagedDirectoryLease, "close", fail_persona_close)
    error = None
    result = None
    try:
        try:
            result = local_business(tmp_path / "state", Settings(persona="fixture"))
        except BaseException as caught:
            error = caught
        descriptors = [lease.fd for lease in held]
        assert descriptors and all(os.fstat(fd).st_ino > 0 for fd in descriptors)
        print(
            {
                "returned_status": result and result["status"],
                "propagated": type(error).__name__ if error else None,
                "real_open_descriptors": len(descriptors),
                "close_attempts": len(calls),
            }
        )
        assert error is not None, "available readback stranded an open descriptor"
        handle = rollback_handle(error)
        blocked = False
        assert handle.retry() is True
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        attempts = len(calls)
        assert handle.retry() is True
        assert len(calls) == attempts
        print(
            {
                "retry_completed": True,
                "real_descriptors_closed": True,
                "repeat_retry_close_calls": len(calls) - attempts,
            }
        )
    finally:
        blocked = False
        for lease in held:
            original_close(lease)


@pytest.mark.parametrize(
    "failure_type", [OSError, ValueError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_read_error_with_pending_cleanup_keeps_original_exception_and_fd_owner(
    tmp_path,
    monkeypatch,
    failure_type,
):
    state = tmp_path / "state"
    persona = state / "persona" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("fixture persona")
    identity = persona.stat().st_ino
    original_read = os.read
    original_close = ManagedDirectoryLease.close
    held = []
    blocked = True
    failure = failure_type("synthetic file read interruption")

    def interrupted_read(fd, amount):
        if os.fstat(fd).st_ino == identity:
            raise failure
        return original_read(fd, amount)

    def interrupted_close(lease):
        if lease.path.name == "persona" and blocked:
            if lease not in held:
                held.append(lease)
            raise OSError("synthetic directory close failure")
        return original_close(lease)

    monkeypatch.setattr(os, "read", interrupted_read)
    monkeypatch.setattr(ManagedDirectoryLease, "close", interrupted_close)
    try:
        with pytest.raises(failure_type) as caught:
            local_business(state, Settings())
        assert caught.value is failure
        handle = rollback_handle(caught.value)
        descriptors = [lease.fd for lease in held]
        assert descriptors and all(os.fstat(fd).st_ino > 0 for fd in descriptors)
        blocked = False
        assert handle.retry() is True
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert handle.retry() is True
    finally:
        blocked = False
        for lease in held:
            original_close(lease)


@pytest.mark.parametrize("boundary", ["state", "database", "file_tools", "calendar"])
def test_control_interruption_at_constructor_return_closes_real_resources_once(
    tmp_path,
    monkeypatch,
    boundary,
):
    from agent_alfred import database
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.tools.calendar import CalendarTools
    from agent_alfred.tools.files import FileTools

    before = _open_fd_count()
    real_connect = database.sqlite3.connect
    connections = []
    closed = []

    class CountingConnection(database.sqlite3.Connection):
        def close(self):
            closed.append(id(self))
            super().close()

    def counted_connect(path, owner, **kwargs):
        conn = real_connect(path, factory=CountingConnection, **kwargs)
        owner.publish(conn)
        connections.append(conn)

    monkeypatch.setattr(database.sqlite3, "connect", counted_connect)
    code = {
        "state": ManagedStateDirectory.acquire.__func__.__code__,
        "database": database.open_database.__code__,
        "file_tools": FileTools.__init__.__code__,
        "calendar": CalendarTools.__init__.__code__,
    }[boundary]
    control = SystemExit("synthetic construction return interruption")
    with interrupt_py_return_once(
        "acceptance-construction-return", code, control
    ) as armed:
        with pytest.raises(SystemExit) as caught:
            local_business(tmp_path / "state", Settings())
    assert armed[0] is False
    assert caught.value is control
    assert closed == [id(conn) for conn in connections]
    assert _open_fd_count() == before


def test_database_open_failure_preserves_a_directory_that_cannot_yet_close(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    state = tmp_path / "state"
    state.mkdir()
    (state / "db.sqlite3").write_bytes(b"corrupt database fixture")
    original_close = ManagedDirectoryLease.close
    held = []
    blocked = True

    def blocked_root_close(lease):
        if lease.path.name == "state" and blocked:
            if lease not in held:
                held.append(lease)
            raise OSError("synthetic root descriptor close failure")
        return original_close(lease)

    monkeypatch.setattr(ManagedDirectoryLease, "close", blocked_root_close)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not a database") as caught:
            local_business(state, Settings())
        handle = rollback_handle(caught.value)
        descriptors = [lease.fd for lease in held]
        assert descriptors and all(os.fstat(fd).st_ino > 0 for fd in descriptors)
        blocked = False
        assert handle.retry() is True
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert handle.retry() is True
    finally:
        blocked = False
        for lease in held:
            original_close(lease)


def test_tool_history_cleanup_failure_is_not_normalized_to_unknown(
    tmp_path,
    monkeypatch,
):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.evals.acceptance.artifacts import tool_projections
    from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
    from agent_alfred.evals.acceptance.runner import run_offline
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.wiring import build_default_host

    batch = controlled_batch()
    batch["cases"] = [c for c in batch["cases"] if c["group"] == "tools"]
    result = run_offline(batch, tmp_path)["results"][0]
    state = tmp_path / batch["batch_id"] / batch["cases"][0]["id"]
    bundle = next((state / "traces").glob("*/*/trace.jsonl")).parent
    host = build_default_host(
        state_dir=state,
        settings=Settings(),
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
        local_tool_allowlist=["draft_message"],
        skill_builtin=tmp_path / "empty",
    )
    host.start()
    original_close = ManagedDirectoryLease.close
    blocked = True
    held = []
    close_attempts = []

    def fail_bundle_close(lease):
        if lease.path == bundle:
            close_attempts.append(lease.fd)
            if lease not in held:
                held.append(lease)
            if blocked:
                raise OSError("synthetic ToolHistory bundle release failure")
        return original_close(lease)

    monkeypatch.setattr(ManagedDirectoryLease, "close", fail_bundle_close)
    failure = None
    exported = None
    try:
        try:
            exported = tool_projections(host, state, result["run_id"])
        except BaseException as error:
            failure = error
        descriptors = [lease.fd for lease in held]
        assert descriptors and all(os.fstat(fd).st_ino > 0 for fd in descriptors)
        print(
            {
                "history_returned_status": exported and exported["status"],
                "propagated": type(failure).__name__ if failure else None,
                "real_open_descriptors": len(descriptors),
            }
        )
        assert failure is not None, "unknown projection swallowed unfinished cleanup"
        handle = rollback_handle(failure)
        blocked = False
        assert handle.retry() is True
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        attempts = len(close_attempts)
        assert handle.retry() is True
        assert len(close_attempts) == attempts
        print(
            {
                "history_retry_completed": True,
                "real_descriptors_closed": True,
                "repeat_retry_close_calls": len(close_attempts) - attempts,
            }
        )
    finally:
        blocked = False
        host.close()
        for lease in held:
            original_close(lease)


def test_cleanup_return_control_keeps_the_pending_directory_recoverable(
    tmp_path,
    monkeypatch,
):
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )
    from agent_alfred.tools.files import FileTools

    original_close = ManagedDirectoryLease.close
    held = []
    blocked = True
    control = KeyboardInterrupt("synthetic cleanup return interruption")

    def blocked_persona_close(lease):
        if lease.path.name == "persona" and blocked:
            if lease not in held:
                held.append(lease)
            raise OSError("synthetic directory close failure")
        return original_close(lease)

    monkeypatch.setattr(ManagedDirectoryLease, "close", blocked_persona_close)
    try:
        with interrupt_py_return_once(
            "acceptance-cleanup-return",
            FileTools.close.__code__,
            control,
        ) as armed:
            with pytest.raises(KeyboardInterrupt) as caught:
                local_business(tmp_path / "state", Settings())
        assert armed[0] is False
        assert caught.value is control
        handle = rollback_handle(caught.value)
        descriptors = [lease.fd for lease in held]
        assert descriptors and all(os.fstat(fd).st_ino > 0 for fd in descriptors)
        blocked = False
        assert handle.retry() is True
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert handle.retry() is True
    finally:
        blocked = False
        for lease in held:
            original_close(lease)
