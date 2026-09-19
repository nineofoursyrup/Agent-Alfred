"""v19 adds optional admission evidence without guessing or rewriting history."""

import sqlite3

from agent_alfred import schema


def test_v19_preserves_every_existing_value_and_attached_object(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:18])
        schema.migrate(conn)
    schema.insert_accepted_run(
        conn,
        run_id="legacy",
        purpose="chat",
        gateway="cli",
        accepted_at="2026-09-19T00:00:00Z",
        session_id=None,
    )
    conn.execute("CREATE INDEX user_runs_idx ON runs (accepted_at)")
    conn.commit()
    rows = conn.execute("SELECT * FROM runs").fetchall()
    ledger = conn.execute("SELECT * FROM schema_migrations").fetchall()
    objects = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name != 'runs'"
    ).fetchall()
    schema.migrate(conn)
    assert [r[:-1] for r in conn.execute("SELECT * FROM runs")] == rows
    assert conn.execute("SELECT routing_admission FROM runs").fetchall() == [(None,)]
    assert (
        conn.execute("SELECT * FROM schema_migrations WHERE version<=18").fetchall()
        == ledger
    )
    for name, sql in objects:
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone() == (sql,)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
        19,
    )
    schema.migrate(conn)
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (19,)
    conn.close()
