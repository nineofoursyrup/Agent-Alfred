"""Frozen migration definitions and ordered registry; no runtime Run writes."""
from __future__ import annotations

import re
import sqlite3

from agent_alfred.memory.consolidation_migration import TABLES as _V11_OBJECTS
from agent_alfred.memory.forget_migration import TABLES as _V6_OBJECTS
from agent_alfred.memory.mirror_migration import OBJECTS as _V14_OBJECTS
from agent_alfred.memory.notification_migration import OBJECTS as _V15_OBJECTS
from agent_alfred.memory.notification_migration import RUN_OBJECTS as _V16_OBJECTS

from .activity import allocate_activity_revision
from .contracts import (
    CONSOLIDATION_STATUSES,
    GATEWAYS,
    LEDGER_STATUSES,
    LEDGERED_EFFECTS,
    ORIGIN_KINDS,
    ORIGIN_REQUIRED_COLUMN,
    OUTCOMES,
    PHASES,
    PRUNE_REASON_PRIORITY,
    ROLES,
    SOURCES,
    Migration,
    SchemaVersionError,
)
from .runner import _table_exists

_PRUNE_REASON_SQL = ", ".join(f"'{reason}'" for reason in PRUNE_REASON_PRIORITY)
_LEDGER_STATUS_SQL = ", ".join(f"'{status}'" for status in LEDGER_STATUSES)
_CONSOLIDATION_STATUS_SQL = ", ".join(f"'{s}'" for s in CONSOLIDATION_STATUSES)
_SOURCE_SQL = ", ".join(f"'{source}'" for source in SOURCES)
_ROLE_SQL = ", ".join(f"'{role}'" for role in ROLES)
_LEDGERED_EFFECT_SQL = ", ".join(f"'{effect}'" for effect in LEDGERED_EFFECTS)
_ORIGIN_KIND_SQL = ", ".join(f"'{kind}'" for kind in ORIGIN_KINDS)
_GATEWAY_SQL = ", ".join(f"'{gateway}'" for gateway in GATEWAYS)
# v3 DDL is frozen to this pair. Current purpose names live in PURPOSES and
# are applied by a later migration; changing PURPOSES must not rewrite v3 SQL.
_V3_PURPOSES = ("chat", "inference_probe")
_V3_PURPOSE_SQL = ", ".join(f"'{purpose}'" for purpose in _V3_PURPOSES)
# v12 remains frozen to its originally published closed set.
_PURPOSE_SQL = "'chat', 'inference_probe', 'consolidation'"
_PHASE_SQL = ", ".join(f"'{phase}'" for phase in PHASES)
_OUTCOME_SQL = ", ".join(f"'{outcome}'" for outcome in OUTCOMES)
# Non-terminal phases may only pair with a null outcome; finished must pair
# with a closed outcome. The CHECK is the shape; callers still have to
# UPDATE with an old-phase predicate that affects exactly one row.
_PHASE_OUTCOME_CHECK = """(
    (
      phase IN ('accepted', 'running')
      AND outcome IS NULL
    )
    OR (
      phase = 'finished'
      AND outcome IS NOT NULL
    )
  )"""

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


def _v2_row_guard(zone_sql: str) -> str:
    """The version 2 CHECKs as one predicate, so the pre-flight cannot drift.

    `zone_sql` is the expression standing in for the zone column: the literal NULL
    that a pre-6d7659c `events` table supplies has nothing to test.
    """
    guard = (
        f"{_instant_check('starts_at')}\n"
        f"    AND (ends_at IS NULL OR {_instant_check('ends_at')})"
    )
    if zone_sql != _NO_ZONE_COLUMN:
        guard += f"\n    AND (iana_time_zone IS NULL OR {_IANA_ZONE_CHECK})"
    return guard


def _reject_unconvertible_rows(
    conn: sqlite3.Connection, calendar: str, zone_sql: str
) -> None:
    """Fail before the first destructive statement, not part-way through a copy."""
    guard = _v2_row_guard(zone_sql)
    offending = f"SELECT {{what}} FROM {calendar} WHERE COALESCE({guard}, 0) = 0"
    count = conn.execute(offending.format(what="COUNT(*)")).fetchone()[0]
    if not count:
        return
    sample = conn.execute(
        offending.format(what=f"id, starts_at, ends_at, {zone_sql}")
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
    zone_sql = "iana_time_zone" if calendar == "calendar_entries" else _NO_ZONE_COLUMN
    _reject_unconvertible_rows(conn, calendar, zone_sql)
    _rebuild_table(
        conn,
        source=calendar,
        create=_V2_CALENDAR_ENTRIES,
        target="calendar_entries",
        columns=_CALENDAR_COLUMNS,
        select=tuple(
            zone_sql if column == "iana_time_zone" else column
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


# ---------------------------------------------------------------------------
# Version 3 -- sessions, the Run index, the activity clock.
#
# Forward-only. Version 1 and version 2 stay frozen: a database that already
# recorded those numbers would skip any in-place edit, and two shapes would
# then share a ledger row.
# ---------------------------------------------------------------------------

_V3_VERSION = 3

_V3_AGENT_LOG_COLUMNS = (
    "id",
    "session_id",
    "role",
    "content",
    "consolidated",
    "source",
    "telemetry",
    "created_at",
)

_V3_AGENT_LOG = f"""
CREATE TABLE agent_log (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ({_ROLE_SQL})),
  content TEXT NOT NULL CHECK (json_valid(content)),
  consolidated INTEGER NOT NULL DEFAULT 0 CHECK (consolidated IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ({_SOURCE_SQL})),
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  created_at TEXT NOT NULL,
  run_id TEXT,
  CHECK (run_id IS NULL OR telemetry IS NULL)
)
"""

_V3_SESSIONS = """
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  activity_revision INTEGER NOT NULL UNIQUE
)
"""

_V3_RUNS = f"""
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  purpose TEXT NOT NULL CHECK (purpose IN ({_V3_PURPOSE_SQL})),
  session_id TEXT,
  gateway TEXT NOT NULL CHECK (gateway IN ({_GATEWAY_SQL})),
  entry_surface_id TEXT,
  prompt_preview TEXT,
  phase TEXT NOT NULL CHECK (phase IN ({_PHASE_SQL})),
  outcome TEXT CHECK (
    outcome IS NULL OR outcome IN ({_OUTCOME_SQL})
  ),
  accepted_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  activity_revision INTEGER NOT NULL,
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  CHECK {_PHASE_OUTCOME_CHECK}
)
"""

_V3_ACTIVITY_CLOCK = """
CREATE TABLE activity_clock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  next_revision INTEGER NOT NULL CHECK (next_revision >= 1)
)
"""

_V3_AGENT_LOG_SESSION_IDX = """
CREATE INDEX agent_log_session_created_idx
  ON agent_log (session_id, created_at)
"""

_V3_AGENT_LOG_RUN_ROLE_IDX = """
CREATE UNIQUE INDEX agent_log_run_role_unique
  ON agent_log (run_id, role) WHERE run_id IS NOT NULL
"""

_V3_RUNS_ACTIVITY_IDX = """
CREATE INDEX runs_activity_revision_idx
  ON runs (activity_revision, run_id)
"""

_V3_MANAGED_OBJECTS = (
    "sessions",
    "runs",
    "runs_activity_revision_idx",
    "activity_clock",
    "agent_log_run_role_unique",
)

_V3_UNKNOWN_AGENT_LOG = """\
Refusing to migrate to version {version}: `agent_log` is not the version 2 \
shape this code copies from ({detail}).

Version 3 rebuilds the table to add `run_id` and a CHECK that a message with \
a run_id cannot also carry telemetry. A copy driven by the wrong columns \
would drop or invent fields. Back the database file up. No DDL has run; \
the database is exactly as it was found.
"""

_V3_OBJECTS_ALREADY_PRESENT = """\
Refusing to migrate to version {version}: {objects} already exist, so this \
is not a version 2 database this code can upgrade. Back the database file \
up. No DDL has run; the database is exactly as it was found.
"""


def _agent_log_columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(agent_log)")]


def _verify_v2_agent_log(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "agent_log"):
        raise SchemaVersionError(
            _V3_UNKNOWN_AGENT_LOG.format(
                version=_V3_VERSION, detail="the table is missing"
            )
        )
    actual = _agent_log_columns(conn)
    expected = list(_V3_AGENT_LOG_COLUMNS)
    if actual != expected:
        raise SchemaVersionError(
            _V3_UNKNOWN_AGENT_LOG.format(
                version=_V3_VERSION,
                detail=f"columns {actual} rather than {expected}",
            )
        )


def _reject_v3_if_already_present(conn: sqlite3.Connection) -> None:
    found = [
        name
        for name in ("sessions", "runs", "activity_clock")
        if _table_exists(conn, name)
    ]
    if found:
        raise SchemaVersionError(
            _V3_OBJECTS_ALREADY_PRESENT.format(
                version=_V3_VERSION, objects=", ".join(found)
            )
        )


def _backfill_sessions(conn: sqlite3.Connection) -> None:
    """Insert one sessions row per distinct agent_log.session_id, verbatim.

    created_at is copied from the row with the smallest id, not from MIN()
    on the time text: that column was never a strict instant, so a
    lexicographic minimum is not the earliest message.
    activity_revision is allocated oldest-max-id first so the session that
    last received a message sorts ahead. Re-running the migration is a
    no-op because the ledger already records version 3.
    """
    groups = conn.execute(
        """
        SELECT session_id, MAX(id) AS max_id
        FROM agent_log
        GROUP BY session_id
        ORDER BY max_id ASC
        """
    ).fetchall()
    for session_id, _max_id in groups:
        created_at = conn.execute(
            """
            SELECT created_at FROM agent_log
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()[0]
        revision = allocate_activity_revision(conn)
        conn.execute(
            """INSERT INTO sessions (
                 session_id, created_at, activity_revision
               ) VALUES (?, ?, ?)""",
            (session_id, created_at, revision),
        )


def _apply_v3(conn: sqlite3.Connection) -> None:
    _reject_v3_if_already_present(conn)
    _verify_v2_agent_log(conn)
    _rebuild_table(
        conn,
        source="agent_log",
        create=_V3_AGENT_LOG,
        target="agent_log",
        columns=(*_V3_AGENT_LOG_COLUMNS, "run_id"),
        select=(*_V3_AGENT_LOG_COLUMNS, "NULL"),
    )
    conn.execute(_V3_AGENT_LOG_SESSION_IDX)
    conn.execute(_V3_AGENT_LOG_RUN_ROLE_IDX)
    conn.execute(_V3_SESSIONS)
    conn.execute(_V3_RUNS)
    conn.execute(_V3_RUNS_ACTIVITY_IDX)
    conn.execute(_V3_ACTIVITY_CLOCK)
    conn.execute("INSERT INTO activity_clock (id, next_revision) VALUES (1, 1)")
    _backfill_sessions(conn)


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Preserve v3 facts; absence of execution evidence is not a refusal."""
    conn.execute(
        """ALTER TABLE runs ADD COLUMN admission_state TEXT NOT NULL
           DEFAULT 'unconfirmed' CHECK (
             admission_state IN ('pending', 'admitted', 'rejected', 'unconfirmed')
           )"""
    )
    conn.execute(
        """UPDATE runs SET admission_state = 'admitted'
           WHERE started_at IS NOT NULL OR phase = 'running'
             OR telemetry IS NOT NULL
             OR EXISTS (SELECT 1 FROM agent_log WHERE agent_log.run_id = runs.run_id)"""
    )


def _apply_v5(conn: sqlite3.Connection) -> None:
    from agent_alfred.memory.command_migration import migrate_commands
    from agent_alfred.memory.migration import migrate_memory

    migrate_memory(conn)
    migrate_commands(conn)


def _apply_v6(conn):
    from agent_alfred.memory.forget_migration import migrate_forgetting

    migrate_forgetting(conn)


def _apply_v7(conn):
    from agent_alfred.tools.file_migration import migrate_files
    migrate_files(conn)


def _apply_v8(conn):
    from agent_alfred.tools.external_migration import migrate_external_operations

    migrate_external_operations(conn)


def _apply_v9(conn):
    from agent_alfred.tools.file_attempt_migration import migrate_file_attempts

    migrate_file_attempts(conn)


def _apply_v10(conn):
    conn.execute("""CREATE TABLE run_input_explanations (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        identity TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('attempt','preparation','failure')),
        explanation TEXT NOT NULL CHECK (json_valid(explanation)),
        UNIQUE(run_id, identity)
    )""")


def _apply_v11(conn):
    from agent_alfred.memory.consolidation_migration import migrate_consolidation

    migrate_consolidation(conn)


_V12_RUN_COLUMNS = (
    "run_id",
    "purpose",
    "session_id",
    "gateway",
    "entry_surface_id",
    "prompt_preview",
    "phase",
    "outcome",
    "accepted_at",
    "started_at",
    "finished_at",
    "activity_revision",
    "telemetry",
    "admission_state",
)
_V12_RUNS = f"""
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  purpose TEXT NOT NULL CHECK (purpose IN ({_PURPOSE_SQL})),
  session_id TEXT,
  gateway TEXT NOT NULL CHECK (gateway IN ({_GATEWAY_SQL})),
  entry_surface_id TEXT,
  prompt_preview TEXT,
  phase TEXT NOT NULL CHECK (phase IN ({_PHASE_SQL})),
  outcome TEXT CHECK (
    outcome IS NULL OR outcome IN ({_OUTCOME_SQL})
  ),
  accepted_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  activity_revision INTEGER NOT NULL,
  telemetry TEXT CHECK (telemetry IS NULL OR json_valid(telemetry)),
  admission_state TEXT NOT NULL DEFAULT 'unconfirmed' CHECK (
    admission_state IN ('pending', 'admitted', 'rejected', 'unconfirmed')
  ),
  CHECK {_PHASE_OUTCOME_CHECK}
)
"""


def _apply_v12(conn):
    _rebuild_table(
        conn,
        source="runs",
        create=_V12_RUNS,
        target="runs",
        columns=_V12_RUN_COLUMNS,
        select=_V12_RUN_COLUMNS,
    )
    conn.execute(_V3_RUNS_ACTIVITY_IDX)
    conn.execute(
        "ALTER TABLE memory_consolidation_plans ADD COLUMN request_json TEXT "
        "CHECK(request_json IS NULL OR json_valid(request_json))"
    )


def _apply_v13(conn):
    from agent_alfred.memory.consolidation_scheduling import migrate_scheduling
    migrate_scheduling(conn)


def _apply_v14(conn):
    from agent_alfred.memory.mirror_migration import migrate_mirrors
    migrate_mirrors(conn)




def _apply_v15(conn):
    from agent_alfred.memory.notification_migration import migrate_notifications
    migrate_notifications(conn)


def _apply_v16(conn):
    from agent_alfred.memory.notification_migration import migrate_run_notifications
    migrate_run_notifications(conn)


def _apply_v17(conn):
    from agent_alfred.tools.metering import migrate_metering

    migrate_metering(conn)


def _apply_v18(conn):
    # Rebuilding a SQLite table drops its indexes and triggers. Preserve their
    # exact definitions, including the v16 consolidation revision notification.
    attached = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='runs' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ).fetchall()
    _rebuild_table(
        conn,
        source="runs",
        target="runs",
        create=_V12_RUNS.replace(_PURPOSE_SQL, _PURPOSE_SQL + ", 'aggregation'"),
        columns=_V12_RUN_COLUMNS,
        select=_V12_RUN_COLUMNS,
    )
    for (statement,) in attached:
        conn.execute(statement)


MIGRATIONS = (
    Migration(version=1, apply=_apply_v1, managed_objects=_V1_MANAGED_OBJECTS),
    # Renames and rebuilds only: every name it leaves behind is already v1's.
    Migration(version=_REPAIR_VERSION, apply=_apply_v2, managed_objects=()),
    Migration(
        version=_V3_VERSION,
        apply=_apply_v3,
        managed_objects=_V3_MANAGED_OBJECTS,
    ),
    Migration(version=4, apply=_apply_v4, managed_objects=()),
    Migration(
        version=5,
        apply=_apply_v5,
        managed_objects=(
            "memory_revision",
            "memory_operations",
            "memory_sources",
            "memory_provenance",
        ),
    ),
    Migration(version=6, apply=_apply_v6, managed_objects=_V6_OBJECTS),
    Migration(
        version=7,
        apply=_apply_v7,
        managed_objects=(
            "file_operations",
            "file_pending_target",
            "skill_candidates",
            "local_tool_operations",
        ),
    ),
    Migration(
        version=8, apply=_apply_v8, managed_objects=("external_tool_operations",),
    ),
    Migration(version=9, apply=_apply_v9, managed_objects=()),
    Migration(
        version=10, apply=_apply_v10, managed_objects=("run_input_explanations",),
    ),
    Migration(version=11, apply=_apply_v11, managed_objects=_V11_OBJECTS),
    Migration(version=12, apply=_apply_v12, managed_objects=()),
    Migration(version=13, apply=_apply_v13, managed_objects=(
        "memory_consolidation_ready", "memory_consolidation_triggers",
    )),
    Migration(version=14, apply=_apply_v14, managed_objects=_V14_OBJECTS),
    Migration(version=15, apply=_apply_v15, managed_objects=_V15_OBJECTS),
    Migration(version=16, apply=_apply_v16, managed_objects=_V16_OBJECTS),
    Migration(
        version=17,
        apply=_apply_v17,
        managed_objects=(
            "tool_metering",
            "tool_metering_health",
            "tool_operation_verifications",
        ),
    ),
    Migration(version=18, apply=_apply_v18, managed_objects=()),
)
MIGRATION_VERSIONS = tuple(migration.version for migration in MIGRATIONS)
LATEST_MIGRATION_VERSION = MIGRATION_VERSIONS[-1]
MANAGED_OBJECTS = frozenset(
    name for migration in MIGRATIONS for name in migration.managed_objects
)
