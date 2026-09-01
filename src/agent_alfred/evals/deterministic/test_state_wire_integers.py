"""Integer facts crossing the run-state wire stay exact and non-negative."""

from __future__ import annotations

import pytest

from agent_alfred.gateway.web.progress import AttemptTerminal, StepProjection
from agent_alfred.gateway.web.state import (
    ActiveRunView,
    RunStateSnapshot,
    snapshot_from_payload,
    snapshot_payload,
)


@pytest.mark.parametrize(
    "value",
    [True, False, 1.5, "1", -1],
    ids=["true", "false", "float", "string", "negative"],
)
@pytest.mark.parametrize(
    "field",
    ["state_revision", "current_step", "step_index", "duration_ms"],
)
def test_snapshot_wire_rejects_invalid_integer_facts(field: str, value: object) -> None:
    wire = _wire_payload()
    if field == "current_step":
        active = wire["active_run"]
        assert isinstance(active, dict)
        active[field] = value
    elif field in ("step_index", "duration_ms"):
        step = wire["step"]
        assert isinstance(step, dict)
        if field == "duration_ms":
            attempts = step["attempts"]
            assert isinstance(attempts, list)
            attempt = attempts[0]
            assert isinstance(attempt, dict)
            attempt[field] = value
        else:
            step[field] = value
    else:
        wire[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


def test_nullable_current_step_accepts_none() -> None:
    wire = _wire_payload()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = None

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.active_run is not None
    assert rebuilt.active_run.current_step is None


def _wire_payload() -> dict[str, object]:
    return snapshot_payload(
        RunStateSnapshot(
            process_instance_id="process-1",
            state_revision=1,
            coordinator_state="running",
            active_run=ActiveRunView(
                run_id="run-1",
                purpose="chat",
                gateway="web",
                phase="running",
                outcome=None,
                session_id="session-1",
                prompt_preview="hello",
                started_at="2026-01-01T00:00:00Z",
                current_step=1,
                recording_state=None,
            ),
            step=StepProjection(
                step_index=1,
                attempts=(
                    AttemptTerminal(
                        attempt_id="attempt-1",
                        outcome="committed",
                        stop_reason="end_turn",
                        error_code=None,
                        duration_ms=5,
                    ),
                ),
            ),
            recording_state=None,
            session_valid=True,
            unrecorded_terminal_projection=None,
        )
    )
