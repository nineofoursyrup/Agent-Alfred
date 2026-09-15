"""Run, Session and activity clock writes; the caller owns the transaction."""

from __future__ import annotations

import sqlite3

from agent_alfred.outcomes import parse_run_outcome

from .activity import allocate_activity_revision as allocate_activity_revision
from .contracts import (
    OUTCOMES,
    RUN_PHASE_TRANSITIONS,
    RunOutcome,
    RunPhase,
    RunPhaseError,
)


def insert_session(
    conn: sqlite3.Connection, *, session_id: str, created_at: str
) -> int:
    """Insert a Session and stamp it with a newly allocated activity_revision."""
    revision = allocate_activity_revision(conn)
    conn.execute(
        """INSERT INTO sessions (session_id, created_at, activity_revision)
           VALUES (?, ?, ?)""",
        (session_id, created_at, revision),
    )
    return revision


def insert_accepted_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    purpose: str,
    session_id: str | None,
    gateway: str,
    accepted_at: str,
    entry_surface_id: str | None = None,
    prompt_preview: str | None = None,
) -> int:
    """Insert a Run in phase accepted with a null outcome.

    Allocates one activity_revision and copies it onto the Session when
    session_id is set. The INSERT itself is what the CHECK rejects if the
    phase/outcome pairing is wrong; callers do not pass phase or outcome.
    """
    revision = allocate_activity_revision(conn)
    conn.execute(
        """INSERT INTO runs (
             run_id, purpose, session_id, gateway, entry_surface_id,
             prompt_preview, phase, outcome, accepted_at, started_at,
             finished_at, activity_revision, telemetry, admission_state
           ) VALUES (
             ?, ?, ?, ?, ?, ?, 'accepted', NULL, ?, NULL, NULL, ?, NULL, 'pending'
           )""",
        (
            run_id,
            purpose,
            session_id,
            gateway,
            entry_surface_id,
            prompt_preview,
            accepted_at,
            revision,
        ),
    )
    if session_id is not None:
        updated = conn.execute(
            "UPDATE sessions SET activity_revision = ? WHERE session_id = ?",
            (revision, session_id),
        ).rowcount
        if updated != 1:
            raise RunPhaseError(
                f"expected exactly 1 session {session_id!r} to receive "
                f"activity_revision {revision}, updated {updated}"
            )
    return revision


def _check_transition(
    *, from_phase: RunPhase, to_phase: RunPhase, outcome: RunOutcome | None
) -> None:
    """Reject any transition the closed Run graph does not contain.

    Raises :class:`RunPhaseError` before the caller's UPDATE runs. The
    messages name phases and outcomes only -- never paths, payloads, or the
    caller -- so they are safe to surface.
    """
    if from_phase not in RUN_PHASE_TRANSITIONS:
        raise RunPhaseError(f"unknown from_phase {from_phase!r}")
    if from_phase == "finished":
        raise RunPhaseError(
            f"run is already in terminal phase {from_phase!r}; "
            f"a terminal run has no outgoing edge (refused -> {to_phase!r})"
        )
    if to_phase not in RUN_PHASE_TRANSITIONS:
        raise RunPhaseError(f"unknown to_phase {to_phase!r}")
    if to_phase not in RUN_PHASE_TRANSITIONS[from_phase]:
        allowed = ", ".join(RUN_PHASE_TRANSITIONS[from_phase]) or "none"
        raise RunPhaseError(
            f"illegal run transition {from_phase!r} -> {to_phase!r}; "
            f"allowed from {from_phase!r}: {allowed}"
        )
    if to_phase == "finished":
        try:
            terminal_outcome = parse_run_outcome(outcome)
        except ValueError as exc:
            raise RunPhaseError(
                f"phase 'finished' requires an outcome in "
                f"{', '.join(OUTCOMES)}, got {outcome!r}"
            ) from exc
        if from_phase == "accepted" and terminal_outcome in (
            "completed",
            "max_steps",
        ):
            raise RunPhaseError(
                f"an unstarted run cannot finish with execution outcome "
                f"{terminal_outcome!r}"
            )
    elif outcome is not None:
        raise RunPhaseError(
            f"phase {to_phase!r} is not terminal and must pair with a null "
            f"outcome, got {outcome!r}"
        )


def update_run_phase(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    from_phase: RunPhase,
    to_phase: RunPhase,
    activity_revision: int,
    outcome: RunOutcome | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    telemetry: str | None = None,
    session_id: str | None = None,
) -> None:
    """Move a Run from from_phase to to_phase. Must affect exactly one row.

    Two guards stack, and both are needed:

    - The transition graph is checked here, before any SQL runs, so no
      caller's ``from_phase`` can move a terminal Run or skip a phase. A
      rejected transition raises before the UPDATE, before the caller's
      activity_revision is spent, and before the Session is stamped, so the
      whole transaction rolls back with no clock hole.
    - The WHERE clause still carries the old phase, so a concurrent or
      repeated transition cannot silently rewrite a different state.
      rowcount != 1 is a hard failure, not a retry.

    The database CHECK remains the data-shape backstop; this function owns
    the graph.
    """
    _check_transition(from_phase=from_phase, to_phase=to_phase, outcome=outcome)
    assignments = ["phase = ?", "activity_revision = ?"]
    params: list[object] = [to_phase, activity_revision]
    if to_phase == "running":
        assignments.append("started_at = ?")
        params.append(started_at)
    elif to_phase == "finished":
        assignments.append("outcome = ?")
        params.append(outcome)
        assignments.append("finished_at = ?")
        params.append(finished_at)
        if telemetry is not None:
            assignments.append("telemetry = ?")
            params.append(telemetry)
    params.extend([run_id, from_phase])
    updated = conn.execute(
        f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = ? AND phase = ?",
        params,
    ).rowcount
    if updated != 1:
        raise RunPhaseError(
            f"expected exactly 1 run {run_id!r} in phase {from_phase!r}, "
            f"updated {updated}"
        )
    if session_id is not None:
        session_updated = conn.execute(
            "UPDATE sessions SET activity_revision = ? WHERE session_id = ?",
            (activity_revision, session_id),
        ).rowcount
        if session_updated != 1:
            raise RunPhaseError(
                f"expected exactly 1 session {session_id!r} to receive "
                f"activity_revision {activity_revision}, updated {session_updated}"
            )
