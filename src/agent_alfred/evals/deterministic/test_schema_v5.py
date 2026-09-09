"""Upgrade existing memory through the public numbered migration boundary."""

import hashlib
import hmac
import json
import sqlite3
import threading
from datetime import datetime

import pytest

from agent_alfred import schema
from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.commands import MemoryCommandService
from agent_alfred.memory.episodic import SQLiteEpisodicStore
from agent_alfred.memory.semantic import SQLiteSemanticStore
from agent_alfred.memory.types import (
    ConsolidationOrigin,
    EpisodeQuery,
    FactQuery,
    ManualOrigin,
    MemoryId,
    ToolOrigin,
)
from agent_alfred.runtime.recording import RecordingStore

CREATED = "2026-08-27T12:00:00+00:00"


def fingerprint(value):
    return hmac.new(b"migration-test", value, hashlib.sha256).hexdigest(), "key1"


@pytest.fixture
def version4_database(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:4])
        schema.migrate(conn)
    for kind, identity_column, identity in (
        ("manual", "origin_source", "web"),
        ("tool", "origin_call_id", "old-call"),
        ("consolidation", "origin_batch_id", "old-batch"),
    ):
        conn.execute(
            f"""INSERT INTO facts (
                id, subject, fact, origin_kind, {identity_column}, created_at,
                idempotency_key, fingerprint, key_id, normalization_version
            ) VALUES (?, ' Alice ', 'likes coriander', ?, ?, ?, ?, 'fp', 'key1', 1)""",
            ("fact-" + kind, kind, identity, CREATED, "fact-key-" + kind),
        )
        conn.execute(
            f"""INSERT INTO episodes (
                id, summary, occurred_at, occurred_until,
                origin_kind, {identity_column}, created_at,
                idempotency_key, fingerprint, key_id, normalization_version
            ) VALUES (?, 'coriander dinner', ?, NULL, ?, ?, ?, ?, 'fp', 'key1', 1)""",
            (
                "episode-" + kind,
                CREATED,
                kind,
                identity,
                CREATED,
                "episode-key-" + kind,
            ),
        )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def test_upgrade_starts_observed_versions_preserves_bodies_and_known_origins(
    version4_database,
):
    conn = version4_database
    ledger_before = conn.execute("SELECT * FROM schema_migrations").fetchall()
    schema.migrate(conn)
    semantic = SQLiteSemanticStore(conn, fingerprint=fingerprint)
    episodic = SQLiteEpisodicStore(conn, fingerprint=fingerprint)
    for suffix, origin in (
        ("manual", ManualOrigin("web")),
        ("tool", ToolOrigin("old-call")),
        ("consolidation", ConsolidationOrigin("old-batch")),
    ):
        fact = semantic.get(MemoryId("fact-" + suffix))
        episode = episodic.get(MemoryId("episode-" + suffix))
        assert (fact.subject, fact.fact) == (" Alice ", "likes coriander")
        assert episode.summary == "coriander dinner"
        assert episode.occurred_until is None
        for record in (fact, episode):
            assert record.origin == record.last_change_origin == origin
            assert record.record_version == 1
            assert (
                record.created_at
                == record.modified_at
                == datetime.fromisoformat(CREATED)
            )
    assert len(semantic.search(FactQuery(text="coriander"))) == 3
    assert len(episodic.search(EpisodeQuery(text="coriander"))) == 3
    assert (
        conn.execute("SELECT * FROM schema_migrations WHERE version <= 4").fetchall()
        == ledger_before
    )
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
        (1,),
        (2,),
        (3,),
        (4,),
        (5,),
        (6,),
    ]
    schema.migrate(conn)
    assert semantic.get(MemoryId("fact-manual")).record_version == 1


def test_upgrade_does_not_invent_run_links_operation_receipts_or_modification_history(
    version4_database,
):
    conn = version4_database
    schema.migrate(conn)
    # A known creation origin says nothing about its historical Run associations.
    assert conn.execute("SELECT * FROM memory_sources").fetchall() == []
    assert conn.execute("SELECT * FROM memory_operations").fetchall() == []
    assert conn.execute("SELECT * FROM memory_revision").fetchall() == [(1, 0)]
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("migration-test", b"x" * 32),
        clock=lambda: datetime.fromisoformat(CREATED),
    )
    for kind, prefix in (("semantic", "fact-"), ("episodic", "episode-")):
        for origin in ("manual", "tool", "consolidation"):
            assert memory.get_provenance(kind, prefix + origin, 1) == {
                "state": "unknown", "source_groups": [],
            }
    semantic = SQLiteSemanticStore(conn, fingerprint=fingerprint)
    assert semantic.get(MemoryId("fact-tool")).origin == ToolOrigin("old-call")
    for table in ("facts", "episodes"):
        for modified_at, origin in conn.execute(
            f"SELECT modified_at, last_change_origin FROM {table}"
        ):
            assert modified_at == CREATED
            assert set(json.loads(origin)) <= {"type", "source", "call_id", "batch_id"}


def test_upgrade_protects_known_human_writes_without_protecting_automatic_records(
    version4_database,
):
    conn = version4_database
    schema.migrate(conn)
    for store, prefix in (
        (SQLiteSemanticStore(conn, fingerprint=fingerprint), "fact-"),
        (SQLiteEpisodicStore(conn, fingerprint=fingerprint), "episode-"),
    ):
        assert store.get(MemoryId(prefix + "manual")).human_protected is True
        assert store.get(MemoryId(prefix + "tool")).human_protected is True
        assert store.get(MemoryId(prefix + "consolidation")).human_protected is False


def test_version5_changes_roll_back_with_callers_transaction(version4_database):
    conn = version4_database
    before = conn.execute("SELECT * FROM schema_migrations").fetchall()
    conn.execute("BEGIN")
    schema.migrate(conn)
    assert conn.in_transaction
    assert (
        SQLiteSemanticStore(conn, fingerprint=fingerprint)
        .get(MemoryId("fact-manual"))
        .record_version
        == 1
    )
    conn.rollback()
    assert conn.execute("SELECT * FROM schema_migrations").fetchall() == before
    assert "record_version" not in {
        row[1] for row in conn.execute("PRAGMA table_info(facts)")
    }
    assert conn.execute("SELECT fact FROM facts WHERE id='fact-manual'").fetchone() == (
        "likes coriander",
    )
