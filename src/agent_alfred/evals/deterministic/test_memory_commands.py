"""Shared commands commit records and safe replay receipts together."""

import sqlite3
import threading
from datetime import datetime, timezone

from agent_alfred import schema
from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.commands import CommandContext, MemoryCommandService
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.runtime.recording import RecordingStore


def service(conn):
    schema.migrate(conn)
    return MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
    )


def test_save_receipt_replays_after_restart_without_recreating_deleted_fact(tmp_path):
    path = tmp_path / "memory.db"
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    command = {
        "operation_id": "save-one",
        "kind": "semantic",
        "action": "save",
        "payload": {"subject": "me", "fact": "I prefer coriander"},
    }
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        saved = memory.execute(command, context)
        assert saved["status"] == "saved"
        assert memory.get("semantic", saved["memory_id"]).fact == "I prefer coriander"
        deleted = memory.execute(
            {
                "operation_id": "delete-one",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            context,
        )
        assert deleted["status"] == "deleted"
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        assert memory.execute(command, context) == saved
        assert memory.get("semantic", saved["memory_id"]) is None
        assert memory.get_operation("save-one") == saved
        assert "coriander" not in str(memory.get_operation("delete-one"))


def test_receipt_storage_failure_rolls_back_record_and_fts(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    conn.execute(
        "CREATE TRIGGER failed_receipt BEFORE INSERT ON memory_operations "
        "BEGIN SELECT RAISE(ABORT, 'private-storage-marker'); END"
    )
    result = memory.execute(
        {
            "operation_id": "fails",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "secret-subject", "fact": "private-fact-marker"},
        },
        context,
    )
    assert result == {"error": {"code": "storage_write_failed"}}
    assert memory.get_operation("fails") is None
    from agent_alfred.memory.types import FactQuery

    with memory.reading_stores() as (facts, _):
        assert not facts.search(FactQuery(text="private"))
    conn.close()


def test_replay_mismatch_and_old_key_never_execute(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    command = {
        "operation_id": "fixed",
        "kind": "semantic",
        "action": "save",
        "payload": {"subject": "me", "fact": "tea"},
    }
    saved = memory.execute(command, context)
    assert memory.execute(
        {**command, "payload": {"subject": "me", "fact": "coffee"}}, context
    ) == {"error": {"code": "operation_mismatch"}}
    rotated = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("new-key", b"y" * 32),
        clock=lambda: datetime.now(timezone.utc),
    )
    assert rotated.execute(command, context) == {
        "error": {"code": "operation_unverifiable"}
    }
    assert rotated.get_operation("fixed") == saved
    conn.close()


def test_invalid_episode_edit_returns_safe_error_and_preserves_record(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    saved = memory.execute(
        {
            "operation_id": "episode",
            "kind": "episodic",
            "action": "save",
            "payload": {
                "summary": "travel",
                "occurred_at": "2026-09-09T12:00:00+00:00",
                "occurred_until": None,
            },
        },
        context,
    )
    result = memory.execute(
        {
            "operation_id": "bad-edit",
            "kind": "episodic",
            "action": "update",
            "payload": {
                "id": saved["memory_id"],
                "occurred_until": "2026-09-08T12:00:00+00:00",
            },
            "expected_version": 1,
        },
        context,
    )
    assert result == {"error": {"code": "invalid_input"}}
    assert memory.get("episodic", saved["memory_id"]).record_version == 1
    assert memory.get_operation("bad-edit") is None
    conn.close()


def test_explicit_new_fact_has_known_empty_sources_not_legacy_unknown(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    saved = memory.execute(
        {
            "operation_id": "manual",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "tea"},
        },
        CommandContext(ManualOrigin("web"), "web"),
    )
    assert memory.get_provenance("semantic", saved["memory_id"], 1) == {
        "state": "known_none",
        "source_groups": [],
    }
    assert memory.get_provenance("semantic", "unknown-id", 1) == {
        "state": "unknown",
        "source_groups": [],
    }
    conn.close()


def test_edit_and_delete_rollback_with_failed_ledger_preserve_version_and_search(
    tmp_path,
):
    from agent_alfred.memory.types import FactQuery

    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(ManualOrigin("web"), "web")
    saved = memory.execute(
        {
            "operation_id": "save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "coriander"},
        },
        context,
    )
    conn.execute(
        "CREATE TRIGGER ledger_failure BEFORE INSERT ON tool_ledger "
        "BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    for action, payload in [
        ("update", {"id": saved["memory_id"], "fact": "mint"}),
        ("delete", {"id": saved["memory_id"]}),
    ]:
        result = memory.execute(
            {
                "operation_id": action,
                "kind": "semantic",
                "action": action,
                "payload": payload,
                "expected_version": 1,
            },
            context,
        )
        assert result == {"error": {"code": "storage_write_failed"}}
        assert memory.get_operation(action) is None
        record = memory.get("semantic", saved["memory_id"])
        assert (record.fact, record.record_version) == ("coriander", 1)
        with memory.reading_stores() as (facts, _):
            assert [
                hit.record.id for hit in facts.search(FactQuery(text="coriander"))
            ] == [saved["memory_id"]]
            assert not facts.search(FactQuery(text="mint"))
    conn.close()


def test_repeated_operation_has_one_local_ledger_and_conflicts_do_not_overwrite(
    tmp_path,
):
    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(ManualOrigin("web"), "web")
    command = {
        "operation_id": "save",
        "kind": "semantic",
        "action": "save",
        "payload": {"subject": "me", "fact": "tea"},
    }
    saved = memory.execute(command, context)
    assert memory.execute(command, context) == saved
    # The tool ledger is a durable audit surface independent of the record API.
    rows = conn.execute("SELECT effect,status,summary FROM tool_ledger").fetchall()
    assert len(rows) == 1
    assert rows[0][:2] == ("local_write", "succeeded")
    assert "tea" not in rows[0][2]
    updated = memory.execute(
        {
            "operation_id": "edit",
            "kind": "semantic",
            "action": "update",
            "payload": {"id": saved["memory_id"], "fact": "coffee"},
            "expected_version": 1,
        },
        context,
    )
    assert updated["record_version"] == 2
    conflict = memory.execute(
        {
            "operation_id": "stale",
            "kind": "semantic",
            "action": "update",
            "payload": {"id": saved["memory_id"], "fact": "wine"},
            "expected_version": 1,
        },
        context,
    )
    assert conflict == {"error": {"code": "version_conflict", "current_version": 2}}
    assert memory.get("semantic", saved["memory_id"]).fact == "coffee"
    conn.close()


def test_delete_receipt_identifies_record_content_under_its_original_audit_key(
    tmp_path,
):
    import hashlib
    import hmac

    conn = sqlite3.connect(tmp_path / "memory.db")
    memory = service(conn)
    context = CommandContext(ManualOrigin("web"), "web")
    saved = memory.execute(
        {
            "operation_id": "save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "tea"},
        },
        context,
    )
    deleted = memory.execute(
        {
            "operation_id": "delete",
            "kind": "semantic",
            "action": "delete",
            "payload": {"id": saved["memory_id"]},
            "expected_version": 1,
        },
        context,
    )
    expected = hmac.new(
        b"x" * 32, b'{"fact":"tea","subject":"me","v":1}', hashlib.sha256
    ).hexdigest()
    assert deleted["fingerprint"] == expected
    assert deleted["key_id"] == "test"
    conn.close()
