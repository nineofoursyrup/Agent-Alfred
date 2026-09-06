"""Historic v3 read/upgrade fixture, frozen from 0b4b017's SQLite DDL.

Only this repository's schema is captured. v1/v2 have their own independent
historic matrix; this fixture freezes the v3 additions without importing the
production v3 migration that v4 must preserve. The v2 message table is empty.
"""

import sqlite3

from agent_alfred import schema

_V3_DDL = (
    """CREATE TABLE agent_log (
        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL CHECK (json_valid(content)),
        consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
        source TEXT NOT NULL CHECK (source IN ('cli', 'web')),
        telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
        created_at TEXT NOT NULL, run_id TEXT,
        CHECK (run_id IS NULL OR telemetry IS NULL))""",
    """CREATE INDEX agent_log_session_created_idx
        ON agent_log (session_id, created_at)""",
    """CREATE UNIQUE INDEX agent_log_run_role_unique
        ON agent_log (run_id, role) WHERE run_id IS NOT NULL""",
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        activity_revision INTEGER NOT NULL UNIQUE)""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        purpose TEXT NOT NULL CHECK (purpose IN ('chat', 'inference_probe')),
        session_id TEXT,
        gateway TEXT NOT NULL CHECK (gateway IN ('cli', 'web')),
        entry_surface_id TEXT, prompt_preview TEXT,
        phase TEXT NOT NULL CHECK (phase IN ('accepted', 'running', 'finished')),
        outcome TEXT CHECK (
            outcome IS NULL OR outcome IN
            ('completed', 'max_steps', 'failed', 'interrupted')),
        accepted_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
        activity_revision INTEGER NOT NULL,
        telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
        CHECK ((phase IN ('accepted', 'running') AND outcome IS NULL)
            OR (phase = 'finished' AND outcome IS NOT NULL)))""",
    """CREATE INDEX runs_activity_revision_idx
        ON runs (activity_revision, run_id)""",
    """CREATE TABLE activity_clock (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_revision INTEGER NOT NULL CHECK (next_revision >= 1))""",
)


def v3_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.configure_connection(conn)
    for migration in schema.MIGRATIONS:
        if migration.version > 2:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, 'historic migration time')",
            (migration.version,),
        )
    conn.execute("DROP TABLE agent_log")
    for statement in _V3_DDL:
        conn.execute(statement)
    conn.execute("INSERT INTO activity_clock VALUES (1, 1)")
    conn.execute("INSERT INTO schema_migrations VALUES (3, 'historic migration time')")
    conn.commit()
    return conn
