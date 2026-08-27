"""Lock SQLite schema and idempotent migrations on an injected connection."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_alfred import schema

_TS = "2026-08-27T12:00:00+00:00"


def _migrate() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    return {name for name in names if not name.startswith("sqlite_")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _schema_snapshot(conn: sqlite3.Connection) -> tuple:
    master = tuple(
        conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " ORDER BY type, name"
        ).fetchall()
    )
    tables = sorted(
        name
        for name in _tables(conn)
        if not name.startswith("facts_fts") and not name.startswith("episodes_fts")
    )
    columns = tuple((table, tuple(_columns(conn, table))) for table in tables)
    return master, columns


def test_python_sqlite_has_fts5() -> None:
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


def test_connect_sets_busy_timeout(tmp_path) -> None:
    conn = schema.connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == schema.BUSY_TIMEOUT_MS
    conn.close()


def test_migrate_creates_required_tables_without_user_id() -> None:
    conn = _migrate()
    names = _tables(conn)
    required = {
        "schema_migrations",
        "events",
        "facts",
        "facts_fts",
        "episodes",
        "episodes_fts",
        "agent_log",
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
    conn = _migrate()
    columns = _columns(conn, "agent_log")
    assert "session_id" in columns
    assert "user_id" not in columns
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'user', ?, 'cli', ?)""",
        ("s1", json.dumps([{"type": "text", "text": "hi"}]), _TS),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, created_at
           ) VALUES (?, 'user', ?, 'web', ?)""",
        ("s2", json.dumps([{"type": "text", "text": "yo"}]), _TS),
    )
    rows = conn.execute(
        "SELECT session_id FROM agent_log WHERE session_id = ?", ("s1",)
    ).fetchall()
    assert rows == [("s1",)]
    conn.close()


def test_migrate_twice_leaves_identical_schema() -> None:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    first = _schema_snapshot(conn)
    schema.migrate(conn)
    second = _schema_snapshot(conn)
    conn.close()
    assert first == second


def test_events_table_has_title_start_end_participants_notes() -> None:
    conn = _migrate()
    columns = _columns(conn, "events")
    for name in (
        "title",
        "starts_at",
        "ends_at",
        "participants",
        "notes",
        "created_at",
    ):
        assert name in columns
    conn.close()


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

    conn.execute(
        "UPDATE episodes SET summary = 'lunch with cara' WHERE id = 'e1'"
    )
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
        "tools": [{"call_id": "c1", "name": "create_event", "status": "succeeded"}],
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
    assert loaded["tools"][0]["name"] == "create_event"
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
           ) VALUES ('create_event', 'fp-1', 'local_write', 'succeeded', ?)""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('web_search', 'fp-2', 'external', 'started', ?)""",
        (_TS,),
    )
    index_columns = []
    for row in conn.execute("PRAGMA index_list(tool_ledger)"):
        name = row[1]
        cols = [
            info[2] for info in conn.execute(f"PRAGMA index_info({name})")
        ]
        index_columns.append(cols)
    assert any(
        cols[:2] == ["tool_name", "fingerprint"] for cols in index_columns
    )
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


def test_trace_prunes_have_no_foreign_key_to_agent_log() -> None:
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


def test_local_write_ledger_row_rolls_back_with_the_business_write() -> None:
    conn = _migrate()
    conn.execute("BEGIN")
    conn.execute(
        """INSERT INTO events (title, starts_at, created_at)
           VALUES ('dinner', ?, ?)""",
        (_TS, _TS),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('create_event', 'fp', 'local_write', 'succeeded', ?)""",
        (_TS,),
    )
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM tool_ledger").fetchone() == (0,)
    conn.close()


def test_tool_ledger_keeps_two_rows_with_the_same_fingerprint() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('create_event', 'same-fp', 'local_write', 'succeeded', ?)""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('create_event', 'same-fp', 'local_write', 'succeeded', ?)""",
        (_TS,),
    )
    count = conn.execute("SELECT COUNT(*) FROM tool_ledger").fetchone()[0]
    assert count == 2
    conn.close()


def test_external_ledger_row_can_move_from_started_to_a_terminal_status() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO tool_ledger (
             tool_name, fingerprint, effect, status, created_at
           ) VALUES ('web_search', 'q', 'external', 'started', ?)""",
        (_TS,),
    )
    conn.execute(
        "UPDATE tool_ledger SET status = 'unknown',"
        " updated_at = ? WHERE fingerprint = 'q'",
        (_TS,),
    )
    assert conn.execute("SELECT status FROM tool_ledger").fetchone() == ("unknown",)
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
        prune_reason=("age", "capacity", "manual"),
    )
    assert conn.execute("SELECT prune_reason FROM trace_prunes").fetchone() == (
        "manual",
    )
    conn.close()


def test_pick_prune_reason_takes_manual_over_disk_low_over_age_over_capacity() -> None:
    assert (
        schema.pick_prune_reason(("age", "manual", "capacity", "disk_low"))
        == "manual"
    )
    assert schema.pick_prune_reason(("capacity", "age")) == "age"
    assert schema.pick_prune_reason(("capacity", "disk_low")) == "disk_low"
    assert schema.pick_prune_reason(("capacity",)) == "capacity"


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


def test_migrate_raises_when_fts5_is_missing() -> None:
    inner = sqlite3.connect(":memory:", timeout=0)

    class NoFts5:
        def execute(self, sql, *args, **kwargs):
            if "compile_options" in sql.lower():
                return [("THREADSAFE=1",)]
            return inner.execute(sql, *args, **kwargs)

        def executescript(self, sql):
            raise AssertionError("schema must not be applied without FTS5")

    with pytest.raises(schema.Fts5UnavailableError) as exc:
        schema.migrate(NoFts5())
    text = str(exc.value)
    assert "ENABLE_FTS5" in text
    assert sqlite3.sqlite_version in text
    assert "python.org" in text or "Homebrew" in text or "rebuild" in text.lower()
    inner.close()
