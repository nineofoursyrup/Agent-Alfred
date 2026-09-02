"""Open and migrate the managed SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import PurePath

from agent_alfred import schema
from agent_alfred.managed_state import ManagedStateLease
from agent_alfred.resource_rollback import ResumableRollback


def open_database(state: ManagedStateLease) -> sqlite3.Connection:
    file_lease = state.open_regular(
        PurePath("db.sqlite3"),
        access="read_write",
        create=True,
        role="SQLite database",
    )
    rollback = ResumableRollback()
    rollback.own(file_lease)
    conn: sqlite3.Connection | None = None
    try:
        token = file_lease.connection_token()
        conn = token.connect(sqlite3.connect, check_same_thread=False)
        rollback.own(conn)
        token.verify()
        schema.migrate(conn)
        file_lease.close()
        rollback.transfer(file_lease)
    except BaseException as exc:
        rollback.raise_failure(exc)
    assert conn is not None
    rollback.transfer(conn)
    return conn
