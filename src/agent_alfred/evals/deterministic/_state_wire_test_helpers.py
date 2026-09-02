"""Shared builders and shape checks for state snapshot wire tests."""

from __future__ import annotations

NO_PROJECTION = object()


def running_snapshot_wire() -> dict[str, object]:
    """Return a fresh valid wire snapshot for one running attempt."""
    return {
        "process_instance_id": "process-1",
        "state_revision": 1,
        "coordinator_state": "running",
        "active_run": {
            "run_id": "run-1",
            "purpose": "chat",
            "gateway": "web",
            "phase": "running",
            "outcome": None,
            "session_id": "session-1",
            "prompt_preview": "hello",
            "started_at": "2026-01-01T00:00:00Z",
            "current_step": 1,
            "recording_state": None,
        },
        "step": {
            "step_index": 1,
            "attempts": [
                {
                    "attempt_id": "attempt-1",
                    "outcome": "committed",
                    "stop_reason": "end_turn",
                    "error_code": None,
                    "duration_ms": 5,
                }
            ],
            "attempts_truncated": False,
        },
        "recording_state": None,
        "session_valid": True,
        "unrecorded_terminal_projection": None,
    }


def only_attempt_in_snapshot_wire(
    snapshot_wire: dict[str, object],
) -> dict[str, object]:
    """Return the sole attempt after asserting the expected container shape."""
    step = snapshot_wire["step"]
    assert isinstance(step, dict)
    attempts = step["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert isinstance(attempt, dict)
    assert set(attempt) == {
        "attempt_id",
        "outcome",
        "stop_reason",
        "error_code",
        "duration_ms",
    }
    return attempt


def outcome_snapshot_wire(
    *,
    active_outcome: object,
    projection_outcome: object = NO_PROJECTION,
    active_phase: object = "running",
    coordinator_state: object = "running",
) -> dict[str, object]:
    """Return a fresh snapshot specialized for outcome validation."""
    wire = running_snapshot_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["phase"] = active_phase
    active["outcome"] = active_outcome
    if active_outcome is ...:
        del active["outcome"]
    projection = None
    if projection_outcome is not NO_PROJECTION:
        projection = {
            "run_id": "run-1",
            "purpose": "chat",
            "outcome": projection_outcome,
            "reply_preview": "done",
            "error": None,
            "recording_state": "pending",
            "session_id": "session-1",
            "prompt_preview": "hello",
        }
        if projection_outcome is ...:
            del projection["outcome"]
    if active_phase == "accepted":
        active["started_at"] = None
        active["current_step"] = None
    recording_state = None
    if active_phase == "finished":
        recording_state = (
            "failed"
            if coordinator_state == "recording_failed"
            else "pending"
            if projection is not None
            else "recorded"
        )
        active["recording_state"] = recording_state
    if projection is not None and coordinator_state == "recording_failed":
        projection["recording_state"] = "failed"
    wire["coordinator_state"] = coordinator_state
    wire["step"] = (
        None
        if active["current_step"] is None
        else {
            "step_index": active["current_step"],
            "attempts": [],
            "attempts_truncated": False,
        }
    )
    wire["recording_state"] = recording_state
    wire["unrecorded_terminal_projection"] = projection
    return wire


def recording_snapshot_wire(
    *, projection_state: object = NO_PROJECTION
) -> dict[str, object]:
    """Return a fresh snapshot specialized for recording-state validation."""
    wire = running_snapshot_wire()
    active = wire["active_run"]
    step = wire["step"]
    assert isinstance(active, dict)
    assert isinstance(step, dict)
    step["attempts"] = []
    projection = None
    if projection_state is not NO_PROJECTION:
        projection = {
            "run_id": "run-1",
            "purpose": "chat",
            "outcome": "completed",
            "reply_preview": "done",
            "error": None,
            "recording_state": projection_state,
            "session_id": "session-1",
            "prompt_preview": "hello",
        }
        if projection_state is ...:
            del projection["recording_state"]
    wire["unrecorded_terminal_projection"] = projection
    if type(projection_state) is str and projection_state in ("pending", "failed"):
        active["phase"] = "finished"
        active["outcome"] = "completed"
        active["recording_state"] = projection_state
        wire["coordinator_state"] = (
            "recording_failed"
            if projection_state == "failed"
            else "recording_pending"
        )
        wire["recording_state"] = projection_state
    return wire


def relation_running_snapshot_wire() -> dict[str, object]:
    """Return a fresh running snapshot with no current Step projection."""
    wire = running_snapshot_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = None
    wire["step"] = None
    return wire


def shaped_snapshot_wire(
    *, with_step: bool = False, with_projection: bool = False
) -> dict[str, object]:
    """Return a fresh snapshot for closed wire-shape validation."""
    wire = running_snapshot_wire()
    if not with_step:
        wire["step"] = None
    wire["unrecorded_terminal_projection"] = (
        {
            "run_id": "run-1",
            "purpose": "chat",
            "outcome": "completed",
            "reply_preview": "done",
            "error": None,
            "recording_state": "pending",
            "session_id": "session-1",
            "prompt_preview": "hello",
        }
        if with_projection
        else None
    )
    if with_projection:
        wire["coordinator_state"] = "recording_pending"
        wire["recording_state"] = "pending"
        active = wire["active_run"]
        assert isinstance(active, dict)
        active["phase"] = "finished"
        active["outcome"] = "completed"
        active["recording_state"] = "pending"
    return wire
