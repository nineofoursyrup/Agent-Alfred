"""Shared activity clock allocation for historical backfill and runtime writes."""
import sqlite3

from .contracts import RunPhaseError


def allocate_activity_revision(conn: sqlite3.Connection) -> int:
    """Take the next activity_revision from the single-row clock.

    Must run inside the caller's transaction: Session creation and Run
    phase transfers stamp the same number they just allocated.
    """
    row = conn.execute(
        """UPDATE activity_clock
           SET next_revision = next_revision + 1
           WHERE id = 1
           RETURNING next_revision - 1"""
    ).fetchone()
    if row is None:
        raise RunPhaseError("activity clock is missing")
    return int(row[0])
