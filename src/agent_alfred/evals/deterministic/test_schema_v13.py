"""Scheduling facts append to the frozen v12 database without touching history."""

import sqlite3

import pytest

from agent_alfred import schema


def test_v13_upgrades_v12_and_keeps_stable_unique_ownership(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:12])
        schema.migrate(conn)
    before = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:13])
        schema.migrate(conn)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
        13,
    )
    for name, sql in before:
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone() == (sql,)
    conn.execute("INSERT INTO memory_consolidation_ready VALUES ('z',1,'ready',NULL)")
    conn.execute("INSERT INTO memory_consolidation_ready VALUES ('a',2,'ready',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_consolidation_ready VALUES ('b',1,'ready',NULL)"
        )
    conn.execute(
        "INSERT INTO memory_consolidation_triggers VALUES "
        "('chat','z','system','accepted',NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_consolidation_triggers VALUES "
            "('chat2','z','system','accepted',NULL)"
        )
    conn.commit()
    schema.migrate(conn)
    assert conn.execute(
        "SELECT session_id FROM memory_consolidation_ready ORDER BY ready_order"
    ).fetchall() == [("z",), ("a",)]
    conn.close()
