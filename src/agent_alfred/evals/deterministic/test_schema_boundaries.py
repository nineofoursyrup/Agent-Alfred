"""SCHEMA-SPEC-r1: public registry, transaction and pruning boundaries."""

import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic.test_schema import _historic_database


def _rows(conn, table):
    return conn.execute(f'SELECT * FROM "{table}"').fetchall()


def test_ce04_registry_is_captured_once_and_next_call_uses_replacement(monkeypatch):
    original = schema.MIGRATIONS
    versions = schema.MIGRATION_VERSIONS
    markers = []

    def replacement(conn):
        markers.append("B")
        conn.execute("CREATE TABLE replacement (value TEXT)")

    registry_b = (*original, schema.Migration(19, replacement, ("replacement",)))

    def switch(conn):
        markers.append("A19")
        conn.execute("CREATE TABLE captured (value TEXT)")
        monkeypatch.setattr(schema, "MIGRATIONS", registry_b)

    def tail(conn):
        markers.append("A20")
        conn.execute("INSERT INTO captured VALUES ('tail ran')")

    registry_a = (
        *original,
        schema.Migration(19, switch, ("captured",)),
        schema.Migration(20, tail, ()),
    )
    monkeypatch.setattr(schema, "MIGRATIONS", registry_a)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE captured (value TEXT)")
        with pytest.raises(schema.SchemaVersionError, match="captured"):
            schema.migrate(conn)
        assert markers == []
    conn.close()
    with sqlite3.connect(":memory:") as conn:
        schema.migrate(conn)
        assert markers == ["A19", "A20"]
        assert _rows(conn, "captured") == [("tail ran",)]
        assert conn.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (20,)
    conn.close()
    with sqlite3.connect(":memory:") as conn:
        schema.migrate(conn)
        assert markers == ["A19", "A20", "B"]
        assert _rows(conn, "replacement") == []
        assert conn.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (19,)
    conn.close()
    assert schema.MIGRATION_VERSIONS is versions
    assert schema.LATEST_MIGRATION_VERSION == 18



@pytest.mark.parametrize(
    "initial",
    [
        "managed",
        "retired",
        "empty-ledger",
        "gap",
        "illegal",
        "future",
        "unknown-v1",
        "unconvertible-v1",
    ],
)
def test_ce07_rejected_databases_retain_all_schema_rows_and_ledger(initial):
    conn = (
        _historic_database("40f7f98")
        if initial.endswith("v1")
        else sqlite3.connect(":memory:")
    )
    if initial in ("managed", "retired"):
        name = "facts" if initial == "managed" else "events"
        conn.execute(f"CREATE TABLE {name} (original TEXT)")
        conn.execute(f"INSERT INTO {name} VALUES ('preserve me')")
    elif initial == "unknown-v1":
        conn.execute("ALTER TABLE events ADD COLUMN unexpected TEXT")
    elif initial == "unconvertible-v1":
        conn.execute(
            "INSERT INTO events (title, starts_at, created_at) "
            "VALUES ('original', 'not an instant', 'raw time')"
        )
    else:
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        versions = {
            "empty-ledger": [],
            "gap": [1, 3],
            "illegal": [0],
            "future": list(range(1, 20)),
        }[initial]
        conn.executemany(
            "INSERT INTO schema_migrations VALUES (?, 'original timestamp')",
            [(v,) for v in versions],
        )
    conn.commit()
    before = list(conn.iterdump())
    with pytest.raises(schema.SchemaVersionError):
        schema.migrate(conn)
    assert list(conn.iterdump()) == before
    assert not conn.in_transaction
    conn.close()


@pytest.mark.parametrize("upgraded", [False, True])
@pytest.mark.parametrize("caller_transaction", [False, True])
def test_ce02_control_failure_rolls_back_only_pending_migrations(
    monkeypatch,
    upgraded,
    caller_transaction,
):
    conn = sqlite3.connect(":memory:")
    if upgraded:
        schema.migrate(conn)
    conn.execute("CREATE TABLE caller_notes (note TEXT)")
    before = list(conn.iterdump())
    if caller_transaction:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_notes VALUES ('pending caller write')")
    failure = KeyboardInterrupt("controlled migration interrupt")

    def fail(connection):
        connection.execute("CREATE TABLE partial (id INTEGER)")
        connection.execute("CREATE INDEX partial_idx ON partial (id)")
        raise failure

    monkeypatch.setattr(
        schema,
        "MIGRATIONS",
        (
            *schema.MIGRATIONS,
            schema.Migration(19, fail, ("partial", "partial_idx")),
        ),
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        schema.migrate(conn)
    assert raised.value is failure
    assert conn.in_transaction is caller_transaction
    assert _rows(conn, "caller_notes") == (
        [("pending caller write",)] if caller_transaction else []
    )
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'partial%'"
    ).fetchall()
    conn.rollback()
    assert list(conn.iterdump()) == before
    conn.close()


def test_ce03_prune_preserves_arbitrary_time_text_and_first_observation():
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    with conn:
        schema.record_trace_prune(
            conn,
            run_id="without-messages",
            prune_requested_at="banana",
            absence_confirmed_at="原文",
            prune_reason="age",
        )
        schema.record_trace_prune(
            conn,
            run_id="without-messages",
            prune_requested_at="later",
            absence_confirmed_at="different",
            prune_reason="manual",
        )
    assert _rows(conn, "trace_prunes") == [
        ("without-messages", "banana", "原文", "age"),
    ]
    assert _rows(conn, "agent_log") == []
    assert schema.pick_prune_reason(["capacity", "age", "age", "manual"]) == "manual"
    conn.close()


@pytest.mark.parametrize("catch_inside", [False, True])
def test_ce05_missing_session_after_run_sql_keeps_caller_transaction_ownership(
    catch_inside,
):
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    with conn:
        schema.insert_session(conn, session_id="present", created_at="原文")
        schema.insert_accepted_run(
            conn,
            run_id="r",
            purpose="chat",
            session_id="present",
            gateway="cli",
            accepted_at="unparsed",
        )
    tables = ("runs", "sessions", "activity_clock")
    before = {table: _rows(conn, table) for table in tables}

    def update():
        schema.update_run_phase(
            conn,
            run_id="r",
            from_phase="accepted",
            to_phase="running",
            activity_revision=schema.allocate_activity_revision(conn),
            started_at="unchanged text",
            session_id="missing",
        )

    if catch_inside:
        conn.execute("BEGIN")
        with pytest.raises(schema.RunPhaseError, match="exactly 1 session"):
            update()
        assert conn.in_transaction
        assert conn.execute("SELECT phase FROM runs").fetchone() == ("running",)
        assert _rows(conn, "activity_clock") != before["activity_clock"]
        conn.rollback()
    else:
        with pytest.raises(schema.RunPhaseError, match="exactly 1 session"), conn:
            update()
    assert {table: _rows(conn, table) for table in tables} == before
    assert not conn.in_transaction
    conn.close()
