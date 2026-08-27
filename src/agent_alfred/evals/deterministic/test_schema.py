"""Lock SQLite schema and idempotent migrations on an injected connection."""

from __future__ import annotations

import json
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic import historic_schema

_TS = "2026-08-27T12:00:00+00:00"
# Spelled out from the spec (#4 for the ledger states, the agent_log brief for
# the sources), deliberately NOT imported from schema. An acceptance test that
# reads the implementation's own constant passes no matter what that constant
# becomes, so it locks in nothing.
_SPEC_LEDGER_STATUSES = ("started", "succeeded", "failed", "unknown")
# ADR-0009 states the consolidator's machine separately from the tool
# ledger's. Same words today, different specs -- spelled out twice on
# purpose so a change to one cannot quietly redefine the other.
_SPEC_CONSOLIDATION_STATUSES = ("started", "succeeded", "failed", "unknown")
_SPEC_SOURCES = ("cli", "web")
# ADR-0009's three provenances for a memory row, spelled out from the ADR for
# the same reason as the sets above.
_SPEC_ORIGIN_KINDS = ("consolidation", "manual", "tool")


def _migrate() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    return conn


def _connect_with_schema_migrations() -> sqlite3.Connection:
    """A database holding only the version table, as migrate finds it."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE schema_migrations (
             version INTEGER PRIMARY KEY,
             applied_at TEXT NOT NULL
           )"""
    )
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return {name for name in names if not name.startswith("sqlite_")}


def _object_names(conn: sqlite3.Connection) -> set[str]:
    """Every object this schema owns -- tables, indexes and triggers alike."""
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master")
        if not row[0].startswith("sqlite_")
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _schema_snapshot(conn: sqlite3.Connection) -> tuple:
    master = tuple(
        conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    )
    tables = sorted(
        name
        for name in _tables(conn)
        if not name.startswith("facts_fts") and not name.startswith("episodes_fts")
    )
    columns = tuple((table, tuple(_columns(conn, table))) for table in tables)
    return master, columns


def _index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    indexes = []
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = row[1]
        indexes.append([info[2] for info in conn.execute(f"PRAGMA index_info({name})")])
    return indexes


def _column_notnull(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return bool(row[3])
    raise AssertionError(f"{table}.{column} is missing")


def test_python_sqlite_has_fts5() -> None:
    # Read the pragma directly: this asserts the interpreter, so it must not
    # route through the predicate it is meant to be independent of.
    conn = sqlite3.connect(":memory:")
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    conn.close()
    assert "ENABLE_FTS5" in options


def test_migrate_sets_busy_timeout_on_injected_connection() -> None:
    conn = sqlite3.connect(":memory:", timeout=0)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    schema.migrate(conn)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == schema.BUSY_TIMEOUT_MS
    conn.close()


def test_migrate_creates_required_tables_without_user_id() -> None:
    conn = _migrate()
    names = _tables(conn)
    required = {
        "schema_migrations",
        "calendar_entries",
        "facts",
        "facts_fts",
        "episodes",
        "episodes_fts",
        "agent_log",
        "sessions",
        "runs",
        "activity_clock",
        "tool_ledger",
        "trace_prunes",
        "consolidation_batches",
        "consolidation_ops",
    }
    assert required <= names
    extras = names - required
    assert extras <= {
        name
        for name in names
        if name.startswith("facts_fts_") or name.startswith("episodes_fts_")
    }
    for table in required:
        if table.endswith("_fts"):
            continue
        assert "user_id" not in _columns(conn, table)
    conn.close()


def test_agent_log_isolates_sessions_by_session_id() -> None:
    # What the schema supplies is a NOT NULL session_id and an index on it, and
    # that no *other* isolation axis exists (no user_id). The separation below
    # is the query's doing, not a constraint -- nothing here stops a caller from
    # selecting across sessions.
    conn = _migrate()
    columns = _columns(conn, "agent_log")
    assert "user_id" not in columns
    assert _column_notnull(conn, "agent_log", "session_id")
    assert ["session_id", "created_at"] in _index_columns(conn, "agent_log")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, created_at
               ) VALUES (NULL, 'user', ?, 'cli', ?)""",
            (json.dumps([{"type": "text", "text": "no session"}]), _TS),
        )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'user', ?, 'cli', ?)""",
        ("s1", json.dumps([{"type": "text", "text": "alpha"}]), _TS),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'assistant', ?, 'cli', ?)""",
        (
            "s1",
            json.dumps([{"type": "text", "text": "alpha-reply"}]),
            "2026-08-27T12:00:01+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'user', ?, 'web', ?)""",
        ("s2", json.dumps([{"type": "text", "text": "beta"}]), _TS),
    )
    s1 = [
        row[0]
        for row in conn.execute(
            "SELECT json_extract(content, '$[0].text') FROM agent_log"
            " WHERE session_id = ? ORDER BY created_at",
            ("s1",),
        )
    ]
    s2 = [
        row[0]
        for row in conn.execute(
            "SELECT json_extract(content, '$[0].text') FROM agent_log"
            " WHERE session_id = ? ORDER BY created_at",
            ("s2",),
        )
    ]
    assert s1 == ["alpha", "alpha-reply"]
    assert s2 == ["beta"]
    conn.close()


def test_migrate_twice_leaves_identical_schema() -> None:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    first = _schema_snapshot(conn)
    schema.migrate(conn)
    second = _schema_snapshot(conn)
    conn.close()
    assert first == second


def test_migrate_writes_one_contiguous_ledger_row_per_version() -> None:
    # Spelled out rather than derived from the registry: three published commits
    # each stamped a different schema as version 1, and the repair for that is
    # version 2. A fresh database runs both, so it ends up saying so twice.
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    schema.migrate(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    conn.close()
    assert rows == [(1,), (2,), (3,)]


def test_migrate_does_not_commit_the_callers_transaction() -> None:
    # The already-current path: every startup after the first. migrate() returns
    # without applying anything, and must not commit on the way out. The
    # first-migration path is proved separately by the next test.
    conn = _migrate()
    conn.execute("BEGIN")
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'user', ?, 'cli', ?)""",
        ("s1", json.dumps([{"type": "text", "text": "hi"}]), _TS),
    )
    schema.migrate(conn)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
    conn.close()


def test_first_migrate_joins_an_open_caller_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    schema.migrate(conn)
    assert "agent_log" in _tables(conn)
    conn.rollback()
    assert "agent_log" not in _tables(conn)
    conn.close()


def _ledger(*versions: int) -> sqlite3.Connection:
    conn = _connect_with_schema_migrations()
    for version in versions:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _TS),
        )
    return conn


def test_migrate_applies_nothing_when_the_ledger_is_already_current() -> None:
    # The ledger is the source of truth for *what ran*, and migrate() takes it
    # at its word. It is not a continuous integrity check: it cannot notice a
    # table dropped or a column rewritten after the fact, and this test does not
    # claim otherwise -- it only pins that a current ledger means no DDL.
    conn = _ledger(*range(1, schema.LATEST_MIGRATION_VERSION + 1))
    schema.migrate(conn)
    assert _tables(conn) == {"schema_migrations"}
    conn.close()


def test_migrate_rejects_a_ledger_newer_than_this_code() -> None:
    conn = _ledger(*range(1, schema.LATEST_MIGRATION_VERSION + 2))
    with pytest.raises(schema.SchemaVersionError, match="newer than this code"):
        schema.migrate(conn)
    assert _tables(conn) == {"schema_migrations"}
    conn.close()


def test_migrate_rejects_a_ledger_with_a_gap() -> None:
    # One row per applied version, and they must be a contiguous prefix of the
    # registry. A ledger that skips a version cannot say which DDL actually ran.
    conn = _ledger(schema.LATEST_MIGRATION_VERSION + 1)
    with pytest.raises(schema.SchemaVersionError, match="contiguous prefix"):
        schema.migrate(conn)
    assert _tables(conn) == {"schema_migrations"}
    conn.close()


def test_migrate_rejects_a_ledger_with_an_illegal_version() -> None:
    conn = _ledger(0)
    with pytest.raises(schema.SchemaVersionError, match="illegal version"):
        schema.migrate(conn)
    assert _tables(conn) == {"schema_migrations"}
    conn.close()


def test_migrate_rejects_a_version_table_with_no_row() -> None:
    # The drift case the ledger exists to catch: a database whose shape nothing
    # vouches for. Re-running DDL over it would leave an already-drifted table
    # as-is and report success.
    conn = _connect_with_schema_migrations()
    with pytest.raises(schema.SchemaVersionError, match="records no applied version"):
        schema.migrate(conn)
    assert _tables(conn) == {"schema_migrations"}
    conn.close()


def test_migrate_refuses_an_unversioned_database_that_holds_a_managed_object() -> None:
    # The regression: an old, malformed table and no ledger at all. Registering
    # it as version 1 would vouch for a shape nothing verified.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trace_prunes (run_id TEXT)")
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    text = str(exc.value)
    assert "trace_prunes" in text
    assert "Back the database file up" in text
    # Left exactly as found: no DDL ran, and no version was registered.
    assert _tables(conn) == {"trace_prunes"}
    assert _columns(conn, "trace_prunes") == ["run_id"]
    conn.close()


def test_migrate_refuses_an_unversioned_database_holding_a_retired_object() -> None:
    # `events` is what calendar_entries was called before the glossary rename.
    # A database from that revision is an unversioned database of *ours*, so
    # dropping the old name from the manifest would let exactly the database
    # the guard exists to catch be stamped with a version.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT)")
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    assert "events" in str(exc.value)
    assert _tables(conn) == {"events"}
    conn.close()


def test_schema_migrations_applied_at_is_an_aware_instant() -> None:
    # The ledger's own timestamp has to satisfy the module's own instant
    # contract; datetime('now') would store a naive string parse_instant rejects.
    conn = _migrate()
    applied_at = conn.execute("SELECT applied_at FROM schema_migrations").fetchone()[0]
    conn.close()
    assert schema.parse_instant(applied_at).utcoffset().total_seconds() == 0


def test_every_object_migrate_creates_is_listed_in_the_managed_manifest() -> None:
    # Not a spec value, so reading the implementation's manifest is the point:
    # this pins the manifest against the DDL beside it. An object created but
    # left off the manifest would reopen the unversioned-database hole above.
    conn = _migrate()
    created = _object_names(conn)
    conn.close()
    assert created == set(schema.MANAGED_OBJECTS)


def test_calendar_entries_table_has_title_start_end_participants_notes() -> None:
    conn = _migrate()
    columns = _columns(conn, "calendar_entries")
    for name in (
        "title",
        "starts_at",
        "ends_at",
        "participants",
        "notes",
        "created_at",
        "iana_time_zone",
    ):
        assert name in columns
    conn.close()


def test_calendar_entry_keeps_each_instant_paired_with_its_zone_name() -> None:
    # The contract the schema can actually prove: two offset-bearing absolute
    # instants and one zone name go in, and each row comes back with its pair
    # intact. The conversion below runs on those read-back values to show what
    # the pair is *for*; the differing offsets are ZoneInfo's doing, not a
    # property of the columns, so they are asserted from real stored data only.
    tz_name = "America/New_York"
    conn = _migrate()
    for title, instant in (
        ("before-dst", "2027-03-14T06:00:00+00:00"),
        ("after-dst", "2027-03-14T07:00:00+00:00"),
    ):
        conn.execute(
            """INSERT INTO calendar_entries (
                 title, starts_at, ends_at, iana_time_zone, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (title, instant, instant, tz_name, _TS),
        )
    rows = conn.execute(
        "SELECT title, starts_at, ends_at, iana_time_zone FROM calendar_entries"
        " ORDER BY title"
    ).fetchall()
    assert rows == [
        (
            "after-dst",
            "2027-03-14T07:00:00+00:00",
            "2027-03-14T07:00:00+00:00",
            tz_name,
        ),
        (
            "before-dst",
            "2027-03-14T06:00:00+00:00",
            "2027-03-14T06:00:00+00:00",
            tz_name,
        ),
    ]
    local_offsets = {
        title: schema.parse_instant(starts_at).astimezone(ZoneInfo(zone)).utcoffset()
        for title, starts_at, _, zone in rows
    }
    assert local_offsets["before-dst"] != local_offsets["after-dst"]
    conn.close()


def test_calendar_entry_starts_at_rejects_naive_datetime() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO calendar_entries (title, starts_at, created_at)
               VALUES ('naive', '2026-07-01T12:00:00', ?)""",
            (_TS,),
        )
    conn.close()


def test_calendar_entry_check_rejects_strings_sqlite_cannot_parse_as_instants() -> None:
    # The CHECK is a bounded format-and-parsability guard, not a validator.
    # These are the shapes it does stop.
    conn = _migrate()
    for bad in ("garbageZ", "Z", "2026-13-99T99:99:99Z", "garbage+08:00"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO calendar_entries (title, starts_at, created_at)
                   VALUES ('bad', ?, ?)""",
                (bad, _TS),
            )
    for bad in ("garbageZ", "2026-13-99T99:99:99Z"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO calendar_entries (title, starts_at, ends_at, created_at)
                   VALUES ('bad-end', ?, ?, ?)""",
                ("2026-07-01T12:00:00+00:00", bad, _TS),
            )
    assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone() == (0,)
    conn.close()


def test_calendar_entry_accepts_every_zone_name_zoneinfo_can_load() -> None:
    # The regression: the CHECK demanded an interior '/' for anything but 'UTC',
    # which rejected every slashless tzdata key. Each name is handed to ZoneInfo
    # first, so this list cannot drift into names that only look plausible --
    # and half of it has no '/' at all, which is where the false negative was.
    conn = _migrate()
    slashless = ("UTC", "GMT", "CET", "EST", "MST", "Factory", "CST6CDT", "EST5EDT")
    with_slash = (
        "Asia/Shanghai",
        "America/New_York",
        "Etc/GMT+8",
        "Etc/GMT-8",
        "America/Argentina/Buenos_Aires",
    )
    for zone in (*slashless, *with_slash):
        assert ZoneInfo(zone) is not None
        conn.execute(
            """INSERT INTO calendar_entries (
                 title, starts_at, iana_time_zone, created_at
               ) VALUES ('good-zone', ?, ?, ?)""",
            ("2026-07-01T12:00:00+00:00", zone, _TS),
        )
    stored = {
        row[0] for row in conn.execute("SELECT iana_time_zone FROM calendar_entries")
    }
    assert stored == {*slashless, *with_slash}
    conn.close()


def test_calendar_entry_check_rejects_shapes_that_are_not_zone_names() -> None:
    conn = _migrate()
    bad_zones = (
        "+08:00",
        "08:00",
        "-0800",
        "",
        "  ",
        "//",
        "Asia/",
        "/Shanghai",
        "Asia//Shanghai",
        "not a zone/x",
        "Asia/Shang hai",
        "Asia/Shanghai ",
        "9Zone",
        "Asia/Shanghai;DROP",
    )
    for bad in bad_zones:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO calendar_entries (
                     title, starts_at, iana_time_zone, created_at
                   ) VALUES ('bad-zone', ?, ?, ?)""",
                ("2026-07-01T12:00:00+00:00", bad, _TS),
            )
    assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone() == (0,)
    conn.close()


def test_calendar_entry_checks_do_not_validate_tzdata_or_calendar_ranges() -> None:
    # Stated as a test so the limit is not merely a claim in a comment: the
    # zone CHECK never consults tzdata, so a well-formed invention passes with
    # or without a '/'; SQLite normalises an out-of-range day rather than
    # rejecting it, and its parser takes a bare time with no date. Real IANA
    # validity and recurrence rules belong to the writing layer, not to this
    # schema, and nothing here should be read as promising them.
    conn = _migrate()
    for title, instant, zone in (
        ("out-of-range-day", "2026-02-30T00:00:00Z", "Mars/Olympus_Mons"),
        ("time-with-no-date", "12:00:00Z", "Narnia"),
    ):
        with pytest.raises(ZoneInfoNotFoundError):
            ZoneInfo(zone)
        conn.execute(
            """INSERT INTO calendar_entries (
                 title, starts_at, iana_time_zone, created_at
               ) VALUES (?, ?, ?, ?)""",
            (title, instant, zone, _TS),
        )
    assert conn.execute(
        "SELECT title, starts_at, iana_time_zone FROM calendar_entries"
        " ORDER BY title"
    ).fetchall() == [
        ("out-of-range-day", "2026-02-30T00:00:00Z", "Mars/Olympus_Mons"),
        ("time-with-no-date", "12:00:00Z", "Narnia"),
    ]
    conn.close()


def test_parse_instant_rejects_naive_values() -> None:
    with pytest.raises(ValueError, match="naive"):
        schema.parse_instant("2026-07-01T12:00:00")
    parsed = schema.parse_instant("2026-07-01T12:00:00+08:00")
    assert parsed.tzinfo is not None
    assert schema.parse_instant("2026-07-01T12:00:00Z").utcoffset().total_seconds() == 0


def test_facts_fts_syncs_on_insert_update_delete() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO facts (
             id, subject, fact, origin_kind, origin_source,
             created_at, idempotency_key, fingerprint, key_id,
             normalization_version
           ) VALUES (
             'm1', 'alice', 'likes tea', 'manual', 'cli',
             ?, 'k-m1', 'fp', 'key1', 1
           )""",
        (_TS,),
    )
    hits = conn.execute(
        "SELECT fact FROM facts_fts WHERE facts_fts MATCH 'tea'"
    ).fetchall()
    assert hits == [("likes tea",)]

    conn.execute("UPDATE facts SET fact = 'likes coffee' WHERE id = 'm1'")
    assert (
        conn.execute(
            "SELECT fact FROM facts_fts WHERE facts_fts MATCH 'tea'"
        ).fetchall()
        == []
    )
    assert conn.execute(
        "SELECT fact FROM facts_fts WHERE facts_fts MATCH 'coffee'"
    ).fetchall() == [("likes coffee",)]

    conn.execute("DELETE FROM facts WHERE id = 'm1'")
    assert (
        conn.execute(
            "SELECT fact FROM facts_fts WHERE facts_fts MATCH 'coffee'"
        ).fetchall()
        == []
    )
    conn.close()


def test_episodes_fts_syncs_on_insert_update_delete() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO episodes (
             id, summary, occurred_at, origin_kind, origin_source,
             created_at, idempotency_key, fingerprint, key_id,
             normalization_version
           ) VALUES (
             'e1', 'dinner with bob', ?, 'manual', 'cli',
             ?, 'k-e1', 'fp', 'key1', 1
           )""",
        (_TS, _TS),
    )
    assert conn.execute(
        "SELECT summary FROM episodes_fts WHERE episodes_fts MATCH 'bob'"
    ).fetchall() == [("dinner with bob",)]

    conn.execute("UPDATE episodes SET summary = 'lunch with cara' WHERE id = 'e1'")
    assert (
        conn.execute(
            "SELECT summary FROM episodes_fts WHERE episodes_fts MATCH 'bob'"
        ).fetchall()
        == []
    )
    assert conn.execute(
        "SELECT summary FROM episodes_fts WHERE episodes_fts MATCH 'cara'"
    ).fetchall() == [("lunch with cara",)]

    conn.execute("DELETE FROM episodes WHERE id = 'e1'")
    assert (
        conn.execute(
            "SELECT summary FROM episodes_fts WHERE episodes_fts MATCH 'cara'"
        ).fetchall()
        == []
    )
    conn.close()


def test_agent_log_telemetry_roundtrips_run_level_json() -> None:
    conn = _migrate()
    telemetry = {
        "steps": [{"step_index": 0, "stop_reason": "end_turn"}],
        "attempts": [
            {
                "attempt_id": "a1",
                "committed": True,
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
            {
                "attempt_id": "a2",
                "committed": False,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        ],
        "tools": [
            {"call_id": "c1", "name": "create_calendar_entry", "status": "succeeded"}
        ],
        "trace_incomplete": True,
        "trace_incomplete_reason": "trace sink flush failed: ENOSPC",
    }
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES (?, 'assistant', ?, 'cli', ?, ?)""",
        (
            "s1",
            json.dumps([{"type": "text", "text": "done"}]),
            json.dumps(telemetry),
            _TS,
        ),
    )
    raw = conn.execute("SELECT telemetry FROM agent_log").fetchone()[0]
    loaded = json.loads(raw)
    assert loaded["trace_incomplete"] is True
    assert loaded["trace_incomplete_reason"] == "trace sink flush failed: ENOSPC"
    assert loaded["attempts"][1]["committed"] is False
    assert loaded["attempts"][1]["usage"]["output_tokens"] == 1
    assert loaded["tools"][0]["name"] == "create_calendar_entry"
    conn.close()


def test_agent_log_consolidated_flag_is_zero_or_one() -> None:
    # The brief's 「是否已提炼」column. Without the CHECK this is a free-form
    # integer, and "2" would be neither consolidated nor not.
    conn = _migrate()
    body = json.dumps([{"type": "text", "text": "x"}])
    assert _column_notnull(conn, "agent_log", "consolidated")
    for flag in (0, 1):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, consolidated, created_at
               ) VALUES ('s1', 'user', ?, 'cli', ?, ?)""",
            (body, flag, _TS),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, consolidated, created_at
               ) VALUES ('s1', 'user', ?, 'cli', 2, ?)""",
            (body, _TS),
        )
    stored = {row[0] for row in conn.execute("SELECT consolidated FROM agent_log")}
    assert stored == {0, 1}
    conn.close()


def test_agent_log_rejects_content_and_telemetry_that_are_not_json() -> None:
    # The brief requires a JSON telemetry column. A TEXT column called
    # "telemetry" that accepts 'oops' would carry the name without the shape.
    conn = _migrate()
    body = json.dumps([{"type": "text", "text": "x"}])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, telemetry, created_at
               ) VALUES ('s1', 'assistant', ?, 'cli', 'not json', ?)""",
            (body, _TS),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, created_at
               ) VALUES ('s1', 'assistant', 'not json', 'cli', ?)""",
            (_TS,),
        )
    # NULL telemetry stays legal: a user row has no Run-level telemetry.
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES ('s1', 'user', ?, 'cli', ?)""",
        (body, _TS),
    )
    assert conn.execute("SELECT telemetry FROM agent_log").fetchone() == (None,)
    conn.close()


def test_agent_log_rejects_system_role() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, created_at
               ) VALUES ('s1', 'system', ?, 'cli', ?)""",
            (json.dumps([{"type": "text", "text": "no"}]), _TS),
        )
    conn.close()


def test_facts_origin_kind_check_is_the_closed_set() -> None:
    conn = _migrate()
    required = {
        "consolidation": "origin_batch_id",
        "manual": "origin_source",
        "tool": "origin_call_id",
    }
    value = {"origin_source": "cli"}
    for i, kind in enumerate(_SPEC_ORIGIN_KINDS):
        column = required[kind]
        conn.execute(
            f"""INSERT INTO facts (
                 id, subject, fact, origin_kind, {column},
                 created_at, idempotency_key, fingerprint, key_id,
                 normalization_version
               ) VALUES (?, 'alice', 'likes tea', ?, ?, ?, ?, 'fp', 'key1', 1)""",
            (f"f{i}", kind, value.get(column, f"id-{i}"), _TS, f"k-f{i}"),
        )
    stored = {row[0] for row in conn.execute("SELECT origin_kind FROM facts")}
    assert stored == set(_SPEC_ORIGIN_KINDS)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO facts (
                 id, subject, fact, origin_kind, origin_source,
                 created_at, idempotency_key, fingerprint, key_id,
                 normalization_version
               ) VALUES (
                 'fx', 'alice', 'likes tea', 'imported', 'cli',
                 ?, 'k-fx', 'fp', 'key1', 1
               )""",
            (_TS,),
        )
    conn.close()


def test_facts_reject_manual_origin_that_also_carries_a_batch_id() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO facts (
                 id, subject, fact, origin_kind, origin_source, origin_batch_id,
                 created_at, idempotency_key, fingerprint, key_id,
                 normalization_version
               ) VALUES (
                 'm1', 'alice', 'likes tea', 'manual', 'cli', 'batch-1',
                 ?, 'k-m1', 'fp', 'key1', 1
               )""",
            (_TS,),
        )
    conn.close()


def test_tool_ledger_rejects_local_read_and_indexes_fingerprint() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO tool_ledger (
                 tool_name, fingerprint, effect, status, created_at
               ) VALUES ('search_facts', 'fp', 'local_read', 'succeeded', ?)""",
            (_TS,),
        )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('create_calendar_entry', 'fp-1', 'local_write', 'succeeded', ?)""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('web_search', 'fp-2', 'external', 'started', ?)""",
        (_TS,),
    )
    assert any(
        cols[:2] == ["tool_name", "fingerprint"]
        for cols in _index_columns(conn, "tool_ledger")
    )
    conn.close()


def test_trace_prunes_reject_a_null_timestamp() -> None:
    # The NOT NULL column rejects it; record_trace_prune only passes it through.
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        schema.record_trace_prune(
            conn,
            run_id="run-1",
            prune_requested_at=None,  # type: ignore[arg-type]
            absence_confirmed_at=_TS,
            prune_reason="age",
        )
    assert conn.execute("SELECT COUNT(*) FROM trace_prunes").fetchone() == (0,)
    conn.close()


def test_trace_prune_same_run_id_does_not_insert_a_second_row() -> None:
    conn = _migrate()
    schema.record_trace_prune(
        conn,
        run_id="run-1",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason="age",
    )
    schema.record_trace_prune(
        conn,
        run_id="run-1",
        prune_requested_at="2026-08-28T00:00:00+00:00",
        absence_confirmed_at="2026-08-28T00:00:00+00:00",
        prune_reason="manual",
    )
    rows = conn.execute("SELECT run_id, prune_reason FROM trace_prunes").fetchall()
    assert rows == [("run-1", "age")]
    conn.close()


def test_trace_prunes_have_no_foreign_keys() -> None:
    conn = _migrate()
    fks = conn.execute("PRAGMA foreign_key_list(trace_prunes)").fetchall()
    assert fks == []
    schema.record_trace_prune(
        conn,
        run_id="never-logged",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason="disk_low",
    )
    assert conn.execute("SELECT run_id FROM trace_prunes").fetchone() == (
        "never-logged",
    )
    conn.close()


def test_tool_ledger_keeps_two_rows_with_the_same_fingerprint() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES (
             'create_calendar_entry', 'same-fp', 'local_write', 'succeeded', ?
           )""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES (
             'create_calendar_entry', 'same-fp', 'local_write', 'succeeded', ?
           )""",
        (_TS,),
    )
    count = conn.execute("SELECT COUNT(*) FROM tool_ledger").fetchone()[0]
    assert count == 2
    conn.close()


def test_tool_ledger_status_check_is_the_closed_set() -> None:
    conn = _migrate()
    for i, status in enumerate(_SPEC_LEDGER_STATUSES):
        conn.execute(
            """INSERT INTO tool_ledger (
                 tool_name, fingerprint, effect, status, created_at
               ) VALUES (?, ?, 'external', ?, ?)""",
            (f"t{i}", f"fp-{i}", status, _TS),
        )
    stored = {row[0] for row in conn.execute("SELECT status FROM tool_ledger")}
    assert stored == set(_SPEC_LEDGER_STATUSES)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO tool_ledger (
                 tool_name, fingerprint, effect, status, created_at
               ) VALUES ('web_search', 'bad', 'external', 'running', ?)""",
            (_TS,),
        )
    conn.close()


def test_agent_log_source_check_is_the_closed_set() -> None:
    conn = _migrate()
    for source in _SPEC_SOURCES:
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, created_at
               ) VALUES (?, 'user', ?, ?, ?)""",
            (
                f"s-{source}",
                json.dumps([{"type": "text", "text": source}]),
                source,
                _TS,
            ),
        )
    stored = {row[0] for row in conn.execute("SELECT source FROM agent_log")}
    assert stored == set(_SPEC_SOURCES)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, created_at
               ) VALUES ('s-x', 'user', ?, 'telegram', ?)""",
            (json.dumps([{"type": "text", "text": "no"}]), _TS),
        )
    conn.close()


def test_trace_prunes_in_one_transaction_roll_back_together() -> None:
    conn = _migrate()
    conn.execute("BEGIN")
    schema.record_trace_prune(
        conn,
        run_id="run-a",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason="age",
    )
    schema.record_trace_prune(
        conn,
        run_id="run-b",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason="capacity",
    )
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM trace_prunes").fetchone() == (0,)
    conn.close()


def test_record_trace_prune_stores_the_highest_priority_reason() -> None:
    conn = _migrate()
    schema.record_trace_prune(
        conn,
        run_id="run-1",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason=schema.pick_prune_reason(("age", "capacity", "manual")),
    )
    assert conn.execute("SELECT prune_reason FROM trace_prunes").fetchone() == (
        "manual",
    )
    conn.close()


def test_pick_prune_reason_takes_manual_over_disk_low_over_age_over_capacity() -> None:
    assert (
        schema.pick_prune_reason(("age", "manual", "capacity", "disk_low")) == "manual"
    )
    assert schema.pick_prune_reason(("capacity", "age")) == "age"
    assert schema.pick_prune_reason(("capacity", "disk_low")) == "disk_low"
    assert schema.pick_prune_reason(("capacity",)) == "capacity"


def test_pick_prune_reason_rejects_empty_and_unknown_with_the_same_field_name() -> None:
    with pytest.raises(ValueError, match="prune_reason"):
        schema.pick_prune_reason(())
    with pytest.raises(ValueError, match="prune_reason"):
        schema.pick_prune_reason(("expired",))


def test_record_trace_prune_rejects_unknown_reason() -> None:
    conn = _migrate()
    with pytest.raises(ValueError, match="prune_reason"):
        schema.record_trace_prune(
            conn,
            run_id="run-1",
            prune_requested_at=_TS,
            absence_confirmed_at=_TS,
            prune_reason="expired",
        )
    conn.close()


def test_completed_instants_do_not_carry_an_iana_time_zone_column() -> None:
    conn = _migrate()
    for table in (
        "facts",
        "episodes",
        "agent_log",
        "sessions",
        "runs",
        "activity_clock",
        "tool_ledger",
        "trace_prunes",
        "consolidation_batches",
        "consolidation_ops",
    ):
        assert "iana_time_zone" not in _columns(conn, table)
    conn.close()


def test_consolidation_batch_id_is_the_primary_key() -> None:
    # Uniqueness only. Whether the id is *derived deterministically* is the
    # consolidator's contract, not the schema's -- this ticket cannot prove it,
    # so it does not hold the name.
    conn = _migrate()
    conn.execute(
        """INSERT INTO consolidation_batches (batch_id, status, created_at)
           VALUES ('batch-1', 'started', ?)""",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO consolidation_batches (batch_id, status, created_at)
               VALUES ('batch-1', 'started', ?)""",
            (_TS,),
        )
    conn.close()


def test_consolidation_op_id_primary_key_rejects_a_second_row() -> None:
    # The only property this table has: one row per op id. Any ON CONFLICT
    # clause is the caller's choice of write, so asserting one here would test
    # SQLite rather than the schema.
    conn = _migrate()
    write = """INSERT INTO consolidation_ops (op_id, batch_id, status, created_at)
               VALUES (?, 'batch-1', ?, ?)"""
    conn.execute(write, ("op-1", "started", _TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(write, ("op-1", "succeeded", _TS))
    rows = conn.execute("SELECT op_id, status FROM consolidation_ops").fetchall()
    assert rows == [("op-1", "started")]
    conn.close()


def test_consolidation_op_row_accepts_an_update_to_each_terminal_state() -> None:
    # The schema permits the row to move on from 'started'; it does not enforce
    # a transition graph, and nothing here claims it does.
    conn = _migrate()
    for terminal in ("succeeded", "failed", "unknown"):
        op_id = f"op-{terminal}"
        conn.execute(
            """INSERT INTO consolidation_ops (op_id, batch_id, status, created_at)
               VALUES (?, 'batch-1', 'started', ?)""",
            (op_id, _TS),
        )
        conn.execute(
            "UPDATE consolidation_ops SET status = ?, finished_at = ? WHERE op_id = ?",
            (terminal, _TS, op_id),
        )
    stored = dict(conn.execute("SELECT op_id, status FROM consolidation_ops"))
    assert stored == {
        "op-succeeded": "succeeded",
        "op-failed": "failed",
        "op-unknown": "unknown",
    }
    conn.close()


def test_consolidation_ops_offers_no_foreign_key_guarantee() -> None:
    # The decorative REFERENCES is gone. Foreign keys are off on this
    # connection, so a declared one would have read like a promise while
    # enforcing nothing. Which layer keeps batch/op consistency is not settled
    # here, and this test does not claim any layer already does.
    conn = _migrate()
    assert conn.execute("PRAGMA foreign_key_list(consolidation_ops)").fetchall() == []
    conn.execute(
        """INSERT INTO consolidation_ops (op_id, batch_id, status, created_at)
           VALUES ('orphan', 'NO-SUCH-BATCH', 'started', ?)""",
        (_TS,),
    )
    assert conn.execute("SELECT batch_id FROM consolidation_ops").fetchone() == (
        "NO-SUCH-BATCH",
    )
    conn.close()


def test_consolidation_status_check_is_its_own_closed_set() -> None:
    conn = _migrate()
    for i, status in enumerate(_SPEC_CONSOLIDATION_STATUSES):
        conn.execute(
            """INSERT INTO consolidation_batches (batch_id, status, created_at)
               VALUES (?, ?, ?)""",
            (f"batch-{i}", status, _TS),
        )
    stored = {
        row[0] for row in conn.execute("SELECT status FROM consolidation_batches")
    }
    assert stored == set(_SPEC_CONSOLIDATION_STATUSES)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO consolidation_batches (batch_id, status, created_at)
               VALUES ('batch-x', 'running', ?)""",
            (_TS,),
        )
    conn.close()


def test_migrate_raises_when_fts5_is_missing() -> None:
    inner = sqlite3.connect(":memory:", timeout=0)

    class NoFts5:
        in_transaction = False

        def execute(self, sql, *args, **kwargs):
            if "compile_options" in sql.lower():
                return [("THREADSAFE=1",)]
            if sql.lstrip().upper().startswith("CREATE"):
                raise AssertionError("schema must not be applied without FTS5")
            return inner.execute(sql, *args, **kwargs)

    with pytest.raises(schema.Fts5UnavailableError) as exc:
        schema.migrate(NoFts5())
    text = str(exc.value)
    assert "ENABLE_FTS5" in text
    assert sqlite3.sqlite_version in text
    assert "python.org" in text or "Homebrew" in text or "rebuild" in text.lower()
    inner.close()


# ---------------------------------------------------------------------------
# Upgrading the databases three published commits each stamped as version 1.
# ---------------------------------------------------------------------------

_SEED_START = "2027-03-14T06:00:00+00:00"
_SEED_END = "2027-03-14T07:00:00+00:00"
_HISTORIC_COMMITS = tuple(sorted(historic_schema.V1_SCHEMAS))


def _normalized_master(conn: sqlite3.Connection) -> dict[str, str]:
    """sqlite_master keyed by name, with comments and indentation removed.

    Deliberately not schema's own normaliser: comparing two schemas through the
    implementation's idea of "same" would pass however wrong that idea got.
    """
    return {
        name: " ".join(re.sub(r"--[^\n]*", " ", sql).split())
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master")
        if sql is not None
    }


def _historic_calendar_table(commit: str) -> str:
    statements = historic_schema.V1_SCHEMAS[commit]
    if historic_schema.EVENTS_TABLE in statements:
        return "events"
    return "calendar_entries"


def _historic_database(commit: str) -> sqlite3.Connection:
    """A database exactly as `commit` left it: its DDL, its version 1 row."""
    conn = sqlite3.connect(":memory:")
    for statement in historic_schema.V1_SCHEMAS[commit]:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
        (historic_schema.V1_APPLIED_AT,),
    )
    return conn


def _seed_version_1_rows(conn: sqlite3.Connection, commit: str) -> None:
    """One row in every table version 1 created, so the upgrade has data to lose."""
    if _historic_calendar_table(commit) == "events":
        conn.execute(
            """INSERT INTO events (
                 id, title, starts_at, ends_at, participants, notes, created_at
               ) VALUES (7, 'standup', ?, ?, 'alice,bob', 'daily', ?)""",
            (_SEED_START, _SEED_END, _TS),
        )
    else:
        conn.execute(
            """INSERT INTO calendar_entries (
                 id, title, starts_at, ends_at, iana_time_zone,
                 participants, notes, created_at
               ) VALUES (7, 'standup', ?, ?, 'America/New_York',
                         'alice,bob', 'daily', ?)""",
            (_SEED_START, _SEED_END, _TS),
        )
    conn.execute(
        """INSERT INTO facts (
             id, subject, fact, origin_kind, origin_source, created_at,
             idempotency_key, fingerprint, key_id, normalization_version
           ) VALUES (
             'f1', 'alice', 'likes tea', 'manual', 'cli', ?,
             'k-f1', 'fp', 'key1', 1
           )""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO episodes (
             id, summary, occurred_at, origin_kind, origin_source, created_at,
             idempotency_key, fingerprint, key_id, normalization_version
           ) VALUES (
             'e1', 'dinner with bob', ?, 'manual', 'cli', ?,
             'k-e1', 'fp', 'key1', 1
           )""",
        (_TS, _TS),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES ('s1', 'assistant', ?, 'cli', ?, ?)""",
        (
            json.dumps([{"type": "text", "text": "done"}]),
            json.dumps({"trace_incomplete": False}),
            _TS,
        ),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('web_search', 'fp-1', 'external', 'unknown', ?)""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO trace_prunes (
             run_id, prune_requested_at, absence_confirmed_at, prune_reason
           ) VALUES ('run-1', ?, ?, 'disk_low')""",
        (_TS, _TS),
    )
    conn.execute(
        """INSERT INTO consolidation_batches (batch_id, status, created_at)
           VALUES ('batch-1', 'succeeded', ?)""",
        (_TS,),
    )
    # An orphan on purpose: version 1 declared a foreign key here but ran with
    # foreign keys off, so orphans are part of what a real database holds.
    conn.execute(
        """INSERT INTO consolidation_ops (op_id, batch_id, status, created_at)
           VALUES ('op-1', 'NO-SUCH-BATCH', 'started', ?)""",
        (_TS,),
    )


def _all_rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = (
        "facts",
        "episodes",
        "agent_log",
        "tool_ledger",
        "trace_prunes",
        "consolidation_batches",
        "consolidation_ops",
    )
    rows = {}
    for table in tables:
        if table == "agent_log":
            # Version 3 appends run_id; the upgrade must keep the original
            # eight columns byte-for-byte, so the comparison is on those.
            rows[table] = conn.execute(
                """SELECT id, session_id, role, content, consolidated,
                          source, telemetry, created_at
                   FROM agent_log"""
            ).fetchall()
        else:
            rows[table] = conn.execute(f"SELECT * FROM {table}").fetchall()
    return rows


def test_version_1_ddl_is_frozen_at_the_shape_the_last_commit_published() -> None:
    # Three commits published three different schemas as version 1. Editing
    # version 1 again would make that four: every database already holding a
    # version 1 row is skipped, so the edit would reach no existing database
    # while quietly changing what "version 1" is supposed to mean. What version
    # 1 leaves behind is now pinned to 6d7659c, the last shape actually shipped
    # under that number; anything newer is version 2's job.
    frozen = sqlite3.connect(":memory:")
    schema.MIGRATIONS[0].apply(frozen)
    published = sqlite3.connect(":memory:")
    for statement in historic_schema.V1_6D7659C:
        published.execute(statement)
    assert _normalized_master(frozen) == _normalized_master(published)
    frozen.close()
    published.close()


@pytest.mark.parametrize("commit", _HISTORIC_COMMITS)
def test_upgrading_a_version_1_database_keeps_every_row(commit: str) -> None:
    conn = _historic_database(commit)
    _seed_version_1_rows(conn, commit)
    before = _all_rows(conn)
    schema.migrate(conn)
    assert _all_rows(conn) == before
    # The schedule survives its table being rebuilt under a new name. The zone
    # column did not exist before 6d7659c, so those two databases arrive with
    # nothing to put in it -- NULL, not a guessed local zone.
    expected_zone = "America/New_York" if commit == "6d7659c" else None
    assert conn.execute(
        """SELECT id, title, starts_at, ends_at, iana_time_zone,
                  participants, notes, created_at
             FROM calendar_entries"""
    ).fetchall() == [
        (
            7,
            "standup",
            _SEED_START,
            _SEED_END,
            expected_zone,
            "alice,bob",
            "daily",
            _TS,
        )
    ]
    # The FTS index was built by version 1's triggers and is untouched by the
    # upgrade; a rebuilt-from-empty facts table would show up here as no hit.
    assert conn.execute(
        "SELECT fact FROM facts_fts WHERE facts_fts MATCH 'tea'"
    ).fetchall() == [("likes tea",)]
    # Historic messages keep a null run_id; the Session is backfilled from
    # the original session_id, not invented.
    assert conn.execute("SELECT run_id FROM agent_log").fetchall() == [(None,)]
    assert conn.execute(
        "SELECT session_id, created_at FROM sessions"
    ).fetchall() == [("s1", _TS)]
    conn.close()


@pytest.mark.parametrize("commit", _HISTORIC_COMMITS)
def test_upgrading_a_version_1_database_lands_the_current_shape(commit: str) -> None:
    conn = _historic_database(commit)
    _seed_version_1_rows(conn, commit)
    schema.migrate(conn)
    fresh = _migrate()
    assert _normalized_master(conn) == _normalized_master(fresh)
    fresh.close()
    assert "events" not in _object_names(conn)
    assert "calendar_entries" in _tables(conn)
    # The decorative foreign key is gone, and it is gone from a database that
    # was carrying an orphan row all along.
    assert conn.execute("PRAGMA foreign_key_list(consolidation_ops)").fetchall() == []
    assert conn.execute("SELECT batch_id FROM consolidation_ops").fetchone() == (
        "NO-SUCH-BATCH",
    )
    # The version 2 CHECKs are live on the rebuilt table: a naive wall clock and
    # a malformed zone are refused, and a slashless tzdata key is not.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO calendar_entries (title, starts_at, created_at)
               VALUES ('naive', '2026-09-02T19:00:00', ?)""",
            (_TS,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO calendar_entries (
                 title, starts_at, iana_time_zone, created_at
               ) VALUES ('bad-zone', ?, '//', ?)""",
            (_SEED_START, _TS),
        )
    conn.execute(
        """INSERT INTO calendar_entries (
             title, starts_at, iana_time_zone, created_at
           ) VALUES ('slashless', ?, 'GMT', ?)""",
        (_SEED_START, _TS),
    )
    # Version 1's own ledger row is left exactly as version 1 wrote it, naive
    # `datetime('now')` and all. The upgrade records that it ran; it does not
    # go back and restate an entry it did not make.
    assert conn.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()[0] == (1, historic_schema.V1_APPLIED_AT)
    assert conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,)]
    conn.close()


@pytest.mark.parametrize("commit", _HISTORIC_COMMITS)
def test_migrating_an_upgraded_database_again_changes_nothing(commit: str) -> None:
    conn = _historic_database(commit)
    _seed_version_1_rows(conn, commit)
    schema.migrate(conn)
    schema_before = _schema_snapshot(conn)
    rows_before = _all_rows(conn)
    calendar_before = conn.execute("SELECT * FROM calendar_entries").fetchall()
    ledger_before = conn.execute("SELECT * FROM schema_migrations").fetchall()
    schema.migrate(conn)
    assert _schema_snapshot(conn) == schema_before
    assert _all_rows(conn) == rows_before
    assert conn.execute("SELECT * FROM calendar_entries").fetchall() == calendar_before
    assert conn.execute("SELECT * FROM schema_migrations").fetchall() == ledger_before
    conn.close()


def test_migrate_refuses_version_1_rows_version_2_could_not_carry_across() -> None:
    # `events` had no CHECK on starts_at, so a naive local wall clock could go
    # in. Which zone it meant is not recoverable, and a rebuild that let the
    # copy fail part-way -- or filtered the row out -- would be the schema
    # deciding that for the user. It stops instead, before the first DROP.
    conn = _historic_database("40f7f98")
    conn.execute(
        """INSERT INTO events (id, title, starts_at, created_at)
           VALUES (1, 'wall clock', '2026-09-02T19:00:00', ?)""",
        (_TS,),
    )
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    text = str(exc.value)
    assert "2026-09-02T19:00:00" in text
    assert "Back the database file up" in text
    assert conn.execute("SELECT starts_at FROM events").fetchall() == [
        ("2026-09-02T19:00:00",)
    ]
    assert "calendar_entries" not in _object_names(conn)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    conn.close()


def test_migrate_refuses_a_version_1_zone_value_version_2_would_not_accept() -> None:
    # 6d7659c's zone CHECK was `= 'UTC' OR instr(name, '/') > 0`, so '//' was a
    # legal value then and is not one now. Widening the guard to slashless names
    # narrowed it here, and a row already holding one is the operator's to fix.
    conn = _historic_database("6d7659c")
    conn.execute(
        """INSERT INTO calendar_entries (
             id, title, starts_at, iana_time_zone, created_at
           ) VALUES (1, 'bad zone', ?, '//', ?)""",
        (_SEED_START, _TS),
    )
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    assert "Back the database file up" in str(exc.value)
    assert conn.execute("SELECT iana_time_zone FROM calendar_entries").fetchall() == [
        ("//",)
    ]
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    conn.close()


def test_migrate_refuses_a_version_1_database_whose_shape_it_cannot_place() -> None:
    # The ledger says version 1 ran; it cannot say the table still looks like
    # version 1's output. Version 2 copies rows into a new definition, so a
    # source it has not accounted for is a source it will not read.
    conn = _historic_database("6d7659c")
    conn.execute("ALTER TABLE tool_ledger ADD COLUMN extra TEXT")
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    text = str(exc.value)
    assert "tool_ledger" in text
    assert "Back the database file up" in text
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    assert "extra" in _columns(conn, "tool_ledger")
    conn.close()


def test_migrate_refuses_a_version_1_database_missing_one_of_its_tables() -> None:
    conn = _historic_database("6d7659c")
    conn.execute("DROP TABLE trace_prunes")
    with pytest.raises(schema.SchemaVersionError, match="trace_prunes"):
        schema.migrate(conn)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    conn.close()


def test_migrate_refuses_a_version_1_database_holding_both_calendar_tables() -> None:
    # Neither name can be assumed to be the live one, and picking wrong loses a
    # table of schedules. Which to keep is the operator's call, not this code's.
    conn = _historic_database("40f7f98")
    conn.execute(historic_schema.CALENDAR_ENTRIES_TABLE)
    with pytest.raises(schema.SchemaVersionError) as exc:
        schema.migrate(conn)
    text = str(exc.value)
    assert "both `events` and `calendar_entries`" in text
    assert "Back the database file up" in text
    assert {"events", "calendar_entries"} <= _tables(conn)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    conn.close()


# ---------------------------------------------------------------------------
# A migration that fails part-way, inside and outside a caller's transaction.
# ---------------------------------------------------------------------------


def _create_then_fail(conn: sqlite3.Connection) -> None:
    """A migration that gets objects on disk and then dies.

    A registry entry rather than a hook in migrate(): the production code needs
    no way to be told to fail, and adding one would be a fault injector shipped
    to users so that a test could reach it.
    """
    conn.execute("CREATE TABLE half_applied (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE INDEX half_applied_idx ON half_applied (id)")
    raise RuntimeError("injected migration failure")


_FAILING_MIGRATION = schema.Migration(
    version=schema.LATEST_MIGRATION_VERSION + 1,
    apply=_create_then_fail,
    managed_objects=("half_applied", "half_applied_idx"),
)


def _register_failing_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        schema, "MIGRATIONS", (*schema.MIGRATIONS, _FAILING_MIGRATION)
    )


def test_a_failed_migration_in_a_caller_transaction_undoes_only_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller_notes (note TEXT)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_notes VALUES ('written before migrate')")
    _register_failing_migration(monkeypatch)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.migrate(conn)
    # The caller's transaction is still open, and still holds its own write.
    assert conn.in_transaction
    assert conn.execute("SELECT note FROM caller_notes").fetchall() == [
        ("written before migrate",)
    ]
    # Everything the migrations built is gone: the objects the failing one had
    # already created, the objects the earlier ones had created, and the ledger
    # rows for both. There is no half-schema, and no ledger claiming otherwise.
    assert _object_names(conn) == {"caller_notes"}
    # Committing is still the caller's decision, and it commits the caller's
    # work alone.
    conn.commit()
    assert _object_names(conn) == {"caller_notes"}
    assert conn.execute("SELECT note FROM caller_notes").fetchall() == [
        ("written before migrate",)
    ]
    conn.close()


def test_a_failed_upgrade_in_a_caller_transaction_leaves_the_ledger_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _migrate()
    before = _schema_snapshot(conn)
    conn.execute("BEGIN")
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('web_search', 'fp-1', 'external', 'started', ?)""",
        (_TS,),
    )
    _register_failing_migration(monkeypatch)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.migrate(conn)
    assert conn.in_transaction
    assert conn.execute("SELECT tool_name FROM tool_ledger").fetchall() == [
        ("web_search",)
    ]
    assert "half_applied" not in _object_names(conn)
    assert _schema_snapshot(conn) == before
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
        (1,),
        (2,),
        (3,),
    ]
    # Rolling back is still the caller's decision too, and it takes back the
    # caller's own write and nothing else.
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM tool_ledger").fetchone() == (0,)
    assert _schema_snapshot(conn) == before
    conn.close()


def test_a_failed_migration_without_a_caller_transaction_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _register_failing_migration(monkeypatch)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.migrate(conn)
    assert _object_names(conn) == set()
    assert not conn.in_transaction
    conn.close()
