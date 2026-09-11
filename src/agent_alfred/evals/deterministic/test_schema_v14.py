"""Mirror generation responsibility is atomic and old migrations stay frozen."""

import sqlite3

from agent_alfred import schema


def test_v14_preserves_v13_objects_and_rolls_back_dirty_responsibility(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:13])
        schema.migrate(conn)
    before = conn.execute("SELECT name,sql FROM sqlite_master").fetchall()
    schema.migrate(conn)
    for name, sql in before:
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone() == (sql,)
    conn.execute("INSERT INTO memory_mirrors(target_id) VALUES ('memory/facts.md')")
    conn.commit()
    conn.execute(
        "INSERT INTO memory_provenance VALUES ('semantic','fixture',1,'known_none')"
    )
    assert conn.execute(
        "SELECT required_generation FROM memory_mirrors"
    ).fetchone() == (2,)
    conn.rollback()
    assert conn.execute(
        "SELECT required_generation FROM memory_mirrors"
    ).fetchone() == (1,)
    assert not conn.execute(
        "SELECT 1 FROM memory_provenance WHERE memory_id='fixture'"
    ).fetchone()
    conn.close()
