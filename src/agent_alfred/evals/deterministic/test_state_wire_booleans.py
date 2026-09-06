"""Boolean facts crossing the run-state wire remain exact booleans."""

from __future__ import annotations

import pytest

from agent_alfred.gateway.web.progress import StepProjection
from agent_alfred.gateway.web.state import (
    ActiveRunView,
    RunStateSnapshot,
    snapshot_from_payload,
    snapshot_payload,
)


@pytest.mark.parametrize(
    "value",
    ["false", 0, 1, None, [], {}],
    ids=["string-false", "zero", "one", "null", "list", "object"],
)
@pytest.mark.parametrize(
    "field",
    ["attempts_truncated", "session_valid"],
)
def test_snapshot_wire_rejects_non_boolean_facts(field: str, value: object) -> None:
    wire = _wire_payload()
    if field == "attempts_truncated":
        step = wire["step"]
        assert isinstance(step, dict)
        step[field] = value
    else:
        wire[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    "field",
    ["attempts_truncated", "session_valid"],
)
def test_snapshot_wire_requires_boolean_fact_keys(field: str) -> None:
    wire = _wire_payload()
    if field == "attempts_truncated":
        step = wire["step"]
        assert isinstance(step, dict)
        del step[field]
    else:
        del wire[field]

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("attempts_truncated", [False, True])
@pytest.mark.parametrize("session_valid", [False, True])
def test_snapshot_wire_boolean_facts_round_trip_exactly(
    attempts_truncated: bool, session_valid: bool
) -> None:
    wire = _wire_payload(
        attempts_truncated=attempts_truncated,
        session_valid=session_valid,
    )

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.step is not None
    assert rebuilt.step.attempts_truncated is attempts_truncated
    assert rebuilt.session_valid is session_valid
    assert snapshot_payload(rebuilt) == wire


def _wire_payload(
    *, attempts_truncated: bool = False, session_valid: bool = True
) -> dict[str, object]:
    snapshot = RunStateSnapshot(
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
            attempts=(),
            attempts_truncated=attempts_truncated,
        ),
        recording_state=None,
        session_valid=session_valid,
        unrecorded_terminal_projection=None,
    )
    return snapshot_payload(snapshot)
