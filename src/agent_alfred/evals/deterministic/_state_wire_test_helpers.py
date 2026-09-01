"""Shared builders and shape checks for state snapshot wire tests."""

from __future__ import annotations


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
