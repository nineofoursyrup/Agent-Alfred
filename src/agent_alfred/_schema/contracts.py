"""Shared schema types, closed sets and exception identities."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Literal, NamedTuple

from agent_alfred import run_phases
from agent_alfred.outcomes import RUN_OUTCOMES
from agent_alfred.outcomes import RunOutcome as RunOutcome
from agent_alfred.run_phases import RunPhase

PHASES = run_phases.RUN_PHASES

PruneReason = Literal["manual", "disk_low", "age", "capacity"]
BUSY_TIMEOUT_MS = 5000
# Highest first. One deletion may match several reasons; the stored
# value is whichever of these wins, so the same set always records the same row.
PRUNE_REASON_PRIORITY: tuple[PruneReason, ...] = (
    "manual",
    "disk_low",
    "age",
    "capacity",
)
PRUNE_REASONS = frozenset(PRUNE_REASON_PRIORITY)
# Two separate closed sets that happen to hold the same words today. The tool
# ledger's states come from #4 §7; the consolidator's come from ADR-0009. Sharing
# one constant would make a future edit to the tool ledger silently rewrite what
# consolidation is allowed to record.
LEDGER_STATUSES = ("started", "succeeded", "failed", "unknown")
CONSOLIDATION_STATUSES = ("started", "succeeded", "failed", "unknown")
SOURCES = ("cli", "web")
ROLES = ("user", "assistant")
# #4 §7's effect ladder, minus local_read: a read has nothing to account for, so
# the ledger's CHECK is the closed set of the effects that DO get a row.
LEDGERED_EFFECTS = ("local_write", "external")
# Each origin kind, and the one column that must be present for it. Both the
# closed set and migrations' exclusivity CHECK derive from this map, so a
# fourth kind cannot be added to one of them and forgotten in the other.
ORIGIN_REQUIRED_COLUMN = {
    "consolidation": "origin_batch_id",
    "manual": "origin_source",
    "tool": "origin_call_id",
}
ORIGIN_KINDS = tuple(ORIGIN_REQUIRED_COLUMN)
# Run index closed sets. Independent of SOURCES even though the Gateway
# values match today: a new message source must not silently rewrite runs.
GATEWAYS = ("cli", "web")
PURPOSES = ("chat", "inference_probe", "consolidation", "aggregation")
OUTCOMES = RUN_OUTCOMES
# The closed Run transition graph. ``update_run_phase`` is the only entry
# point that moves a Run, so the graph lives here rather than in callers'
# conventions: a terminal Run has no outgoing edge at all -- not even one
# back to ``finished``, which is how a completed Run used to be rewritten
# into an interrupted one by a second finalize.
#   accepted -> running   the execution thread took the handed-off Run
#   accepted -> finished  the handoff failed, or startup recovery found it
#   running  -> finished  the Run's terminal outcome was decided
RUN_PHASE_TRANSITIONS: dict[RunPhase, tuple[RunPhase, ...]] = {
    "accepted": ("running", "finished"),
    "running": ("finished",),
    "finished": (),
}

_FTS5_UNAVAILABLE = """\
SQLite FTS5 is not enabled in this Python interpreter's sqlite3 module \
(SQLite {sqlite_version}). Agent-Alfred needs FTS5 for semantic and \
episodic memory search.

Python 3.14 official builds (python.org, Homebrew, uv) ship with FTS5. \
If you built Python or SQLite yourself, rebuild SQLite with \
-DSQLITE_ENABLE_FTS5 and link that Python against it.

Confirm with PRAGMA compile_options; the result must include ENABLE_FTS5.
"""


class Migration(NamedTuple):
    """One numbered step. The migration registry is the only source of schema truth."""

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


class RunPhaseError(RuntimeError):
    """Raised when a Run phase UPDATE does not affect exactly one row."""


# Names this schema's own DDL once created and has since retired. A database
# built by an earlier revision is precisely the unversioned database the guard
# in the runner exists to catch, so renames retain old names here instead of dropping
# it -- otherwise the pre-rename database sails through as "new" and gets
# stamped with a version while its stale table survives untouched.
RETIRED_OBJECTS = frozenset(
    (
        # calendar_entries was called `events` until the glossary settled on
        # "日程 (calendar entry)" and put `events` on that entry's _Avoid_ list.
        "events",
    )
)
