"""v12 adds current consolidation purpose without rewriting v3 DDL."""

import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred._schema.migrations import _V3_RUNS
from agent_alfred.evals.deterministic.test_schema import _TS, _migrate, _tables


def test_v12_purpose_check_accepts_consolidation_and_keeps_v3_sql_frozen():
    assert "consolidation" not in _V3_RUNS
    assert "inference_probe" in _V3_RUNS
    conn = _migrate()
    assert schema.LATEST_MIGRATION_VERSION >= 12
    conn.execute(
        """INSERT INTO runs (
             run_id, purpose, gateway, phase, accepted_at, activity_revision,
             admission_state
           ) VALUES ('r-cons', 'consolidation', 'cli', 'accepted', ?, 1,
                     'admitted')""",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, accepted_at, activity_revision,
                 admission_state
               ) VALUES ('r-bad', 'other', 'cli', 'accepted', ?, 2, 'admitted')""",
            (_TS,),
        )
    conn.close()


def test_v12_applies_from_a_v11_ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:11])
        schema.migrate(conn)
    assert conn.execute(
        "SELECT max(version) FROM schema_migrations"
    ).fetchone() == (11,)
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:12])
        schema.migrate(conn)
    assert conn.execute(
        "SELECT max(version) FROM schema_migrations"
    ).fetchone() == (12,)
    assert "memory_consolidation_plans" in _tables(conn)
    names = [
        row[1]
        for row in conn.execute("PRAGMA table_info(memory_consolidation_plans)")
    ]
    assert "request_json" in names
    conn.close()
