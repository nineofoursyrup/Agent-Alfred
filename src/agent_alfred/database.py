"""Open and migrate the managed SQLite database."""

from __future__ import annotations

import sqlite3
from functools import partial
from pathlib import PurePath

from agent_alfred import schema
from agent_alfred.managed_state import ManagedStateLease
from agent_alfred.resource_rollback import (
    ConstructionOwner,
    OwnedResource,
    ResumableRollback,
)


def _open_sqlite_connection(
    path: str,
    owner: OwnedResource[sqlite3.Connection],
    **kwargs: object,
) -> None:
    """Publish the concrete C-level SQLite result before this frame returns."""
    connect = sqlite3.connect
    if getattr(connect, "__code__", None) is not None:
        connect(path, owner, **kwargs)
        return
    owner.capture_c_result(partial(connect, path, **kwargs))


def open_database(
    state: ManagedStateLease, *, _rollback: ResumableRollback | None = None
) -> sqlite3.Connection:
    """Open and migrate the one write connection, owned before it is returned.

    ``_rollback`` is the caller's construction owner, established before this
    call: the file capability and the connection are owned there for the whole
    of construction, so an interrupted return edge -- or an interrupted caller
    store -- still leaves exactly one reachable owner for each of them.
    """
    owner = ConstructionOwner(_rollback)
    conn: sqlite3.Connection | None = None
    try:
        file_lease = state.open_regular(
            PurePath("db.sqlite3"),
            access="read_write",
            create=True,
            role="SQLite database",
            _rollback=owner.rollback,
        )
        token = file_lease.connection_token()
        conn = token.connect(
            _open_sqlite_connection,
            _rollback=owner.rollback,
            check_same_thread=False,
        )
        token.verify()
        state.verify_identity()
        schema.migrate(conn)
        file_lease.close()
        owner.publish(conn, parts=(file_lease,))
    except BaseException as exc:
        owner.fail(exc)
    assert conn is not None
    return conn
