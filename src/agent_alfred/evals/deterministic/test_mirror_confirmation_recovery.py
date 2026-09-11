"""Confirmation actions settle independently of current file synchronization."""

import sqlite3

import pytest

from agent_alfred.evals.deterministic.test_memory_mirrors import (
    CONTEXT,
    build_standalone_host,
    save,
)
from agent_alfred.managed_state import ManagedDirectoryLease


@pytest.mark.parametrize("name", ("facts", "episodes"))
@pytest.mark.parametrize("boundary", ("directory", "open", "intent"))
@pytest.mark.parametrize("restart", (False, True))
def test_interrupted_confirmation_settles_without_reusing_authorization(
    tmp_path, monkeypatch, name, boundary, restart
):
    root = tmp_path / "state"
    host = build_standalone_host(root)
    try:
        save(host.memory_service)
        mirrors = host.memory_service.mirrors
        path = root / "memory" / f"{name}.md"
        path.write_text("External original")
        observation = mirrors.status(name)["confirmation"]
        method = "ensure_directory" if boundary == "directory" else "open_regular"
        original = getattr(ManagedDirectoryLease, method)
        fired = []

        def interrupted(self, relative, **kwargs):
            hit = (
                (boundary == "directory" and str(relative) == "memory")
                or (boundary == "open" and relative.name == path.name)
                or (boundary == "intent" and relative.name.startswith(f".{name}.md."))
            )
            if hit and not fired:
                fired.append(True)
                raise KeyboardInterrupt("confirmation handoff")
            return original(self, relative, **kwargs)

        monkeypatch.setattr(ManagedDirectoryLease, method, interrupted)
        with pytest.raises(KeyboardInterrupt):
            mirrors.confirm(name, observation, "confirm", CONTEXT)
        monkeypatch.undo()
        assert fired
        # Both an external successor and a newer Store generation must not
        # become authorized by the old operation on retry/restart.
        path.write_text("External successor")
        save(host.memory_service, "later", "Later Store generation")
        if restart:
            assert host.close()
            host = build_standalone_host(root)
            host.recover()
        mirrors = host.memory_service.mirrors
        receipt = mirrors.confirm(name, observation, "confirm", CONTEXT)
        assert receipt["status"] == "interrupted"
        assert mirrors.get_operation("confirm")["receipt"] == receipt
        assert path.read_text() == "External successor"
        assert mirrors.confirm(name, {}, "confirm", CONTEXT) == {
            "error": {"code": "operation_mismatch"}
        }
        fresh = mirrors.status(name)["confirmation"]
        assert (
            mirrors.confirm(name, fresh, "new-confirm", CONTEXT)["status"]
            == "regenerated"
        )
        assert mirrors.confirm(name, observation, "confirm", CONTEXT) == receipt
    finally:
        monkeypatch.undo()
        assert host.close()


@pytest.mark.parametrize("name", ("facts", "episodes"))
@pytest.mark.parametrize("persistent", (False, True))
def test_failed_confirmation_remains_terminal_across_sync_and_restart(
    tmp_path, monkeypatch, name, persistent
):
    root = tmp_path / "state"
    host = build_standalone_host(root)
    try:
        saved = save(host.memory_service)
        mirrors = host.memory_service.mirrors
        path = root / "memory" / f"{name}.md"
        path.write_text("External edit")
        observation = mirrors.status(name)["confirmation"]
        original_replace = ManagedDirectoryLease.replace_bytes
        original_open = ManagedDirectoryLease.open_regular
        replaced, failed = [], []

        def replace(self, relative, payload, **kwargs):
            result = original_replace(self, relative, payload, **kwargs)
            if relative.name == path.name:
                replaced.append(True)
            return result

        def opened(self, relative, **kwargs):
            if relative.name == path.name and replaced and (persistent or not failed):
                failed.append(True)
                raise OSError("verification unavailable")
            return original_open(self, relative, **kwargs)

        monkeypatch.setattr(ManagedDirectoryLease, "replace_bytes", replace)
        monkeypatch.setattr(ManagedDirectoryLease, "open_regular", opened)
        receipt = mirrors.confirm(name, observation, "confirm", CONTEXT)
        assert receipt == {"error": {"code": "mirror_sync_failed"}}
        assert mirrors.get_operation("confirm")["receipt"] == receipt
        monkeypatch.undo()
        save(host.memory_service, "successor", "New generation")
        assert host.close()
        host = build_standalone_host(root)
        host.recover()
        mirrors = host.memory_service.mirrors
        assert mirrors.preview(name)["ready"]
        assert mirrors.confirm(name, observation, "confirm", CONTEXT) == receipt
        assert mirrors.get_operation("confirm")["receipt"] == receipt
        assert host.memory_service.get_operation("save") == saved
    finally:
        monkeypatch.undo()
        assert host.close()


def test_terminal_write_failure_keeps_durable_pending_until_recovery(
    tmp_path, monkeypatch
):
    host = build_standalone_host(tmp_path / "state")
    try:
        save(host.memory_service)
        mirrors = host.memory_service.mirrors
        path = tmp_path / "state/memory/facts.md"
        path.write_text("External edit")
        observation = mirrors.status("facts")["confirmation"]
        conn = host.memory_service._db._conn

        def deny_terminal(action, table, *args):
            if action == sqlite3.SQLITE_UPDATE and table == "memory_mirror_actions":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def interrupted(*args, **kwargs):
            conn.set_authorizer(deny_terminal)
            raise OSError("before intent")

        monkeypatch.setattr(ManagedDirectoryLease, "ensure_directory", interrupted)
        receipt = mirrors.confirm("facts", observation, "confirm", CONTEXT)
        assert receipt["status"] == "pending"
        assert mirrors.get_operation("confirm")["receipt"] == receipt
        monkeypatch.undo()
        conn.set_authorizer(None)
        assert (
            mirrors.confirm("facts", observation, "confirm", CONTEXT)["status"]
            == "interrupted"
        )
        assert path.read_text() == "External edit"
    finally:
        host.memory_service._db._conn.set_authorizer(None)
        monkeypatch.undo()
        assert host.close()


@pytest.mark.parametrize("name", ("facts", "episodes"))
def test_process_exit_after_pending_commit_settles_on_restart(tmp_path, name):
    import subprocess
    import sys

    root = tmp_path / "state"
    code = """
import os, sys
from pathlib import Path
from agent_alfred.evals.deterministic.test_memory_mirrors import (
    build_standalone_host, save, CONTEXT,
)
from agent_alfred.managed_state import ManagedDirectoryLease
host=build_standalone_host(Path(sys.argv[1]))
save(host.memory_service)
name=sys.argv[2]
path=Path(sys.argv[1])/'memory'/f'{name}.md'
path.write_text('External contents')
mirrors=host.memory_service.mirrors
proof=mirrors.status(name)['confirmation']
def stopped(*args, **kwargs):
    os._exit(73)
ManagedDirectoryLease.ensure_directory=stopped
mirrors.confirm(name,proof,'confirm',CONTEXT)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(root), name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 73, child.stderr
    host = build_standalone_host(root)
    try:
        mirrors = host.memory_service.mirrors
        assert mirrors.get_operation("confirm")["receipt"]["status"] == "pending"
        host.recover()
        receipt = mirrors.get_operation("confirm")["receipt"]
        assert receipt == {"status": "interrupted", "operation_id": "confirm"}
        assert (root / "memory" / f"{name}.md").read_text() == "External contents"
        assert not mirrors.preview(name)["ready"]
    finally:
        assert host.close()
