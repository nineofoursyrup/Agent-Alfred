"""v11 adds consolidation lifecycle tables without rewriting frozen history."""

import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic.test_schema import (
    _TS,
    _columns,
    _migrate,
    _tables,
)


def test_v11_keeps_legacy_consolidation_status_set_and_adds_lifecycle_tables():
    conn = _migrate()
    assert schema.LATEST_MIGRATION_VERSION >= 11
    assert "memory_consolidation_batches" in _tables(conn)
    assert "consolidation_batches" in _tables(conn)
    conn.execute(
        """INSERT INTO consolidation_batches (batch_id, status, created_at)
           VALUES ('legacy', 'started', ?)""",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO consolidation_batches (batch_id, status, created_at)
               VALUES ('legacy-running', 'running', ?)""",
            (_TS,),
        )
    conn.execute(
        """INSERT INTO memory_consolidation_batches (
             batch_id, session_id, revision, status, created_at, updated_at
           ) VALUES ('b1', 's1', 1, 'awaiting_approval', ?, ?)""",
        (_TS, _TS),
    )
    assert "user_text" not in _columns(conn, "memory_consolidation_batches")
    assert "content" not in _columns(conn, "memory_consolidation_sources")
    conn.close()


def test_v11_applies_from_a_v10_ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:10])
        schema.migrate(conn)
    assert conn.execute(
        "SELECT max(version) FROM schema_migrations"
    ).fetchone() == (10,)
    schema.migrate(conn)
    assert conn.execute(
        "SELECT max(version) FROM schema_migrations"
    ).fetchone()[0] >= 11
    assert "memory_consolidation_actions" in _tables(conn)
    conn.close()
