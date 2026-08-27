"""SQLite schema and idempotent migrations on an injected connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = 1
PRUNE_REASONS = frozenset({"manual", "disk_low", "age", "capacity"})

_FTS5_UNAVAILABLE = """\
SQLite FTS5 is not enabled in this Python interpreter's sqlite3 module \
(SQLite {sqlite_version}). Agent-Alfred needs FTS5 for semantic and \
episodic memory search.

Python 3.14 official builds (python.org, Homebrew, uv) ship with FTS5. \
If you built Python or SQLite yourself, rebuild SQLite with \
-DSQLITE_ENABLE_FTS5 and link that Python against it.

Confirm with PRAGMA compile_options; the result must include ENABLE_FTS5.
"""

_ORIGIN_COLUMNS = """
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('consolidation', 'manual', 'tool')
  ),
  origin_batch_id TEXT,
  origin_source TEXT CHECK (
    origin_source IS NULL OR origin_source IN ('cli', 'web')
  ),
  origin_call_id TEXT
"""

_ORIGIN_CHECK = """
  CHECK (
    (
      origin_kind = 'consolidation'
      AND origin_batch_id IS NOT NULL
      AND origin_source IS NULL
      AND origin_call_id IS NULL
    )
    OR (
      origin_kind = 'manual'
      AND origin_source IS NOT NULL
      AND origin_batch_id IS NULL
      AND origin_call_id IS NULL
    )
    OR (
      origin_kind = 'tool'
      AND origin_call_id IS NOT NULL
      AND origin_batch_id IS NULL
      AND origin_source IS NULL
    )
  )
"""

_SCHEMA_SQL = f"""
BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT,
  participants TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  fact TEXT NOT NULL,
  {_ORIGIN_COLUMNS},
  created_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  key_id TEXT NOT NULL,
  normalization_version INTEGER NOT NULL,
  {_ORIGIN_CHECK}
);

CREATE INDEX IF NOT EXISTS facts_subject_idx ON facts (subject);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  subject,
  fact,
  content='facts',
  content_rowid='rowid',
  tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END;

CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  occurred_until TEXT,
  {_ORIGIN_COLUMNS},
  created_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  key_id TEXT NOT NULL,
  normalization_version INTEGER NOT NULL,
  {_ORIGIN_CHECK}
);

CREATE INDEX IF NOT EXISTS episodes_occurred_at_idx ON episodes (occurred_at);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  summary,
  content='episodes',
  content_rowid='rowid',
  tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END;

CREATE TABLE IF NOT EXISTS agent_log (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL CHECK (json_valid(content)),
  consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ('cli', 'web')),
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_log_session_created_idx
  ON agent_log (session_id, created_at);

CREATE TABLE IF NOT EXISTS tool_ledger (
  id INTEGER PRIMARY KEY,
  tool_name TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  effect TEXT NOT NULL CHECK (effect IN ('local_write', 'external')),
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'unknown')
  ),
  call_id TEXT,
  run_id TEXT,
  session_id TEXT,
  summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS tool_ledger_name_fingerprint_idx
  ON tool_ledger (tool_name, fingerprint);

CREATE INDEX IF NOT EXISTS tool_ledger_session_created_idx
  ON tool_ledger (session_id, created_at);

CREATE TABLE IF NOT EXISTS trace_prunes (
  run_id TEXT PRIMARY KEY,
  prune_requested_at TEXT NOT NULL,
  absence_confirmed_at TEXT NOT NULL,
  prune_reason TEXT NOT NULL CHECK (
    prune_reason IN ('manual', 'disk_low', 'age', 'capacity')
  )
);

CREATE TABLE IF NOT EXISTS consolidation_batches (
  batch_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'unknown')
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS consolidation_ops (
  op_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES consolidation_batches (batch_id),
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'unknown')
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
);

INSERT OR IGNORE INTO schema_migrations (version, applied_at)
VALUES ({SCHEMA_VERSION}, datetime('now'));

COMMIT;
"""


class Fts5UnavailableError(RuntimeError):
    """Raised when this interpreter's sqlite3 was built without FTS5."""

    def __init__(self, sqlite_version: str) -> None:
        super().__init__(_FTS5_UNAVAILABLE.format(sqlite_version=sqlite_version))
        self.sqlite_version = sqlite_version


def _fts5_enabled(conn: sqlite3.Connection) -> bool:
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    return "ENABLE_FTS5" in options


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    return configure_connection(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade tables. Idempotent. Commits via executescript."""
    configure_connection(conn)
    if not _fts5_enabled(conn):
        raise Fts5UnavailableError(sqlite3.sqlite_version)
    conn.executescript(_SCHEMA_SQL)


def record_trace_prune(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    prune_requested_at: str,
    absence_confirmed_at: str,
    prune_reason: str,
) -> None:
    if prune_reason not in PRUNE_REASONS:
        allowed = ", ".join(sorted(PRUNE_REASONS))
        raise ValueError(f"prune_reason must be one of {allowed}, got {prune_reason!r}")
    conn.execute(
        """INSERT OR IGNORE INTO trace_prunes (
             run_id, prune_requested_at, absence_confirmed_at, prune_reason
           ) VALUES (?, ?, ?, ?)""",
        (run_id, prune_requested_at, absence_confirmed_at, prune_reason),
    )
