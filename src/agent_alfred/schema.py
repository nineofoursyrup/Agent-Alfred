"""Public SQLite schema compatibility entry point.

Implementations have one owner in _schema. Each migrate call captures this
module's MIGRATIONS so caller/test replacement remains effective for that call.
"""

from __future__ import annotations

import sqlite3

from ._schema.contracts import (
    BUSY_TIMEOUT_MS,
    CONSOLIDATION_STATUSES,
    GATEWAYS,
    LEDGER_STATUSES,
    LEDGERED_EFFECTS,
    ORIGIN_KINDS,
    ORIGIN_REQUIRED_COLUMN,
    OUTCOMES,
    PHASES,
    PRUNE_REASON_PRIORITY,
    PRUNE_REASONS,
    PURPOSES,
    RETIRED_OBJECTS,
    ROLES,
    RUN_PHASE_TRANSITIONS,
    SOURCES,
    Fts5UnavailableError,
    Migration,
    PruneReason,
    RunOutcome,
    RunPhase,
    RunPhaseError,
    SchemaVersionError,
)
from ._schema.instants import (
    parse_instant,
)
from ._schema.migrations import (
    LATEST_MIGRATION_VERSION,
    MANAGED_OBJECTS,
    MIGRATION_VERSIONS,
    MIGRATIONS,
)
from ._schema.pruning import (
    pick_prune_reason,
    record_trace_prune,
)
from ._schema.run_records import (
    allocate_activity_revision,
    insert_accepted_run,
    insert_session,
    update_run_phase,
)
from ._schema.runner import (
    configure_connection,
)
from ._schema.runner import run_migrations as _run_migrations

__all__ = [
    "BUSY_TIMEOUT_MS",
    "CONSOLIDATION_STATUSES",
    "Fts5UnavailableError",
    "GATEWAYS",
    "LATEST_MIGRATION_VERSION",
    "LEDGERED_EFFECTS",
    "LEDGER_STATUSES",
    "MANAGED_OBJECTS",
    "MIGRATIONS",
    "MIGRATION_VERSIONS",
    "Migration",
    "ORIGIN_KINDS",
    "ORIGIN_REQUIRED_COLUMN",
    "OUTCOMES",
    "PHASES",
    "PRUNE_REASONS",
    "PRUNE_REASON_PRIORITY",
    "PURPOSES",
    "PruneReason",
    "RETIRED_OBJECTS",
    "ROLES",
    "RUN_PHASE_TRANSITIONS",
    "RunOutcome",
    "RunPhase",
    "RunPhaseError",
    "SOURCES",
    "SchemaVersionError",
    "allocate_activity_revision",
    "configure_connection",
    "insert_accepted_run",
    "insert_session",
    "migrate",
    "parse_instant",
    "pick_prune_reason",
    "record_trace_prune",
    "update_run_phase",
]


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade tables without committing a caller-owned transaction."""
    registry = MIGRATIONS
    _run_migrations(conn, registry)
