"""A state patch is one possible, internally consistent lifecycle fact."""

from __future__ import annotations

import pytest

from agent_alfred.gateway.web.progress import StepProjection
from agent_alfred.gateway.web.state import (
    build_snapshot,
    snapshot_from_payload,
    snapshot_payload,
)
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)


def _running_wire() -> dict[str, object]:
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
            "current_step": None,
            "recording_state": None,
        },
        "step": None,
        "recording_state": None,
        "session_valid": True,
        "unrecorded_terminal_projection": None,
    }


def test_wire_rejects_idle_with_an_active_run() -> None:
    wire = _running_wire()
    wire["coordinator_state"] = "idle"

    with pytest.raises(ValueError, match="idle snapshot"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("state", "active_phase", "active_recording", "projection_recording"),
    [
        pytest.param("accepted", "accepted", None, None, id="accepted-no-active"),
        pytest.param("running", "running", None, None, id="running-no-active"),
        pytest.param(
            "recording_pending",
            "finished",
            "pending",
            "pending",
            id="pending-no-active",
        ),
        pytest.param(
            "recording_failed", "finished", "failed", "failed", id="failed-no-active"
        ),
    ],
)
def test_busy_wire_states_require_the_active_run(
    state: str,
    active_phase: str,
    active_recording: str | None,
    projection_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase=active_phase,
        active_outcome=("completed" if state.startswith("recording_") else None),
        active_recording=active_recording,
        projection_recording=projection_recording,
    )
    wire["active_run"] = None

    with pytest.raises(ValueError, match="active_run"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("state", "phase", "outcome", "recording"),
    [
        pytest.param("accepted", "running", None, None, id="accepted-running"),
        pytest.param("running", "accepted", None, None, id="running-accepted"),
        pytest.param(
            "recording_pending", "running", None, "pending", id="pending-running"
        ),
        pytest.param(
            "recording_failed", "finished", "completed", "pending", id="failed-pending"
        ),
    ],
)
def test_wire_rejects_coordinator_active_lifecycle_contradictions(
    state: str, phase: str, outcome: str | None, recording: str | None
) -> None:
    projection_recording = (
        "failed" if state == "recording_failed" else "pending"
        if state == "recording_pending"
        else None
    )
    wire = _lifecycle_wire(
        state,
        active_phase=phase,
        active_outcome=outcome,
        active_recording=recording,
        projection_recording=projection_recording,
    )

    with pytest.raises(ValueError, match="coordinator_state"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("state", "active_recording", "projection_recording"),
    [
        pytest.param("accepted", None, "pending", id="accepted-projection"),
        pytest.param("running", None, "pending", id="running-projection"),
        pytest.param("recording_pending", "pending", None, id="pending-no-projection"),
        pytest.param("recording_failed", "failed", None, id="failed-no-projection"),
        pytest.param(
            "recording_pending", "recorded", "pending", id="recorded-with-projection"
        ),
    ],
)
def test_wire_rejects_projection_presence_outside_the_real_transitions(
    state: str,
    active_recording: str | None,
    projection_recording: str | None,
) -> None:
    phase = "accepted" if state == "accepted" else "running"
    outcome = None
    if state.startswith("recording_"):
        phase = "finished"
        outcome = "completed"
    wire = _lifecycle_wire(
        state,
        active_phase=phase,
        active_outcome=outcome,
        active_recording=active_recording,
        projection_recording=projection_recording,
    )

    with pytest.raises(ValueError, match="projection"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("current_step", "step_index"),
    [
        pytest.param(None, 0, id="step-without-current"),
        pytest.param(2, 1, id="different-index"),
    ],
)
def test_wire_rejects_a_step_that_is_not_the_active_runs_current_step(
    current_step: int | None, step_index: int
) -> None:
    wire = _running_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = current_step
    wire["step"] = {
        "step_index": step_index,
        "attempts": [],
        "attempts_truncated": False,
    }

    with pytest.raises(ValueError, match="step"):
        snapshot_from_payload(wire)


def test_wire_rejects_a_step_without_an_active_run() -> None:
    wire = _running_wire()
    wire["active_run"] = None
    wire["step"] = {
        "step_index": 0,
        "attempts": [],
        "attempts_truncated": False,
    }

    with pytest.raises(ValueError, match="active_run"):
        snapshot_from_payload(wire)


def test_wire_rejects_accepted_snapshot_that_claims_execution_started() -> None:
    wire = _lifecycle_wire(
        "accepted",
        active_phase="accepted",
        active_recording=None,
        projection_recording=None,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = "2026-01-01T00:00:00Z"

    with pytest.raises(ValueError, match="started_at"):
        snapshot_from_payload(wire)


def test_build_snapshot_rejects_running_without_a_start_time() -> None:
    runtime = RuntimeSnapshot(
        process_instance_id="process-1",
        state_revision=1,
        coordinator_state="running",
        active_run=ActiveRunSummary(
            run_id="run-1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id="session-1",
            prompt_preview="hello",
            started_at=None,
            recording_state=None,
        ),
        unrecorded_terminal_projection=None,
    )

    with pytest.raises(ValueError, match="started_at"):
        build_snapshot(runtime, step=None, session_valid=True)


@pytest.mark.parametrize("seam", ["wire", "build"])
def test_running_requires_a_non_empty_start_time(seam: str) -> None:
    wire = _running_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = ""

    with pytest.raises(ValueError, match="started_at"):
        if seam == "wire":
            snapshot_from_payload(wire)
        else:
            build_snapshot(
                RuntimeSnapshot(
                    process_instance_id="process-1",
                    state_revision=1,
                    coordinator_state="running",
                    active_run=ActiveRunSummary(
                        run_id="run-1",
                        purpose="chat",
                        gateway="web",
                        phase="running",
                        session_id="session-1",
                        prompt_preview="hello",
                        started_at="",
                        recording_state=None,
                    ),
                    unrecorded_terminal_projection=None,
                ),
                step=None,
                session_valid=True,
            )


@pytest.mark.parametrize(
    ("state", "recording_state"),
    [
        pytest.param("recording_pending", "pending", id="pending"),
        pytest.param("recording_failed", "failed", id="failed"),
    ],
)
@pytest.mark.parametrize("seam", ["wire", "build"])
def test_terminal_recording_rejects_an_empty_execution_start_time(
    seam: str,
    state: str,
    recording_state: str,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase="finished",
        active_outcome="completed",
        active_recording=recording_state,
        projection_recording=recording_state,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = ""

    with pytest.raises(ValueError, match="started_at"):
        if seam == "wire":
            snapshot_from_payload(wire)
        else:
            build_snapshot(
                _terminal_runtime(
                    state=state,
                    outcome="completed",
                    recording_state=recording_state,
                    started_at="",
                    error=None,
                ),
                step=None,
                session_valid=True,
            )


@pytest.mark.parametrize("seam", ["wire", "build"])
@pytest.mark.parametrize("outcome", ["failed", "interrupted"])
@pytest.mark.parametrize(
    ("state", "recording_state", "projection_recording"),
    [
        pytest.param("recording_pending", "pending", "pending", id="pending"),
        pytest.param("recording_pending", "recorded", None, id="recorded"),
        pytest.param("recording_failed", "failed", "failed", id="failed"),
    ],
)
def test_terminal_recording_allows_a_null_execution_start_time(
    seam: str,
    outcome: str,
    state: str,
    recording_state: str,
    projection_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase="finished",
        active_outcome=outcome,
        active_recording=recording_state,
        projection_recording=projection_recording,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = None

    if seam == "wire":
        assert snapshot_payload(snapshot_from_payload(wire)) == wire
        return

    projection = None
    if projection_recording is not None:
        projection = UnrecordedTerminalProjection(
            run_id="run-1",
            purpose="chat",
            outcome=outcome,
            reply_text="done",
            error=None,
            recording_state=projection_recording,
            session_id="session-1",
            prompt_preview="hello",
        )
    built = build_snapshot(
        RuntimeSnapshot(
            process_instance_id="process-1",
            state_revision=1,
            coordinator_state=state,
            active_run=ActiveRunSummary(
                run_id="run-1",
                purpose="chat",
                gateway="web",
                phase="finished",
                outcome=outcome,
                session_id="session-1",
                prompt_preview="hello",
                started_at=None,
                recording_state=recording_state,
            ),
            unrecorded_terminal_projection=projection,
        ),
        step=None,
        session_valid=True,
    )
    assert snapshot_payload(built) == wire


@pytest.mark.parametrize("seam", ["wire", "build"])
@pytest.mark.parametrize("outcome", ["completed", "max_steps"])
@pytest.mark.parametrize(
    ("state", "recording_state", "projection_recording"),
    [
        pytest.param("recording_pending", "pending", "pending", id="pending"),
        pytest.param("recording_pending", "recorded", None, id="recorded"),
        pytest.param("recording_failed", "failed", "failed", id="failed"),
    ],
)
def test_started_terminal_allows_execution_outcomes_and_a_matching_step(
    seam: str,
    outcome: str,
    state: str,
    recording_state: str,
    projection_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase="finished",
        active_outcome=outcome,
        active_recording=recording_state,
        projection_recording=projection_recording,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = 2
    step = StepProjection(step_index=2, attempts=(), attempts_truncated=False)
    wire["step"] = {
        "step_index": 2,
        "attempts": [],
        "attempts_truncated": False,
    }

    if seam == "wire":
        assert snapshot_payload(snapshot_from_payload(wire)) == wire
        return

    built = build_snapshot(
        _terminal_runtime(
            state=state,
            outcome=outcome,
            recording_state=recording_state,
            started_at="2026-01-01T00:00:00Z",
            error=None,
            current_step=2,
        ),
        step=step,
        session_valid=True,
    )
    assert snapshot_payload(built) == wire


def test_anonymous_recording_failure_remains_a_valid_web_shape() -> None:
    wire = _lifecycle_wire(
        "recording_failed",
        active_phase=None,
        active_recording="failed",
        projection_recording=None,
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_unstarted_handoff_failure_remains_anonymous_when_built() -> None:
    built = build_snapshot(
        _terminal_runtime(
            state="recording_failed",
            outcome="interrupted",
            recording_state="failed",
            started_at=None,
            error="handoff_failed",
        ),
        step=None,
        session_valid=True,
    )
    payload = snapshot_payload(built)

    assert payload["coordinator_state"] == "recording_failed"
    assert payload["active_run"] is None
    assert payload["recording_state"] == "failed"
    assert payload["unrecorded_terminal_projection"] is None


@pytest.mark.parametrize("seam", ["wire", "build"])
@pytest.mark.parametrize("outcome", ["completed", "max_steps"])
@pytest.mark.parametrize(
    ("state", "recording_state", "projection_recording"),
    [
        pytest.param("recording_pending", "pending", "pending", id="pending"),
        pytest.param("recording_pending", "recorded", None, id="recorded"),
        pytest.param("recording_failed", "failed", "failed", id="failed"),
    ],
)
def test_unstarted_terminal_rejects_an_execution_outcome(
    seam: str,
    outcome: str,
    state: str,
    recording_state: str,
    projection_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase="finished",
        active_outcome=outcome,
        active_recording=recording_state,
        projection_recording=projection_recording,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = None

    with pytest.raises(ValueError, match="started_at"):
        if seam == "wire":
            snapshot_from_payload(wire)
        else:
            build_snapshot(
                _terminal_runtime(
                    state=state,
                    outcome=outcome,
                    recording_state=recording_state,
                    started_at=None,
                    error=None,
                ),
                step=None,
                session_valid=True,
            )


@pytest.mark.parametrize("seam", ["wire", "build"])
@pytest.mark.parametrize(
    ("state", "recording_state", "projection_recording"),
    [
        pytest.param("recording_pending", "pending", "pending", id="pending"),
        pytest.param("recording_pending", "recorded", None, id="recorded"),
        pytest.param("recording_failed", "failed", "failed", id="failed"),
    ],
)
def test_unstarted_terminal_rejects_current_step_and_step_projection(
    seam: str,
    state: str,
    recording_state: str,
    projection_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase="finished",
        active_outcome="failed",
        active_recording=recording_state,
        projection_recording=projection_recording,
    )
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["started_at"] = None
    active["current_step"] = 2
    step = StepProjection(step_index=2, attempts=(), attempts_truncated=False)
    wire["step"] = {
        "step_index": 2,
        "attempts": [],
        "attempts_truncated": False,
    }

    with pytest.raises(ValueError, match="started_at"):
        if seam == "wire":
            snapshot_from_payload(wire)
        else:
            build_snapshot(
                _terminal_runtime(
                    state=state,
                    outcome="failed",
                    recording_state=recording_state,
                    started_at=None,
                    error=None,
                    current_step=2,
                ),
                step=step,
                session_valid=True,
            )


def test_wire_rejects_current_step_without_its_projection() -> None:
    wire = _running_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = 3

    with pytest.raises(ValueError, match="step"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("top_recording", [None, "failed", "recorded"])
def test_wire_rejects_top_level_recording_that_disagrees_with_pending_authority(
    top_recording: str | None,
) -> None:
    wire = _lifecycle_wire(
        "recording_pending",
        active_phase="finished",
        active_outcome="completed",
        active_recording="pending",
        projection_recording="pending",
    )
    wire["recording_state"] = top_recording

    with pytest.raises(ValueError, match="recording_state"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("run_id", "run-2", id="run-id"),
        pytest.param("purpose", "probe", id="purpose"),
        pytest.param("session_id", None, id="session-id"),
        pytest.param("prompt_preview", "other", id="prompt-preview"),
        pytest.param("outcome", "failed", id="outcome"),
        pytest.param("recording_state", "failed", id="recording-state"),
    ],
)
def test_wire_rejects_projection_that_describes_a_different_run(
    field: str, value: object
) -> None:
    wire = _lifecycle_wire(
        "recording_pending",
        active_phase="finished",
        active_outcome="completed",
        active_recording="pending",
        projection_recording="pending",
    )
    projection = wire["unrecorded_terminal_projection"]
    assert isinstance(projection, dict)
    projection[field] = value

    with pytest.raises(ValueError, match=field):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("session_valid", [True, False])
@pytest.mark.parametrize(
    ("state", "phase", "outcome", "recording", "projection_recording"),
    [
        pytest.param("idle", None, None, None, None, id="idle"),
        pytest.param("accepted", "accepted", None, None, None, id="accepted"),
        pytest.param("running", "running", None, None, None, id="running-no-step"),
        pytest.param(
            "recording_pending",
            "finished",
            "completed",
            "pending",
            "pending",
            id="pending",
        ),
        pytest.param(
            "recording_failed", "finished", "completed", "failed", "failed", id="failed"
        ),
        pytest.param(
            "recording_pending",
            "finished",
            "completed",
            "recorded",
            None,
            id="recorded-settle",
        ),
    ],
)
def test_every_real_lifecycle_snapshot_round_trips(
    state: str,
    phase: str | None,
    outcome: str | None,
    recording: str | None,
    projection_recording: str | None,
    session_valid: bool,
) -> None:
    wire = _lifecycle_wire(
        state,
        active_phase=phase,
        active_outcome=outcome,
        active_recording=recording,
        projection_recording=projection_recording,
        session_valid=session_valid,
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_running_snapshot_with_matching_step_round_trips() -> None:
    wire = _running_wire()
    active = wire["active_run"]
    assert isinstance(active, dict)
    active["current_step"] = 3
    wire["step"] = {
        "step_index": 3,
        "attempts": [],
        "attempts_truncated": False,
    }

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_build_snapshot_rejects_an_impossible_runtime_combination() -> None:
    runtime = RuntimeSnapshot(
        process_instance_id="process-1",
        state_revision=1,
        coordinator_state="idle",
        active_run=ActiveRunSummary(
            run_id="run-1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id="session-1",
            prompt_preview="hello",
            started_at="2026-01-01T00:00:00Z",
            recording_state=None,
        ),
        unrecorded_terminal_projection=None,
    )

    with pytest.raises(ValueError, match="idle snapshot"):
        build_snapshot(runtime, step=None, session_valid=True)


def test_build_snapshot_reuses_the_step_relationship_validation() -> None:
    runtime = RuntimeSnapshot(
        process_instance_id="process-1",
        state_revision=1,
        coordinator_state="running",
        active_run=ActiveRunSummary(
            run_id="run-1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id="session-1",
            prompt_preview="hello",
            started_at="2026-01-01T00:00:00Z",
            recording_state=None,
            current_step=2,
        ),
        unrecorded_terminal_projection=None,
    )

    with pytest.raises(ValueError, match="step"):
        build_snapshot(
            runtime,
            step=StepProjection(step_index=1, attempts=(), attempts_truncated=False),
            session_valid=True,
        )


def _lifecycle_wire(
    state: str,
    *,
    active_phase: str | None,
    active_outcome: str | None = None,
    active_recording: str | None,
    projection_recording: str | None,
    session_valid: bool = True,
) -> dict[str, object]:
    wire = _running_wire()
    wire["coordinator_state"] = state
    wire["session_valid"] = session_valid
    if active_phase is None:
        wire["active_run"] = None
    else:
        active = wire["active_run"]
        assert isinstance(active, dict)
        active["phase"] = active_phase
        active["outcome"] = active_outcome
        active["recording_state"] = active_recording
        if active_phase == "accepted":
            active["started_at"] = None
    wire["recording_state"] = active_recording
    if projection_recording is not None:
        wire["unrecorded_terminal_projection"] = {
            "run_id": "run-1",
            "purpose": "chat",
            "outcome": active_outcome or "completed",
            "reply_preview": "done",
            "error": None,
            "recording_state": projection_recording,
            "session_id": "session-1",
            "prompt_preview": "hello",
        }
    return wire


def _unstarted_handoff_failure_wire() -> dict[str, object]:
    wire = _lifecycle_wire(
        "recording_failed",
        active_phase="finished",
        active_outcome="interrupted",
        active_recording="failed",
        projection_recording="failed",
    )
    active = wire["active_run"]
    projection = wire["unrecorded_terminal_projection"]
    assert isinstance(active, dict)
    assert isinstance(projection, dict)
    active["started_at"] = None
    projection["error"] = "handoff_failed"
    projection["reply_preview"] = None
    return wire


def _terminal_runtime(
    *,
    state: str,
    outcome: str,
    recording_state: str,
    started_at: str | None,
    error: str | None,
    current_step: int | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        process_instance_id="process-1",
        state_revision=1,
        coordinator_state=state,
        active_run=ActiveRunSummary(
            run_id="run-1",
            purpose="chat",
            gateway="web",
            phase="finished",
            outcome=outcome,
            session_id="session-1",
            prompt_preview="hello",
            started_at=started_at,
            recording_state=recording_state,
            current_step=current_step,
        ),
        unrecorded_terminal_projection=(
            None
            if recording_state == "recorded"
            else UnrecordedTerminalProjection(
                run_id="run-1",
                purpose="chat",
                outcome=outcome,
                reply_text=None if error == "handoff_failed" else "done",
                error=error,
                recording_state=recording_state,
                session_id="session-1",
                prompt_preview="hello",
            )
        ),
    )
