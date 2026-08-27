"""SQLite schema and versioned migrations on an injected connection."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import NamedTuple

BUSY_TIMEOUT_MS = 5000
# Highest first. One deletion may match several reasons; the stored
# value is whichever of these wins, so the same set always records the same row.
PRUNE_REASON_PRIORITY = ("manual", "disk_low", "age", "capacity")
PRUNE_REASONS = frozenset(PRUNE_REASON_PRIORITY)
_PRUNE_REASON_SQL = ", ".join(f"'{reason}'" for reason in PRUNE_REASON_PRIORITY)
# Two separate closed sets that happen to hold the same words today. The tool
# ledger's states come from #4 §7; the consolidator's come from ADR-0009. Sharing
# one constant would make a future edit to the tool ledger silently rewrite what
# consolidation is allowed to record.
LEDGER_STATUSES = ("started", "succeeded", "failed", "unknown")
_LEDGER_STATUS_SQL = ", ".join(f"'{status}'" for status in LEDGER_STATUSES)
CONSOLIDATION_STATUSES = ("started", "succeeded", "failed", "unknown")
_CONSOLIDATION_STATUS_SQL = ", ".join(f"'{s}'" for s in CONSOLIDATION_STATUSES)
SOURCES = ("cli", "web")
_SOURCE_SQL = ", ".join(f"'{source}'" for source in SOURCES)
ROLES = ("user", "assistant")
_ROLE_SQL = ", ".join(f"'{role}'" for role in ROLES)
# #4 §7's effect ladder, minus local_read: a read has nothing to account for, so
# the ledger's CHECK is the closed set of the effects that DO get a row.
LEDGERED_EFFECTS = ("local_write", "external")
_LEDGERED_EFFECT_SQL = ", ".join(f"'{effect}'" for effect in LEDGERED_EFFECTS)
# Each origin kind, and the one column that must be present for it. Both the
# closed set and the exclusivity CHECK below are generated from this map, so a
# fourth kind cannot be added to one of them and forgotten in the other.
ORIGIN_REQUIRED_COLUMN = {
    "consolidation": "origin_batch_id",
    "manual": "origin_source",
    "tool": "origin_call_id",
}
ORIGIN_KINDS = tuple(ORIGIN_REQUIRED_COLUMN)
_ORIGIN_KIND_SQL = ", ".join(f"'{kind}'" for kind in ORIGIN_KINDS)

_FTS5_UNAVAILABLE = """\
SQLite FTS5 is not enabled in this Python interpreter's sqlite3 module \
(SQLite {sqlite_version}). Agent-Alfred needs FTS5 for semantic and \
episodic memory search.

Python 3.14 official builds (python.org, Homebrew, uv) ship with FTS5. \
If you built Python or SQLite yourself, rebuild SQLite with \
-DSQLITE_ENABLE_FTS5 and link that Python against it.

Confirm with PRAGMA compile_options; the result must include ENABLE_FTS5.
"""

_UNVERSIONED_DATABASE = """\
Refusing to migrate: this database has no schema_migrations ledger, yet it \
already holds objects this schema owns: {objects}.

Registering it as version {version} would vouch for a shape nothing verified -- \
an old table whose columns have since drifted would be left exactly as it is \
and the run would report success. Back the database file up, export whatever \
rows you still need, and point migrate() at a fresh database.
"""

# Guards calendar_entries.starts_at/ends_at ONLY. The other *_at columns in this
# schema carry no such CHECK at all -- 'banana' goes into facts.created_at
# without complaint -- so do not read this as a module-wide instant guarantee.
# Even where it applies the CHECK is a LIMITED format-and-parsability guard,
# not a validator: it requires a trailing
# UTC designator or numeric offset (so a naive local wall clock cannot be
# mistaken for an instant) and requires SQLite's own date parser to accept the
# string (so 'garbageZ', 'Z', 'garbage+08:00' and '2026-13-99T99:99:99Z' are
# rejected). It does NOT check that the offset is the right one for any zone,
# SQLite normalises out-of-range days ('2026-02-30' becomes 2026-03-02) rather
# than rejecting them, and SQLite's parser also accepts a bare time with no date
# ('12:00:00Z'). Whatever guarantees more than this is not in the database.
_OFFSET_SUFFIX = """\
    {column} LIKE '%Z'
    OR {column} LIKE '%+__:__'
    OR {column} LIKE '%-__:__'\
"""


def _instant_check(column: str) -> str:
    suffix = _OFFSET_SUFFIX.format(column=column)
    return f"""(
    ({suffix})
    AND datetime({column}) IS NOT NULL
  )"""


# A LIMITED shape guard for an IANA zone name, and nothing more.
#
# What it requires: a leading ASCII letter, characters drawn only from
# [A-Za-z0-9_+/-], no trailing '/' and no empty path segment. That admits every
# shape tzdata actually uses -- the slashless keys ('UTC', 'GMT', 'CET', 'EST',
# 'Factory', 'CST6CDT', 'W-SU'), the one-slash keys ('Asia/Shanghai'), the
# sign-bearing ones ('Etc/GMT+8') and the three-part ones
# ('America/Argentina/Buenos_Aires'). An earlier revision demanded an interior
# '/' for anything but 'UTC', which rejected every slashless key ZoneInfo can
# load; that was a false negative, not a stricter guard.
#
# What it does NOT do: consult tzdata. A well-formed invention ('Narnia',
# 'Mars/Olympus_Mons') passes, because the database has no zone table to check
# against. Whether a name really exists is settled where the value is written,
# by handing it to ZoneInfo -- not here. It says nothing about recurrence rules.
_IANA_ZONE_CHECK = """(
    iana_time_zone GLOB '[A-Za-z]*'
    AND iana_time_zone NOT GLOB '*[^A-Za-z0-9_+/-]*'
    AND iana_time_zone NOT LIKE '%/'
    AND iana_time_zone NOT LIKE '%//%'
  )"""

_ORIGIN_COLUMNS = f"""
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ({_ORIGIN_KIND_SQL})
  ),
  origin_batch_id TEXT,
  origin_source TEXT CHECK (
    origin_source IS NULL OR origin_source IN ({_SOURCE_SQL})
  ),
  origin_call_id TEXT
"""



def _origin_arm(kind: str) -> str:
    """One arm of the exclusivity CHECK: this kind's column set, and no other's."""
    lines = [f"      origin_kind = \'{kind}\'"]
    lines.append(f"      AND {ORIGIN_REQUIRED_COLUMN[kind]} IS NOT NULL")
    lines += [
        f"      AND {column} IS NULL"
        for other, column in ORIGIN_REQUIRED_COLUMN.items()
        if other != kind
    ]
    body = "\n".join(lines)
    return f"(\n{body}\n    )"


_ORIGIN_CHECK = """
  CHECK (
    {arms}
  )
""".format(arms="\n    OR ".join(_origin_arm(kind) for kind in ORIGIN_KINDS))

# ---------------------------------------------------------------------------
# Version 1 -- FROZEN.
#
# Three published commits each stamped a *different* schema as version 1
# (40f7f98, ae253b2, 6d7659c). Editing this DDL again would repeat that: the
# ledger would still read 1, so every database already carrying a version 1 row
# would be skipped and left in whatever shape its own commit created. The two
# statements below that have since changed are therefore spelled out verbatim
# rather than derived from the constants above -- the current shape of a table
# is version 2's business, not version 1's. Anything this schema needs to change
# from here on gets a new numbered migration.
#
# The other twenty statements still interpolate the closed-set constants at the
# top of this file, because those constants are what keeps one closed set from
# being spelled two ways. That leaves one way to edit version 1 by accident:
# adding a value to SOURCES, ROLES, LEDGERED_EFFECTS, ORIGIN_KINDS,
# CONSOLIDATION_STATUSES, LEDGER_STATUSES or PRUNE_REASON_PRIORITY rewrites DDL
# that has already shipped, after which _verify_v1_shape would refuse every real
# version 1 database as drifted. Nothing in this module can stop that, so it is
# caught instead: test_version_1_ddl_is_frozen_at_the_shape_the_last_commit_published
# diffs the normalized shape of what version 1 builds against an independent
# baseline of 6d7659c and fails the moment it moves. Adding a value means
# adding a migration.
# ---------------------------------------------------------------------------

# The pre-rename calendar table (40f7f98, ae253b2). Never created any more; kept
# because version 2 has to recognise a database that still holds one.
_V1_EVENTS = """
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT,
  participants TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
)
"""

# The post-rename calendar table (6d7659c): suffix-only instant CHECKs and a
# zone CHECK that demanded an interior '/'.
_V1_CALENDAR_ENTRIES = """
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
)
"""

_V1_CONSOLIDATION_OPS = f"""
CREATE TABLE consolidation_ops (
  op_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES consolidation_batches (batch_id),
  status TEXT NOT NULL CHECK (
    status IN ({_CONSOLIDATION_STATUS_SQL})
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)
"""

# Plain CREATE, not CREATE IF NOT EXISTS: the migration runner decides whether a
# version runs, and it runs each version at most once. IF NOT EXISTS would turn
# "this object already exists in a shape nobody vouched for" into a silent no-op.
_V1_STATEMENTS = (
    """
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)
""",
    _V1_CALENDAR_ENTRIES,
    f"""
CREATE TABLE facts (
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
CREATE INDEX facts_subject_idx ON facts (subject)
""",
    """
CREATE VIRTUAL TABLE facts_fts USING fts5(
  subject,
  fact,
  content='facts',
  content_rowid='rowid',
  tokenize='unicode61'
)
""",
    """
CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END
""",
    """
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
END
""",
    """
CREATE TRIGGER facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, fact)
  VALUES ('delete', old.rowid, old.subject, old.fact);
  INSERT INTO facts_fts(rowid, subject, fact)
  VALUES (new.rowid, new.subject, new.fact);
END
""",
    f"""
CREATE TABLE episodes (
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
CREATE INDEX episodes_occurred_at_idx ON episodes (occurred_at)
""",
    """
CREATE VIRTUAL TABLE episodes_fts USING fts5(
  summary,
  content='episodes',
  content_rowid='rowid',
  tokenize='unicode61'
)
""",
    """
CREATE TRIGGER episodes_fts_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END
""",
    """
CREATE TRIGGER episodes_fts_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
END
""",
    """
CREATE TRIGGER episodes_fts_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, summary)
  VALUES ('delete', old.rowid, old.summary);
  INSERT INTO episodes_fts(rowid, summary)
  VALUES (new.rowid, new.summary);
END
""",
    f"""
CREATE TABLE agent_log (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ({_ROLE_SQL})),
  content TEXT NOT NULL CHECK (json_valid(content)),
  consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ({_SOURCE_SQL})),
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  created_at TEXT NOT NULL
)
""",
    """
CREATE INDEX agent_log_session_created_idx
  ON agent_log (session_id, created_at)
""",
    f"""
CREATE TABLE tool_ledger (
  id INTEGER PRIMARY KEY,
  tool_name TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  effect TEXT NOT NULL CHECK (effect IN ({_LEDGERED_EFFECT_SQL})),
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
CREATE INDEX tool_ledger_name_fingerprint_idx
  ON tool_ledger (tool_name, fingerprint)
""",
    """
CREATE INDEX tool_ledger_session_created_idx
  ON tool_ledger (session_id, created_at)
""",
    f"""
CREATE TABLE trace_prunes (
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
CREATE TABLE consolidation_batches (
  batch_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (
    status IN ({_CONSOLIDATION_STATUS_SQL})
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)
""",
    _V1_CONSOLIDATION_OPS,
)

# Every object v1 creates, including the FTS5 shadow tables SQLite materialises
# behind each virtual table. This manifest lives next to the statements that
# create it because the two must be edited together: it is what tells migrate()
# whether a database with no version ledger is genuinely new or is an old,
# unversioned database whose shape nothing vouches for.
_V1_MANAGED_OBJECTS = (
    "schema_migrations",
    "calendar_entries",
    "facts",
    "facts_subject_idx",
    "facts_fts",
    "facts_fts_config",
    "facts_fts_data",
    "facts_fts_docsize",
    "facts_fts_idx",
    "facts_fts_ai",
    "facts_fts_ad",
    "facts_fts_au",
    "episodes",
    "episodes_occurred_at_idx",
    "episodes_fts",
    "episodes_fts_config",
    "episodes_fts_data",
    "episodes_fts_docsize",
    "episodes_fts_idx",
    "episodes_fts_ai",
    "episodes_fts_ad",
    "episodes_fts_au",
    "agent_log",
    "agent_log_session_created_idx",
    "tool_ledger",
    "tool_ledger_name_fingerprint_idx",
    "tool_ledger_session_created_idx",
    "trace_prunes",
    "consolidation_batches",
    "consolidation_ops",
)

# ---------------------------------------------------------------------------
# Version 2 -- the repair.
#
# It rebuilds the two tables whose shape moved after version 1 went out, and it
# has to do so on a database that may be any of the three shapes stamped as
# version 1. Every rebuild is a copy: rows are carried across, never dropped or
# recreated empty, and anything the new CHECKs would refuse stops the migration
# before the first destructive statement runs.
# ---------------------------------------------------------------------------

_V2_CALENDAR_ENTRIES = f"""
CREATE TABLE calendar_entries (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  -- Absolute instants. See the CHECK comment above: format guard, not validator.
  starts_at TEXT NOT NULL CHECK {_instant_check("starts_at")},
  ends_at TEXT CHECK (
    ends_at IS NULL
    OR {_instant_check("ends_at")}
  ),
  -- Kept only so a local rendering can name the zone; recurrence is out of scope.
  iana_time_zone TEXT CHECK (
    iana_time_zone IS NULL
    OR {_IANA_ZONE_CHECK}
  ),
  participants TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
)
"""

_V2_CONSOLIDATION_OPS = f"""
CREATE TABLE consolidation_ops (
  op_id TEXT PRIMARY KEY,
  -- No REFERENCES clause: this database runs with foreign keys off, so one
  -- would read like a guarantee while enforcing nothing. Batch/op consistency
  -- is not something this schema offers.
  batch_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ({_CONSOLIDATION_STATUS_SQL})
  ),
  created_at TEXT NOT NULL,
  finished_at TEXT
)
"""

# Named once so the registry entry and the refusal messages cannot drift apart.
_REPAIR_VERSION = 2

_CALENDAR_COLUMNS = (
    "id",
    "title",
    "starts_at",
    "ends_at",
    "iana_time_zone",
    "participants",
    "notes",
    "created_at",
)
_CONSOLIDATION_OPS_COLUMNS = (
    "op_id",
    "batch_id",
    "status",
    "created_at",
    "finished_at",
)
# Held only for the length of one migration, inside its transaction.
_V2_OLD_SUFFIX = "_v1_being_replaced"
# What stands in for the zone column when the source table has none.
_NO_ZONE_COLUMN = "NULL"

_V2_AMBIGUOUS_CALENDAR = """\
Refusing to migrate to version {version}: this version 1 database holds {found}, \
and version 2 cannot tell which one carries the schedule.

Exactly one of `events` (40f7f98, ae253b2) or `calendar_entries` (6d7659c) is \
expected. Back the database file up before touching it, decide by hand which \
table holds the rows you want, drop or rename the other, and re-run. No DDL has \
run; the database is exactly as it was found.
"""

_V2_UNKNOWN_SHAPE = """\
Refusing to migrate to version {version}: this database records version 1, but \
its shape is not one this code recognises.

{detail}

Version 2 rebuilds tables by copying rows into a new definition, so it will not \
run against a shape it cannot account for -- a copy driven by a wrong idea of \
the source is how rows go missing. Back the database file up, export what you \
need, and either restore a database this code knows or migrate the rows into a \
fresh one by hand. No DDL has run; the database is exactly as it was found.
"""

_V2_UNCONVERTIBLE_ROWS = """\
Refusing to migrate to version {version}: {count} row(s) in `{table}` cannot \
satisfy the version 2 CHECK constraints, so copying them across would fail \
part-way or silently drop them.

First offenders (id, starts_at, ends_at, zone): {sample}

Version 2 requires starts_at (and ends_at when present) to carry a UTC \
designator or numeric offset and to parse, and any zone name to have an IANA \
name's shape. Back the database file up, correct or remove those rows -- a \
naive local wall clock needs a decision about which zone it meant, and this \
code will not guess one -- then re-run. No DDL has run; the database is exactly \
as it was found.
"""


class Migration(NamedTuple):
    """One numbered step. The registry below is the only source of schema truth."""

    version: int
    apply: Callable[[sqlite3.Connection], None]
    managed_objects: tuple[str, ...]


class Fts5UnavailableError(RuntimeError):
    """Raised when this interpreter's sqlite3 was built without FTS5."""

    def __init__(self, sqlite_version: str) -> None:
        super().__init__(_FTS5_UNAVAILABLE.format(sqlite_version=sqlite_version))
        self.sqlite_version = sqlite_version


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk schema version cannot be migrated by this code."""


_OBJECT_NAME = re.compile(
    r"^CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER)\s+(\w+)", re.IGNORECASE
)


def _object_name(statement: str) -> str:
    match = _OBJECT_NAME.match(statement.strip())
    if match is None:
        raise ValueError(f"cannot read an object name out of: {statement.strip()[:60]}")
    return match.group(1)


def _normalize_ddl(sql: str) -> str:
    """Reduce a CREATE statement to text two revisions can be compared on.

    SQLite stores DDL as written, so indentation and comments differ between
    revisions that create the same object. Both are dropped here. A `--` inside
    a string literal would be mangled, which can only turn a match into a
    mismatch -- the direction that fails closed rather than waving a shape through.
    """
    return " ".join(re.sub(r"--[^\n]*", " ", sql).split())


# What version 1 left behind, by object name. Version 2 compares the database
# against this before it rebuilds anything: the ledger says which migration ran,
# it cannot say whether the objects still look like its output.
_V1_AUTHORED_SHAPES = {
    _object_name(statement): _normalize_ddl(statement)
    for statement in _V1_STATEMENTS
}
_V1_CALENDAR_SHAPES = {
    "calendar_entries": _V1_AUTHORED_SHAPES["calendar_entries"],
    "events": _normalize_ddl(_V1_EVENTS),
}
# SQLite writes these itself behind each CREATE VIRTUAL TABLE, in whatever form
# this build of FTS5 uses, so they are checked for presence and not for text.
_V1_SHADOW_OBJECTS = tuple(
    name for name in _V1_MANAGED_OBJECTS if name not in _V1_AUTHORED_SHAPES
)


def _fts5_enabled(conn: sqlite3.Connection) -> bool:
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    return "ENABLE_FTS5" in options


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _apply_v1(conn: sqlite3.Connection) -> None:
    for statement in _V1_STATEMENTS:
        conn.execute(statement)


def _v1_calendar_table(conn: sqlite3.Connection) -> str:
    found = [name for name in _V1_CALENDAR_SHAPES if _table_exists(conn, name)]
    if len(found) != 1:
        raise SchemaVersionError(
            _V2_AMBIGUOUS_CALENDAR.format(
                version=_REPAIR_VERSION,
                found="both `events` and `calendar_entries`"
                if found
                else "neither `events` nor `calendar_entries`",
            )
        )
    return found[0]


def _verify_v1_shape(conn: sqlite3.Connection, calendar: str) -> None:
    expected = {
        name: shape
        for name, shape in _V1_AUTHORED_SHAPES.items()
        if name != "calendar_entries"
    }
    expected[calendar] = _V1_CALENDAR_SHAPES[calendar]
    actual = dict(conn.execute("SELECT name, sql FROM sqlite_master"))
    missing = sorted(
        name for name in (*expected, *_V1_SHADOW_OBJECTS) if name not in actual
    )
    drifted = sorted(
        name
        for name, shape in expected.items()
        if name in actual and _normalize_ddl(actual[name] or "") != shape
    )
    if not missing and not drifted:
        return
    detail = []
    if missing:
        detail.append(f"Version 1 objects this database does not have: {missing}.")
    if drifted:
        detail.append(
            f"Objects whose DDL differs from every version 1 this code knows: "
            f"{drifted}."
        )
    raise SchemaVersionError(
        _V2_UNKNOWN_SHAPE.format(
            version=_REPAIR_VERSION, detail="\n".join(detail)
        )
    )


def _v2_row_guard(zone: str) -> str:
    """The version 2 CHECKs as one predicate, so the pre-flight cannot drift.

    `zone` is the expression standing in for the zone column: the literal NULL
    that a pre-6d7659c `events` table supplies has nothing to test.
    """
    guard = (
        f"{_instant_check('starts_at')}\n"
        f"    AND (ends_at IS NULL OR {_instant_check('ends_at')})"
    )
    if zone != _NO_ZONE_COLUMN:
        guard += f"\n    AND (iana_time_zone IS NULL OR {_IANA_ZONE_CHECK})"
    return guard


def _reject_unconvertible_rows(
    conn: sqlite3.Connection, calendar: str, zone: str
) -> None:
    """Fail before the first destructive statement, not part-way through a copy."""
    guard = _v2_row_guard(zone)
    offending = f"SELECT {{what}} FROM {calendar} WHERE COALESCE({guard}, 0) = 0"
    count = conn.execute(offending.format(what="COUNT(*)")).fetchone()[0]
    if not count:
        return
    sample = conn.execute(
        offending.format(what=f"id, starts_at, ends_at, {zone}")
        + " ORDER BY id LIMIT 3"
    ).fetchall()
    raise SchemaVersionError(
        _V2_UNCONVERTIBLE_ROWS.format(
            version=_REPAIR_VERSION, count=count, table=calendar, sample=sample
        )
    )


def _rebuild_table(
    conn: sqlite3.Connection,
    *,
    source: str,
    create: str,
    target: str,
    columns: tuple[str, ...],
    select: tuple[str, ...],
) -> None:
    """Copy `source` into a table built by `create`, then drop the original.

    Renaming the old table out of the way first (rather than renaming the new
    one into place) keeps the surviving table's stored DDL exactly the text in
    `create`: `ALTER TABLE ... RENAME TO` rewrites the name it stores, so the
    other order would leave a quoted name no fresh database ever writes. The
    tables version 1 created and version 2 does not touch still carry whatever
    text their own commit wrote, so this is a per-rebuilt-table property, not a
    claim that two databases match byte for byte.
    """
    parked = f"{source}{_V2_OLD_SUFFIX}"
    conn.execute(f"ALTER TABLE {source} RENAME TO {parked}")
    conn.execute(create)
    conn.execute(
        f"INSERT INTO {target} ({', '.join(columns)}) "
        f"SELECT {', '.join(select)} FROM {parked}"
    )
    conn.execute(f"DROP TABLE {parked}")


def _apply_v2(conn: sqlite3.Connection) -> None:
    calendar = _v1_calendar_table(conn)
    _verify_v1_shape(conn, calendar)
    # Decided once. `events` predates the zone column, so every later step reads
    # this expression rather than re-deciding which table it is looking at.
    zone = "iana_time_zone" if calendar == "calendar_entries" else _NO_ZONE_COLUMN
    _reject_unconvertible_rows(conn, calendar, zone)
    _rebuild_table(
        conn,
        source=calendar,
        create=_V2_CALENDAR_ENTRIES,
        target="calendar_entries",
        columns=_CALENDAR_COLUMNS,
        select=tuple(
            zone if column == "iana_time_zone" else column
            for column in _CALENDAR_COLUMNS
        ),
    )
    _rebuild_table(
        conn,
        source="consolidation_ops",
        create=_V2_CONSOLIDATION_OPS,
        target="consolidation_ops",
        columns=_CONSOLIDATION_OPS_COLUMNS,
        select=_CONSOLIDATION_OPS_COLUMNS,
    )


MIGRATIONS = (
    Migration(version=1, apply=_apply_v1, managed_objects=_V1_MANAGED_OBJECTS),
    # Renames and rebuilds only: every name it leaves behind is already v1's.
    Migration(version=_REPAIR_VERSION, apply=_apply_v2, managed_objects=()),
)
MIGRATION_VERSIONS = tuple(migration.version for migration in MIGRATIONS)
LATEST_MIGRATION_VERSION = MIGRATION_VERSIONS[-1]
MANAGED_OBJECTS = frozenset(
    name for migration in MIGRATIONS for name in migration.managed_objects
)
# Names this schema's own DDL once created and has since retired. A database
# built by an earlier revision is precisely the unversioned database the guard
# below exists to catch, so a rename moves the old name here instead of dropping
# it -- otherwise the pre-rename database sails through as "new" and gets
# stamped with a version while its stale table survives untouched.
RETIRED_OBJECTS = frozenset(
    (
        # calendar_entries was called `events` until the glossary settled on
        # "日程 (calendar entry)" and put `events` on that entry's _Avoid_ list.
        "events",
    )
)
_VERSION_TABLE = "schema_migrations"
_MIGRATION_SAVEPOINT = "agent_alfred_migrate"


def _existing_managed_objects(
    conn: sqlite3.Connection, registry: tuple[Migration, ...]
) -> list[str]:
    """Which of this schema's own object names a database already holds.

    Takes the registry rather than reading MIGRATIONS: migrate() resolves the
    registry once per call, so everything downstream sees the same list even
    when a test has put a migration in front of it.
    """
    markers = {name for m in registry for name in m.managed_objects} | RETIRED_OBJECTS
    names = sorted(markers)
    placeholders = ", ".join("?" * len(names))
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})", names
    ).fetchall()
    return sorted(row[0] for row in rows)


def _applied_versions(
    conn: sqlite3.Connection, known: tuple[int, ...]
) -> list[int]:
    """Read the ledger: one row per applied version. Anything odd fails closed.

    The ledger records what migrate() applied. It is not a continuous integrity
    check -- it cannot notice a table someone dropped or a column someone
    rewrote after the fact. Version 2 does its own shape check for that reason.
    """
    applied = [
        row[0]
        for row in conn.execute(
            f"SELECT version FROM {_VERSION_TABLE} ORDER BY version"
        )
    ]
    if not applied:
        raise SchemaVersionError(
            f"{_VERSION_TABLE} exists but records no applied version; the "
            "version row lands in the same transaction as its DDL, so an empty "
            "ledger is a database whose shape nothing vouches for"
        )
    illegal = [version for version in applied if version < 1]
    if illegal:
        raise SchemaVersionError(
            f"{_VERSION_TABLE} records illegal version(s) {illegal}; "
            "migration versions start at 1"
        )
    # The registry is 1..N by construction, so a ledger that is not a contiguous
    # run from 1 is missing a version (or holds one this code never issued), and
    # a ledger that IS contiguous but longer than the registry comes from newer
    # code. Both fail closed; only the message differs.
    if applied != list(range(1, len(applied) + 1)):
        raise SchemaVersionError(
            f"{_VERSION_TABLE} records {applied}, which is not a contiguous "
            f"prefix of the migration registry {list(known)}; "
            "refusing to guess which versions actually ran"
        )
    if len(applied) > len(known):
        raise SchemaVersionError(
            f"database schema version {applied[-1]} is newer than this code "
            f"(latest migration {known[-1]})"
        )
    return applied


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade tables. Idempotent. Does not commit a caller transaction.

    Inside a transaction the caller opened, every pending migration runs under
    one SAVEPOINT: it is released on success and rolled back to on failure, so a
    migration that dies half-way takes its own DDL with it and leaves whatever
    the caller had already written still in play, still uncommitted, still the
    caller's to commit or roll back.
    """
    registry = MIGRATIONS
    known = tuple(migration.version for migration in registry)
    caller_owns_txn = conn.in_transaction
    configure_connection(conn)
    if not _fts5_enabled(conn):
        raise Fts5UnavailableError(sqlite3.sqlite_version)
    if _table_exists(conn, _VERSION_TABLE):
        applied = _applied_versions(conn, known)
    else:
        leftovers = _existing_managed_objects(conn, registry)
        if leftovers:
            raise SchemaVersionError(
                _UNVERSIONED_DATABASE.format(
                    objects=", ".join(leftovers), version=known[0]
                )
            )
        applied = []
    pending = [migration for migration in registry if migration.version not in applied]
    if not pending:
        return
    if caller_owns_txn:
        conn.execute(f"SAVEPOINT {_MIGRATION_SAVEPOINT}")
    else:
        conn.execute("BEGIN")
    try:
        for migration in pending:
            migration.apply(conn)
            conn.execute(
                f"INSERT INTO {_VERSION_TABLE} (version, applied_at) "
                # datetime('now') is a naive string, which parse_instant below
                # rejects as "not an instant". The ledger's own column must
                # satisfy the module's own contract.
                #
                # Rows every version 1 already wrote stay naive: this applies to
                # what this code writes, not to what it finds. Restating someone
                # else's stored timestamp is a write nobody asked for, on the one
                # table whose job is to record what happened -- and the row is
                # audit history, not an input any code here reads back as an
                # instant.
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (migration.version,),
            )
    except BaseException:
        if not conn.in_transaction:
            # SQLite already tore the whole transaction down (a disk-full or
            # otherwise fatal error does that). Nothing left to undo.
            raise
        if caller_owns_txn:
            conn.execute(f"ROLLBACK TO {_MIGRATION_SAVEPOINT}")
            conn.execute(f"RELEASE {_MIGRATION_SAVEPOINT}")
        else:
            conn.rollback()
        raise
    if caller_owns_txn:
        conn.execute(f"RELEASE {_MIGRATION_SAVEPOINT}")
    else:
        conn.commit()


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
    """Record one pruned Run. Never commits: the caller owns the batch transaction.

    Only `prune_reason` is validated. The two timestamps are passed through as
    given -- the column is NOT NULL and nothing more, so 'banana' goes in. That
    is deliberate rather than overlooked: parse_instant states the module's
    instant contract, and applying it here would make this function the only
    write path in the schema that enforces one, which reads as a guarantee the
    other tables do not offer.
    """
    prune_reason = pick_prune_reason((prune_reason,))
    conn.execute(
        """INSERT INTO trace_prunes (
             run_id, prune_requested_at, absence_confirmed_at, prune_reason
           ) VALUES (?, ?, ?, ?)
           ON CONFLICT(run_id) DO NOTHING""",
        (run_id, prune_requested_at, absence_confirmed_at, prune_reason),
    )
