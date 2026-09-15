"""Migration admission and execution within caller-owned transactions."""
from __future__ import annotations

import sqlite3

from .contracts import (
    BUSY_TIMEOUT_MS,
    RETIRED_OBJECTS,
    Fts5UnavailableError,
    Migration,
    SchemaVersionError,
)

_UNVERSIONED_DATABASE = """\
Refusing to migrate: this database has no schema_migrations ledger, yet it \
already holds objects this schema owns: {objects}.

Registering it as version {version} would vouch for a shape nothing verified -- \
an old table whose columns have since drifted would be left exactly as it is \
and the run would report success. Back the database file up, export whatever \
rows you still need, and point migrate() at a fresh database.
"""


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


def run_migrations(
    conn: sqlite3.Connection, registry: tuple[Migration, ...]
) -> None:
    """Create or upgrade tables. Idempotent. Does not commit a caller transaction.

    Inside a transaction the caller opened, every pending migration runs under
    one SAVEPOINT: it is released on success and rolled back to on failure, so a
    migration that dies half-way takes its own DDL with it and leaves whatever
    the caller had already written still in play, still uncommitted, still the
    caller's to commit or roll back.
    """
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
                # datetime('now') is a naive string, which parse_instant
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
