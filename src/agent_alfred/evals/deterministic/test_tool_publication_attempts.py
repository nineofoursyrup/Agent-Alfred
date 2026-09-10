"""A missing publication identity is not evidence that exclusive create never ran."""

import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.managed_state import ManagedDirectoryLease
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.tools import ToolContext, ToolFailure
from agent_alfred.tools.files import FileTools, digest
from agent_alfred.wiring import build_default_host


@pytest.mark.parametrize(
    "target,expected",
    [
        ("outbox/draft.md", None),
        ("persona/persona.md", digest("old")),
        ("skills/new/SKILL.md", None),
    ],
)
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("seam", ["create_return", "identity_before", "identity_after"])
@pytest.mark.parametrize("error_type", [OSError, SystemExit])
def test_unconfirmed_create_preserves_later_deletion(
    tmp_path,
    monkeypatch,
    target,
    expected,
    automatic,
    seam,
    error_type,
):
    state = tmp_path / "state"
    host = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    path = state / target
    if expected:
        path.parent.mkdir(parents=True)
        path.write_text("old")
        path.chmod(0o600)
    error = error_type("publication interrupted")
    original_open = ManagedDirectoryLease.open_regular
    original_identity = FileTools.record_publication_identity

    def interrupted_open(self, *args, **kwargs):
        lease = original_open(self, *args, **kwargs)
        if kwargs.get("access") == "exclusive_write" and self.path / args[0] == path:
            raise error
        return lease

    def interrupted_identity(self, identity):
        if seam == "identity_after":
            original_identity(self, identity)
        raise error

    if seam == "create_return":
        monkeypatch.setattr(ManagedDirectoryLease, "open_regular", interrupted_open)
    else:
        monkeypatch.setattr(
            FileTools, "record_publication_identity", interrupted_identity
        )
    try:

        def write():
            return host._file_tools.write(
                "operation",
                "draft_message",
                target,
                "new",
                expected,
                ToolContext("run", 1, "call", "cli", float("inf")),
            )

        if error_type is SystemExit:
            with pytest.raises(SystemExit) as raised:
                write()
            assert raised.value is error
        else:
            assert isinstance(write(), ToolFailure)
        assert path.exists()
    finally:
        assert host.close()
    path.unlink()
    monkeypatch.undo()
    restarted = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        if automatic:
            restarted._file_tools.resume_pending()
        else:
            restarted._file_tools.recover("operation")
        assert not path.exists()
        assert restarted._conn.execute(
            "SELECT state FROM file_operations"
        ).fetchall() == [("conflict",)]
        assert (
            restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0]
            == 0
        )
    finally:
        restarted.close()


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("error_type", [OSError, SystemExit])
def test_attempt_receipt_failure_is_conservative(
    tmp_path, monkeypatch, after_commit, error_type
):
    state = tmp_path / "state"
    host = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    original = FileTools.record_publication_attempt
    error = error_type("attempt interrupted")

    def interrupted(self):
        if after_commit:
            original(self)
        raise error

    monkeypatch.setattr(FileTools, "record_publication_attempt", interrupted)
    try:

        def write():
            return host._file_tools.write(
                "operation",
                "draft_message",
                "outbox/draft.md",
                "new",
                None,
                ToolContext("run", 1, "call", "cli", float("inf")),
            )

        if error_type is SystemExit:
            with pytest.raises(SystemExit):
                write()
        else:
            assert isinstance(write(), ToolFailure)
        assert not (state / "outbox/draft.md").exists()
    finally:
        host.close()
    monkeypatch.undo()
    restarted = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        restarted._file_tools.recover("operation")
        assert (state / "outbox/draft.md").exists() is (not after_commit)
        assert restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[
            0
        ] == (0 if after_commit else 1)
    finally:
        restarted.close()


@pytest.mark.parametrize("old_state", ["prepared", "complete"])
def test_legacy_null_identity_does_not_authorize_recreation(
    tmp_path, monkeypatch, old_state
):
    state = tmp_path / "state"
    current = schema.MIGRATIONS
    monkeypatch.setattr(
        schema, "MIGRATIONS", tuple(m for m in current if m.version < 9)
    )
    host = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    host._conn.execute(
        "INSERT INTO file_operations VALUES "
        "('operation','draft_message','outbox/draft.md','new',NULL,?,"
        "'call','run',NULL,'2026-09-10T00:00:00Z',?,'fingerprint',NULL,NULL)",
        (old_state, 'historical receipt' if old_state == 'complete' else None),
    )
    host._conn.commit()
    host.close()
    monkeypatch.setattr(schema, "MIGRATIONS", current)
    restarted = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        assert restarted._conn.execute(
            "SELECT publication_attempted FROM file_operations"
        ).fetchone() == (1,)
        result = restarted._file_tools.recover("operation")
        assert not (state / "outbox/draft.md").exists()
        if old_state == "complete":
            assert result.content[0].text == "historical receipt"
        else:
            assert isinstance(result, ToolFailure)
        assert (
            restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0]
            == 0
        )
    finally:
        restarted.close()


def test_v9_migration_respects_caller_rollback(monkeypatch):
    conn = sqlite3.connect(":memory:")
    current = schema.MIGRATIONS
    monkeypatch.setattr(
        schema, "MIGRATIONS", tuple(m for m in current if m.version < 9)
    )
    schema.migrate(conn)
    monkeypatch.setattr(schema, "MIGRATIONS", current)
    try:
        conn.execute("BEGIN")
        schema.migrate(conn)
        assert conn.in_transaction
        conn.rollback()
        assert "publication_attempted" not in [
            row[1] for row in conn.execute("PRAGMA table_info(file_operations)")
        ]
        schema.migrate(conn)
        assert "publication_attempted" in [
            row[1] for row in conn.execute("PRAGMA table_info(file_operations)")
        ]
    finally:
        conn.close()
