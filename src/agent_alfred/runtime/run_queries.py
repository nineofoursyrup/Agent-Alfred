"""Shared read-only SQL for Run-backed Session views."""

from __future__ import annotations

from agent_alfred.run_phases import IN_FLIGHT_RUN_PHASES


def _runs_beyond_clause() -> str:
    return (
        "AND (runs.activity_revision > ?"
        " OR (runs.activity_revision = ? AND runs.run_id > ?))\n"
    )


def has_inflight_chat_run(
    conn,
    *,
    session_id: str,
    position: tuple[int, str] | None,
    recording_failed_run_ids: frozenset[str],
) -> bool:
    """Return whether a chat Run beyond ``position`` may still record."""
    sql = """
        SELECT 1 FROM runs
        WHERE runs.session_id = ?
          AND runs.purpose = 'chat'
          AND runs.phase IN (?, ?)
          {failed}
          {beyond}
        LIMIT 1
    """
    params: list = [session_id, *IN_FLIGHT_RUN_PHASES]
    failed_clause = ""
    if recording_failed_run_ids:
        marks = ", ".join("?" for _ in recording_failed_run_ids)
        failed_clause = f"AND runs.run_id NOT IN ({marks})\n"
        params.extend(sorted(recording_failed_run_ids))
    beyond_clause = ""
    if position is not None:
        position_ar, position_r = position
        beyond_clause = _runs_beyond_clause()
        params.extend([position_ar, position_ar, position_r])
    row = conn.execute(
        sql.format(failed=failed_clause, beyond=beyond_clause), params
    ).fetchone()
    return row is not None


def page_recorded_chat_run_keys(
    conn,
    *,
    session_id: str,
    position: tuple[int, str] | None,
    count: int,
) -> list[tuple[int, str]]:
    """Return ascending recorded chat Run keys after ``position``."""
    sql = """
        SELECT runs.activity_revision, runs.run_id FROM runs
        WHERE runs.session_id = ?
          AND runs.purpose = 'chat'
          AND EXISTS (
            SELECT 1 FROM agent_log WHERE agent_log.run_id = runs.run_id
          )
          {keyset}
        ORDER BY runs.activity_revision ASC, runs.run_id ASC
        LIMIT ?
    """
    if position is None:
        rows = conn.execute(sql.format(keyset=""), (session_id, count)).fetchall()
    else:
        position_ar, position_r = position
        rows = conn.execute(
            sql.format(keyset=_runs_beyond_clause()),
            (session_id, position_ar, position_ar, position_r, count),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]
