"""Lock schema version 3: sessions, the Run index, the activity clock.

Every CHECK, UNIQUE, and ordering claim below has a path that turns red
when the database stops enforcing it. Reading the DDL is not that path.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_alfred import schema

_TS = "2026-08-27T12:00:00+00:00"
_BODY = json.dumps([{"type": "text", "text": "hello"}])
# Spelled from the spec, not from schema.py: an acceptance test that
# imports the implementation's closed set cannot disagree with it.
_SPEC_PHASES = ("accepted", "running", "finished")
_SPEC_OUTCOMES = ("completed", "max_steps", "failed", "interrupted")
_SPEC_PURPOSES = ("chat", "inference_probe")
_SPEC_GATEWAYS = ("cli", "web")


def _migrate() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    return conn


def _database_through_version(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    for migration in schema.MIGRATIONS:
        if migration.version > version:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (migration.version, _TS),
        )
    conn.commit()
    return conn


def _insert_log(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    text: str,
    created_at: str,
    source: str = "cli",
    telemetry: str | None = None,
    run_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at, run_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            role,
            json.dumps([{"type": "text", "text": text}]),
            source,
            telemetry,
            created_at,
            run_id,
        ),
    )


def test_fresh_migrate_creates_sessions_runs_and_activity_clock() -> None:
    conn = _migrate()
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }
    assert {"sessions", "runs", "activity_clock"} <= names
    assert conn.execute("SELECT id, next_revision FROM activity_clock").fetchall() == [
        (1, 1)
    ]
    conn.close()


def test_phase_outcome_check_rejects_every_illegal_pairing() -> None:
    conn = _migrate()
    # accepted/running + any outcome, and finished + null, are the illegal
    # combinations. Each INSERT is a real write the CHECK has to refuse.
    illegal = (
        ("accepted", "completed"),
        ("accepted", "max_steps"),
        ("accepted", "failed"),
        ("accepted", "interrupted"),
        ("running", "completed"),
        ("running", "failed"),
        ("finished", None),
    )
    for i, (phase, outcome) in enumerate(illegal):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO runs (
                     run_id, purpose, session_id, gateway, phase, outcome,
                     accepted_at, activity_revision
                   ) VALUES (?, 'chat', NULL, 'cli', ?, ?, ?, ?)""",
                (f"bad-{i}", phase, outcome, _TS, i + 1),
            )
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    conn.close()


def test_phase_outcome_check_accepts_the_legal_pairings() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO runs (
             run_id, purpose, gateway, phase, outcome, accepted_at,
             activity_revision
           ) VALUES ('r-acc', 'chat', 'cli', 'accepted', NULL, ?, 1)""",
        (_TS,),
    )
    conn.execute(
        """INSERT INTO runs (
             run_id, purpose, gateway, phase, outcome, accepted_at,
             started_at, activity_revision
           ) VALUES ('r-run', 'inference_probe', 'web', 'running', NULL, ?, ?, 2)""",
        (_TS, _TS),
    )
    for i, outcome in enumerate(_SPEC_OUTCOMES):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, outcome, accepted_at,
                 finished_at, activity_revision
               ) VALUES (?, 'chat', 'cli', 'finished', ?, ?, ?, ?)""",
            (f"r-fin-{outcome}", outcome, _TS, _TS, i + 10),
        )
    stored = {
        row[0]
        for row in conn.execute("SELECT phase FROM runs")
    }
    assert stored == set(_SPEC_PHASES)
    conn.close()


def test_runs_reject_unknown_phase_outcome_purpose_and_gateway() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, outcome, accepted_at,
                 activity_revision
               ) VALUES ('r1', 'chat', 'cli', 'pending', NULL, ?, 1)""",
            (_TS,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, outcome, accepted_at,
                 finished_at, activity_revision
               ) VALUES ('r2', 'chat', 'cli', 'finished', 'crashed', ?, ?, 1)""",
            (_TS, _TS),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, outcome, accepted_at,
                 activity_revision
               ) VALUES ('r3', 'search', 'cli', 'accepted', NULL, ?, 1)""",
            (_TS,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, gateway, phase, outcome, accepted_at,
                 activity_revision
               ) VALUES ('r4', 'chat', 'telegram', 'accepted', NULL, ?, 1)""",
            (_TS,),
        )
    conn.close()


def test_message_with_run_id_cannot_carry_telemetry() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_log(
            conn,
            session_id="s1",
            role="assistant",
            text="done",
            created_at=_TS,
            telemetry=json.dumps({"trace_incomplete": False}),
            run_id="run-1",
        )
    assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
    # Historic telemetry without a run_id still lands.
    _insert_log(
        conn,
        session_id="s1",
        role="assistant",
        text="done",
        created_at=_TS,
        telemetry=json.dumps({"trace_incomplete": False}),
        run_id=None,
    )
    assert conn.execute(
        "SELECT run_id, telemetry IS NOT NULL FROM agent_log"
    ).fetchone() == (None, 1)
    conn.close()


def test_unique_run_id_role_rejects_a_second_row_of_the_same_role() -> None:
    conn = _migrate()
    _insert_log(
        conn,
        session_id="s1",
        role="user",
        text="hi",
        created_at=_TS,
        run_id="run-1",
    )
    _insert_log(
        conn,
        session_id="s1",
        role="assistant",
        text="hello",
        created_at=_TS,
        run_id="run-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_log(
            conn,
            session_id="s1",
            role="user",
            text="again",
            created_at=_TS,
            run_id="run-1",
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_log(
            conn,
            session_id="s1",
            role="assistant",
            text="again",
            created_at=_TS,
            run_id="run-1",
        )
    # Two historic user rows with a null run_id stay legal: the unique
    # index is partial.
    _insert_log(conn, session_id="s1", role="user", text="old-a", created_at=_TS)
    _insert_log(conn, session_id="s1", role="user", text="old-b", created_at=_TS)
    roles = [
        row[0]
        for row in conn.execute(
            "SELECT role FROM agent_log WHERE run_id = 'run-1' ORDER BY role"
        )
    ]
    assert roles == ["assistant", "user"]
    conn.close()


def test_sessions_accept_historic_identifiers_that_are_not_server_issued() -> None:
    conn = _migrate()
    for i, session_id in enumerate(
        ("not-a-uuid", "会话-1", "", "spaces in id", "a" * 200)
    ):
        conn.execute(
            """INSERT INTO sessions (session_id, created_at, activity_revision)
               VALUES (?, ?, ?)""",
            (session_id, "not-an-instant", i + 1),
        )
    stored = {
        row[0] for row in conn.execute("SELECT session_id FROM sessions")
    }
    assert stored == {"not-a-uuid", "会话-1", "", "spaces in id", "a" * 200}
    conn.close()


def test_sessions_reject_a_second_row_with_the_same_activity_revision() -> None:
    conn = _migrate()
    conn.execute(
        """INSERT INTO sessions (session_id, created_at, activity_revision)
           VALUES ('a', ?, 1)""",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO sessions (session_id, created_at, activity_revision)
               VALUES ('b', ?, 1)""",
            (_TS,),
        )
    conn.close()


def test_activity_clock_is_a_single_row_and_allocates_consecutive_revisions() -> None:
    conn = _migrate()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO activity_clock (id, next_revision) VALUES (2, 1)")
    first = schema.allocate_activity_revision(conn)
    second = schema.allocate_activity_revision(conn)
    assert (first, second) == (1, 2)
    assert conn.execute("SELECT next_revision FROM activity_clock").fetchone() == (3,)
    conn.close()


def test_update_run_phase_must_affect_exactly_one_row() -> None:
    conn = _migrate()
    schema.insert_session(conn, session_id="s1", created_at=_TS)
    schema.insert_accepted_run(
        conn,
        run_id="run-1",
        purpose="chat",
        session_id="s1",
        gateway="cli",
        accepted_at=_TS,
    )
    with pytest.raises(schema.RunPhaseError, match="exactly 1 run"):
        schema.update_run_phase(
            conn,
            run_id="run-1",
            from_phase="running",
            to_phase="finished",
            activity_revision=99,
            outcome="completed",
            finished_at=_TS,
            session_id="s1",
        )
    phase = conn.execute("SELECT phase, outcome FROM runs").fetchone()
    assert phase == ("accepted", None)
    schema.update_run_phase(
        conn,
        run_id="run-1",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=_TS,
        session_id="s1",
    )
    assert conn.execute("SELECT phase, outcome FROM runs").fetchone() == (
        "running",
        None,
    )
    conn.close()


def test_insert_accepted_run_writes_null_outcome_and_stamps_the_session() -> None:
    conn = _migrate()
    schema.insert_session(conn, session_id="s1", created_at=_TS)
    rev = schema.insert_accepted_run(
        conn,
        run_id="run-1",
        purpose="chat",
        session_id="s1",
        gateway="cli",
        accepted_at=_TS,
        prompt_preview="hello",
    )
    row = conn.execute(
        """SELECT phase, outcome, session_id, activity_revision, prompt_preview
           FROM runs WHERE run_id = 'run-1'"""
    ).fetchone()
    assert row == ("accepted", None, "s1", rev, "hello")
    assert conn.execute(
        "SELECT activity_revision FROM sessions WHERE session_id = 's1'"
    ).fetchone() == (rev,)
    conn.close()


def test_v3_backfills_sessions_verbatim_from_a_real_v2_database() -> None:
    conn = _database_through_version(2)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
        (1,),
        (2,),
    ]
    assert "sessions" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master")
    }
    # Three sessions. The first has a later-looking created_at on the
    # *second* row, so MIN(created_at) would pick the wrong text.
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES (?, 'user', ?, 'cli', ?, ?)""",
        ("weird id", _BODY, None, "zzz-not-min-as-text"),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES (?, 'assistant', ?, 'cli', ?, ?)""",
        (
            "weird id",
            json.dumps([{"type": "text", "text": "reply"}]),
            json.dumps({"legacy": True, "trace_incomplete": False}),
            "aaa-would-win-min-text",
        ),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES (?, 'user', ?, 'web', NULL, ?)""",
        ("会话-2", json.dumps([{"type": "text", "text": "second"}]), "not-iso"),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES (?, 'user', ?, 'cli', NULL, ?)""",
        ("s-old", json.dumps([{"type": "text", "text": "oldest"}]), "2020-01-01"),
    )
    before_log = conn.execute("SELECT * FROM agent_log ORDER BY id").fetchall()
    schema.migrate(conn)
    after_log = conn.execute(
        """SELECT id, session_id, role, content, consolidated, source,
                  telemetry, created_at FROM agent_log ORDER BY id"""
    ).fetchall()
    assert after_log == before_log
    assert conn.execute("SELECT run_id FROM agent_log").fetchall() == [
        (None,),
        (None,),
        (None,),
        (None,),
    ]
    sessions = conn.execute(
        """SELECT session_id, created_at, activity_revision
           FROM sessions ORDER BY activity_revision"""
    ).fetchall()
    # max(id) order: s-old (id 4? wait) let's compute from actual ids.
    # Insert order: weird id, weird id, 会话-2, s-old.
    # max_id: weird id has 2, 会话-2 has 3, s-old has 4.
    # oldest max_id first: weird id rev 1, 会话-2 rev 2, s-old rev 3.
    assert sessions == [
        ("weird id", "zzz-not-min-as-text", 1),
        ("会话-2", "not-iso", 2),
        ("s-old", "2020-01-01", 3),
    ]
    revisions = [row[2] for row in sessions]
    assert revisions == sorted(set(revisions))
    # Inbox visibility: newest activity first, and opening the historic
    # session still returns every original message.
    inbox = conn.execute(
        """SELECT session_id FROM sessions
           ORDER BY activity_revision DESC, session_id DESC"""
    ).fetchall()
    assert inbox == [("s-old",), ("会话-2",), ("weird id",)]
    opened = conn.execute(
        """SELECT json_extract(content, '$[0].text'), telemetry
           FROM agent_log WHERE session_id = ? ORDER BY id""",
        ("weird id",),
    ).fetchall()
    assert opened == [
        ("hello", None),
        ("reply", json.dumps({"legacy": True, "trace_incomplete": False})),
    ]
    clock_after = conn.execute("SELECT next_revision FROM activity_clock").fetchone()
    schema.migrate(conn)
    assert conn.execute("SELECT next_revision FROM activity_clock").fetchone() == (
        clock_after
    )
    assert conn.execute(
        "SELECT session_id, created_at, activity_revision"
        " FROM sessions ORDER BY activity_revision"
    ).fetchall() == sessions
    conn.close()


def test_v3_does_not_invent_runs_for_historic_messages() -> None:
    conn = _database_through_version(2)
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES ('s1', 'assistant', ?, 'cli', ?, ?)""",
        (_BODY, json.dumps({"trace_incomplete": True}), _TS),
    )
    schema.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    conn.close()


def test_v3_refuses_a_version_2_agent_log_whose_columns_it_does_not_know() -> None:
    conn = _database_through_version(2)
    conn.execute("ALTER TABLE agent_log ADD COLUMN extra TEXT")
    with pytest.raises(schema.SchemaVersionError, match="agent_log"):
        schema.migrate(conn)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
        (1,),
        (2,),
    ]
    assert "extra" in [row[1] for row in conn.execute("PRAGMA table_info(agent_log)")]
    conn.close()


def test_trace_prune_does_not_delete_runs_or_advance_the_activity_clock() -> None:
    conn = _migrate()
    schema.insert_session(conn, session_id="s1", created_at=_TS)
    schema.insert_accepted_run(
        conn,
        run_id="run-keep",
        purpose="chat",
        session_id="s1",
        gateway="cli",
        accepted_at=_TS,
    )
    _insert_log(
        conn,
        session_id="s1",
        role="user",
        text="hi",
        created_at=_TS,
        run_id="run-keep",
    )
    clock = conn.execute("SELECT * FROM activity_clock").fetchall()
    runs = conn.execute("SELECT * FROM runs").fetchall()
    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    logs = conn.execute("SELECT * FROM agent_log").fetchall()
    schema.record_trace_prune(
        conn,
        run_id="run-keep",
        prune_requested_at=_TS,
        absence_confirmed_at=_TS,
        prune_reason="age",
    )
    assert conn.execute("SELECT * FROM activity_clock").fetchall() == clock
    assert conn.execute("SELECT * FROM runs").fetchall() == runs
    assert conn.execute("SELECT * FROM sessions").fetchall() == sessions
    assert conn.execute("SELECT * FROM agent_log").fetchall() == logs
    assert conn.execute("SELECT run_id FROM trace_prunes").fetchone() == ("run-keep",)
    conn.close()


def test_system_run_can_store_telemetry_without_a_session_or_message() -> None:
    conn = _migrate()
    rev = schema.insert_accepted_run(
        conn,
        run_id="probe-1",
        purpose="inference_probe",
        session_id=None,
        gateway="web",
        accepted_at=_TS,
    )
    finished_rev = schema.allocate_activity_revision(conn)
    schema.update_run_phase(
        conn,
        run_id="probe-1",
        from_phase="accepted",
        to_phase="finished",
        activity_revision=finished_rev,
        outcome="completed",
        finished_at=_TS,
        telemetry=json.dumps({"attempts": [{"attempt_id": "a1"}]}),
    )
    row = conn.execute(
        """SELECT purpose, session_id, phase, outcome, telemetry, activity_revision
           FROM runs WHERE run_id = 'probe-1'"""
    ).fetchone()
    assert row[0] == "inference_probe"
    assert row[1] is None
    assert row[2] == "finished"
    assert row[3] == "completed"
    assert json.loads(row[4]) == {"attempts": [{"attempt_id": "a1"}]}
    assert row[5] == finished_rev != rev
    assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
    conn.close()
