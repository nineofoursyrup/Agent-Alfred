"""Version 1 schemas as three published commits structured them.

A **normalized structural baseline**, not a byte-for-byte transcript. Each
statement started as a capture -- that commit's `schema.py` checked out of git,
its `migrate()` run against a fresh `:memory:` database, `sqlite_master.sql`
read back (FTS5 shadow tables omitted; SQLite materialises those from the
`CREATE VIRTUAL TABLE` above) -- and was then laid out for a reader. What is
guaranteed is the shape: table and index names, columns, types, constraints,
CHECK bodies, trigger bodies, and creation order. Exact whitespace is not, and
five of `6d7659c`'s `CREATE INDEX` statements are known to differ from the live
text by a trailing newline.

That is enough for what the file is for. Every comparison against it runs
through the test module's own normaliser, which collapses whitespace and strips
comments, and the upgrade matrix builds real databases by executing these
statements -- where SQLite cares about structure and nothing else. To audit
this file, re-capture from those commits and compare **normalized**; a raw diff
will report layout the tests never claimed.

They live here rather than being read out of git at test time: a test that
shells out to `git show` proves nothing on a shallow clone or an exported
tarball, and would start passing vacuously the day the history is rewritten.

They are also deliberately *not* imported from `agent_alfred.schema`. The whole
point of the upgrade matrix is to check the module's idea of what version 1 was
against an independent record of what version 1 actually was.

Source commits, all of which stamped their (different) schema as version 1:

- `40f7f98` "Add idempotent SQLite schema and FTS5 migrations."
- `ae253b2` "Honor #12 comment invariants for ledger and prune reasons."
- `6d7659c` "Honor the #12 bounce-back for migrate, ledger tests, and IANA zone."

Diffed pairwise, the three differ in exactly two statements, spelled out below;
the other twenty are shared verbatim by construction.
"""

from __future__ import annotations

# A representative `applied_at` in the historic format: all three commits wrote
# `datetime('now')`, whose output SQLite documents as UTC and renders naive and
# space-separated. The value is 40f7f98's own commit time in UTC.
V1_APPLIED_AT = "2026-08-27 08:28:35"

# The calendar table before the glossary rename (40f7f98, ae253b2). No zone
# column, and no CHECK at all on starts_at / ends_at.
EVENTS_TABLE = """\
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT,
  participants TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
)\
"""

# The calendar table after the rename (6d7659c): a zone column, a suffix-only
# instant CHECK, and a zone CHECK that demanded an interior '/'.
CALENDAR_ENTRIES_TABLE = """\
CREATE TABLE calendar_entries (
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
)\
"""

# trace_prunes as first created (40f7f98).
TRACE_PRUNES_TABLE_40F7F98 = """\
CREATE TABLE trace_prunes (
  run_id TEXT PRIMARY KEY,
  prune_requested_at TEXT NOT NULL,
  absence_confirmed_at TEXT NOT NULL,
  prune_reason TEXT NOT NULL CHECK (
    prune_reason IN ('manual', 'disk_low', 'age', 'capacity')
  )
)\
"""

# The same table one SQL comment later (ae253b2, unchanged in 6d7659c). The
# comment is the entire DDL difference between those two commits.
TRACE_PRUNES_TABLE_LATER = """\
CREATE TABLE trace_prunes (
  run_id TEXT PRIMARY KEY,
  prune_requested_at TEXT NOT NULL,
  -- Observed-gone time, not an unlink instant we cannot prove.
  absence_confirmed_at TEXT NOT NULL,
  prune_reason TEXT NOT NULL CHECK (
    prune_reason IN ('manual', 'disk_low', 'age', 'capacity')
  )
)\
"""

# Byte-identical in all three commits. Split into the runs that sit before,
# between and after the two statements the commits disagree on, so a variant
# is assembled by naming its parts rather than by inserting at an ordinal.
_BEFORE_CALENDAR = (
    """\
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)\
""",
)

_BETWEEN_CALENDAR_AND_TRACE_PRUNES = (
    """\
CREATE TABLE facts (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  fact TEXT NOT NULL,

  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('consolidation', 'manual', 'tool')
  ),
  origin_batch_id TEXT,
  origin_source TEXT CHECK (
    origin_source IS NULL OR origin_source IN ('cli', 'web')
  ),
  origin_call_id TEXT
,
  created_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  key_id TEXT NOT NULL,
  normalization_version INTEGER NOT NULL,

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

)\
""",
    """\
CREATE INDEX facts_subject_idx ON facts (subject)\
""",
    """\
CREATE VIRTUAL TABLE facts_fts USING fts5(
  subject,
  fact,
  content='facts',
  content_rowid='rowid',
  tokenize='unicode61'
)\
""",
    """\
CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END\
""",
    """\
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
END\
""",
    """\
CREATE TRIGGER facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END\
""",
    """\
CREATE TABLE episodes (
  id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  occurred_until TEXT,

  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('consolidation', 'manual', 'tool')
  ),
  origin_batch_id TEXT,
  origin_source TEXT CHECK (
    origin_source IS NULL OR origin_source IN ('cli', 'web')
  ),
  origin_call_id TEXT
,
  created_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  key_id TEXT NOT NULL,
  normalization_version INTEGER NOT NULL,

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

)\
""",
    """\
CREATE INDEX episodes_occurred_at_idx ON episodes (occurred_at)\
""",
    """\
CREATE VIRTUAL TABLE episodes_fts USING fts5(
  summary,
  content='episodes',
  content_rowid='rowid',
  tokenize='unicode61'
)\
""",
    """\
CREATE TRIGGER episodes_fts_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END\
""",
    """\
CREATE TRIGGER episodes_fts_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
END\
""",
    """\
CREATE TRIGGER episodes_fts_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END\
""",
    """\
CREATE TABLE agent_log (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL CHECK (json_valid(content)),
  consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ('cli', 'web')),
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  created_at TEXT NOT NULL
)\
""",
    """\
CREATE INDEX agent_log_session_created_idx
  ON agent_log (session_id, created_at)\
""",
    """\
CREATE TABLE tool_ledger (
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
)\
""",
    """\
CREATE INDEX tool_ledger_name_fingerprint_idx
  ON tool_ledger (tool_name, fingerprint)\
""",
    """\
CREATE INDEX tool_ledger_session_created_idx
  ON tool_ledger (session_id, created_at)\
""",
)

_AFTER_TRACE_PRUNES = (
    """\
CREATE TABLE consolidation_batches (
  batch_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'unknown')
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)\
""",
    """\
CREATE TABLE consolidation_ops (
  op_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES consolidation_batches (batch_id),
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'unknown')
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)\
""",
)

# Creation order is load-bearing -- an FTS table and its triggers cannot be
# created before the table they read -- so a variant is spliced, never sorted.
def _v1(calendar_table: str, trace_prunes_table: str) -> tuple[str, ...]:
    return (
        *_BEFORE_CALENDAR,
        calendar_table,
        *_BETWEEN_CALENDAR_AND_TRACE_PRUNES,
        trace_prunes_table,
        *_AFTER_TRACE_PRUNES,
    )


V1_40F7F98 = _v1(EVENTS_TABLE, TRACE_PRUNES_TABLE_40F7F98)
V1_AE253B2 = _v1(EVENTS_TABLE, TRACE_PRUNES_TABLE_LATER)
V1_6D7659C = _v1(CALENDAR_ENTRIES_TABLE, TRACE_PRUNES_TABLE_LATER)

# Keyed by the commit that published the shape, so a failure names the database
# it came from rather than an index.
V1_SCHEMAS = {
    "40f7f98": V1_40F7F98,
    "ae253b2": V1_AE253B2,
    "6d7659c": V1_6D7659C,
}
