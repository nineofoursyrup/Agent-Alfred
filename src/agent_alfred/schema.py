"""SQLite schema and idempotent migrations on an injected connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime

BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = 1
# Highest first. One deletion may match several reasons; the stored
# value is whichever of these wins, so the same set always records the same row.
PRUNE_REASON_PRIORITY = ("manual", "disk_low", "age", "capacity")
PRUNE_REASONS = frozenset(PRUNE_REASON_PRIORITY)
_PRUNE_REASON_SQL = ", ".join(f"'{reason}'" for reason in PRUNE_REASON_PRIORITY)
LEDGER_STATUSES = ("started", "succeeded", "failed", "unknown")
_LEDGER_STATUS_SQL = ", ".join(f"'{status}'" for status in LEDGER_STATUSES)
SOURCES = ("cli", "web")
_SOURCE_SQL = ", ".join(f"'{source}'" for source in SOURCES)

_FTS5_UNAVAILABLE = """\
SQLite FTS5 is not enabled in this Python interpreter's sqlite3 module \
(SQLite {sqlite_version}). Agent-Alfred needs FTS5 for semantic and \
episodic memory search.

Python 3.14 official builds (python.org, Homebrew, uv) ship with FTS5. \
If you built Python or SQLite yourself, rebuild SQLite with \
-DSQLITE_ENABLE_FTS5 and link that Python against it.

Confirm with PRAGMA compile_options; the result must include ENABLE_FTS5.
"""

_ORIGIN_COLUMNS = f"""
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('consolidation', 'manual', 'tool')
  ),
  origin_batch_id TEXT,
  origin_source TEXT CHECK (
    origin_source IS NULL OR origin_source IN ({_SOURCE_SQL})
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

_V1_STATEMENTS = (
    """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS calendar_entries (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL CHECK (
    starts_at LIKE '%Z'
    OR starts_at LIKE '%+__:__'
    OR starts_at LIKE '%-__:__'
  ),
  ends_at TEXT CHECK (
    ends_at IS NULL
    OR ends_at LIKE '%Z'
    OR ends_at LIKE '%+__:__'
    OR ends_at LIKE '%-__:__'
  ),
  iana_time_zone TEXT CHECK (
    iana_time_zone IS NULL
    OR iana_time_zone = 'UTC'
    OR instr(iana_time_zone, '/') > 0
  ),
  participants TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
)
""",
    f"""
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
)
""",
    """
CREATE INDEX IF NOT EXISTS facts_subject_idx ON facts (subject)
""",
    """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  subject,
  fact,
  content='facts',
  content_rowid='rowid',
  tokenize='unicode61'
)
""",
    """
CREATE TRIGGER IF NOT EXISTS facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END
""",
    """
CREATE TRIGGER IF NOT EXISTS facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
END
""",
    """
CREATE TRIGGER IF NOT EXISTS facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END
""",
    f"""
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
)
""",
    """
CREATE INDEX IF NOT EXISTS episodes_occurred_at_idx ON episodes (occurred_at)
""",
    """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  summary,
  content='episodes',
  content_rowid='rowid',
  tokenize='unicode61'
)
""",
    """
CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END
""",
    """
CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
END
""",
    """
CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END
""",
    f"""
CREATE TABLE IF NOT EXISTS agent_log (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL CHECK (json_valid(content)),
  consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ({_SOURCE_SQL})),
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  created_at TEXT NOT NULL
)
""",
    """
CREATE INDEX IF NOT EXISTS agent_log_session_created_idx
  ON agent_log (session_id, created_at)
""",
    f"""
CREATE TABLE IF NOT EXISTS tool_ledger (
  id INTEGER PRIMARY KEY,
  tool_name TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  effect TEXT NOT NULL CHECK (effect IN ('local_write', 'external')),
  status TEXT NOT NULL CHECK (
    status IN ({_LEDGER_STATUS_SQL})
  ),
  call_id TEXT,
  run_id TEXT,
  session_id TEXT,
  summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
)
""",
    """
CREATE INDEX IF NOT EXISTS tool_ledger_name_fingerprint_idx
  ON tool_ledger (tool_name, fingerprint)
""",
    """
CREATE INDEX IF NOT EXISTS tool_ledger_session_created_idx
  ON tool_ledger (session_id, created_at)
""",
    f"""
CREATE TABLE IF NOT EXISTS trace_prunes (
  run_id TEXT PRIMARY KEY,
  prune_requested_at TEXT NOT NULL,
  -- Observed-gone time, not an unlink instant we cannot prove.
  absence_confirmed_at TEXT NOT NULL,
  prune_reason TEXT NOT NULL CHECK (
    prune_reason IN ({_PRUNE_REASON_SQL})
  )
)
""",
    f"""
CREATE TABLE IF NOT EXISTS consolidation_batches (
  batch_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN ({_LEDGER_STATUS_SQL})
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)
""",
    f"""
CREATE TABLE IF NOT EXISTS consolidation_ops (
  op_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES consolidation_batches (batch_id),
  status TEXT NOT NULL CHECK (
    status IN ({_LEDGER_STATUS_SQL})
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)
""",
)


class Fts5UnavailableError(RuntimeError):
    """Raised when this interpreter's sqlite3 was built without FTS5."""

    def __init__(self, sqlite_version: str) -> None:
        super().__init__(_FTS5_UNAVAILABLE.format(sqlite_version=sqlite_version))
        self.sqlite_version = sqlite_version


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk schema version cannot be migrated by this code."""


def _fts5_enabled(conn: sqlite3.Connection) -> bool:
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    return "ENABLE_FTS5" in options


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _applied_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type = 'table' AND name = 'schema_migrations'"""
    ).fetchone()
    if row is None:
        return None
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return version


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade tables. Idempotent. Does not commit a caller transaction."""
    caller_owns_txn = conn.in_transaction
    configure_connection(conn)
    if not _fts5_enabled(conn):
        raise Fts5UnavailableError(sqlite3.sqlite_version)
    current = _applied_version(conn)
    if current is not None:
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {current} is newer than "
                f"this code (version {SCHEMA_VERSION})"
            )
        if current == SCHEMA_VERSION:
            return
        raise SchemaVersionError(
            f"no upgrade path from schema version {current} to {SCHEMA_VERSION}"
        )
    if not caller_owns_txn:
        conn.execute("BEGIN")
    try:
        for statement in _V1_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )
        if not caller_owns_txn:
            conn.commit()
    except Exception:
        if not caller_owns_txn and conn.in_transaction:
            conn.rollback()
        raise


def parse_instant(value: str) -> datetime:
    """Parse a stored instant. Naive values are rejected, not localized."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an aware ISO8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"naive datetime is not an instant: {value!r}")
    return parsed


def pick_prune_reason(reasons: Iterable[str]) -> str:
    """Pick the stored prune_reason: manual > disk_low > age > capacity."""
    best: str | None = None
    best_rank = len(PRUNE_REASON_PRIORITY)
    for reason in reasons:
        if reason not in PRUNE_REASONS:
            allowed = ", ".join(PRUNE_REASON_PRIORITY)
            raise ValueError(f"prune_reason must be one of {allowed}, got {reason!r}")
        rank = PRUNE_REASON_PRIORITY.index(reason)
        if rank < best_rank:
            best = reason
            best_rank = rank
    if best is None:
        raise ValueError("prune_reason must not be empty")
    return best


def record_trace_prune(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    prune_requested_at: str,
    absence_confirmed_at: str,
    prune_reason: str,
) -> None:
    prune_reason = pick_prune_reason((prune_reason,))
    conn.execute(
        """INSERT INTO trace_prunes (
             run_id, prune_requested_at, absence_confirmed_at, prune_reason
           ) VALUES (?, ?, ?, ?)
           ON CONFLICT(run_id) DO NOTHING""",
        (run_id, prune_requested_at, absence_confirmed_at, prune_reason),
    )
