"""Recording state is one closed runtime-domain set."""

from __future__ import annotations

from importlib import import_module
from typing import get_type_hints

import pytest

from agent_alfred.gateway.web.state import (
    ActiveRunView,
    RunStateSnapshot,
    StatePatchRejected,
    UnrecordedTerminalView,
    apply_state_patch,
    build_snapshot,
    snapshot_from_payload,
)
from agent_alfred.runtime.recording_state import parse_recording_state
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)


class _EqualToPending:
    def __eq__(self, other: object) -> bool:
        return other == "pending"


_NO_PROJECTION = object()


def test_runtime_and_wire_views_reuse_domain_recording_states() -> None:
    recording = import_module("agent_alfred.runtime.recording_state")

    assert get_type_hints(ActiveRunSummary)["recording_state"] == (
        recording.RecordingState | None
    )
    assert get_type_hints(UnrecordedTerminalProjection)["recording_state"] is (
        recording.UnrecordedTerminalState
    )
    assert get_type_hints(ActiveRunView)["recording_state"] == (
        recording.RecordingState | None
    )
    assert get_type_hints(UnrecordedTerminalView)["recording_state"] is (
        recording.UnrecordedTerminalState
    )
    assert get_type_hints(RunStateSnapshot)["recording_state"] == (
        recording.RecordingState | None
    )


@pytest.mark.parametrize("state", ["pending", "recorded", "failed"])
def test_every_recording_state_is_accepted(state: object) -> None:
    assert parse_recording_state(state) == state


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_every_unrecorded_terminal_state_is_accepted(state: object) -> None:
    assert parse_recording_state(state, unrecorded_terminal=True) == state


def test_nullable_recording_state_accepts_none() -> None:
    assert parse_recording_state(None, allow_none=True) is None


@pytest.mark.parametrize(
    "state",
    [None, "unknown", 1, _EqualToPending()],
    ids=["null", "unknown", "non-string", "non-string-equality-impostor"],
)
def test_required_recording_state_rejects_invalid_values(state: object) -> None:
    with pytest.raises(ValueError, match="recording state"):
        parse_recording_state(state)


def test_unrecorded_terminal_parser_rejects_recorded() -> None:
    with pytest.raises(ValueError, match="unrecorded terminal recording state"):
        parse_recording_state("recorded", unrecorded_terminal=True)


def test_unrecorded_terminal_projection_rejects_recorded() -> None:
    with pytest.raises(ValueError, match="unrecorded terminal recording state"):
        _projection("recorded")


@pytest.mark.parametrize(
    "state",
    ["unknown", 1, _EqualToPending()],
    ids=["unknown", "non-string", "non-string-equality-impostor"],
)
def test_active_run_summary_rejects_invalid_recording_states(state: object) -> None:
    with pytest.raises(ValueError, match="recording state"):
        ActiveRunSummary(
            run_id="run-1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id="session-1",
            prompt_preview="hello",
            started_at="2026-01-01T00:00:00Z",
            recording_state=state,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("state", [None, "pending", "recorded", "failed"])
def test_runtime_active_recording_state_is_preserved_in_the_web_view(
    state: object,
) -> None:
    active = ActiveRunSummary(
        run_id="run-1",
        purpose="chat",
        gateway="web",
        phase="finished",
        session_id="session-1",
        prompt_preview="hello",
        started_at="2026-01-01T00:00:00Z",
        recording_state=state,  # type: ignore[arg-type]
        outcome="completed",
    )
    runtime = RuntimeSnapshot("process-1", 1, "idle", active, None)

    view = build_snapshot(runtime, step=None, session_valid=True)

    assert view.recording_state == state
    assert view.active_run is not None
    assert view.active_run.recording_state == state
    assert view.active_run.outcome == "completed"


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_runtime_unrecorded_terminal_state_is_preserved_in_the_web_view(
    state: object,
) -> None:
    projection = _projection(state)
    runtime = RuntimeSnapshot("process-1", 1, "idle", None, projection)

    view = build_snapshot(runtime, step=None, session_valid=True)

    assert view.recording_state == state
    assert view.unrecorded_terminal_projection is not None
    assert view.unrecorded_terminal_projection.recording_state == state
    assert view.unrecorded_terminal_projection.outcome == "completed"


def test_nullable_wire_fields_accept_null() -> None:
    wire = _wire_payload()
    active = wire["active_run"]
    assert isinstance(active, dict)
    wire["recording_state"] = None
    active["recording_state"] = None

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.recording_state is None
    assert rebuilt.active_run is not None
    assert rebuilt.active_run.recording_state is None


@pytest.mark.parametrize("state", ["pending", "recorded", "failed"])
@pytest.mark.parametrize("target", ["top-level", "active"])
def test_every_recording_state_crosses_the_wire(target: str, state: object) -> None:
    wire = _wire_payload()
    if target == "top-level":
        wire["recording_state"] = state
    else:
        active = wire["active_run"]
        assert isinstance(active, dict)
        active["recording_state"] = state

    rebuilt = snapshot_from_payload(wire)

    actual = (
        rebuilt.recording_state
        if target == "top-level"
        else rebuilt.active_run.recording_state if rebuilt.active_run else None
    )
    assert actual == state


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_every_unrecorded_terminal_state_crosses_the_wire(state: object) -> None:
    wire = _wire_payload(projection_state=state)

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.unrecorded_terminal_projection is not None
    assert rebuilt.unrecorded_terminal_projection.recording_state == state


@pytest.mark.parametrize("target", ["top-level", "active"])
@pytest.mark.parametrize(
    "state",
    ["unknown", 1, _EqualToPending()],
    ids=["unknown", "non-string", "non-string-equality-impostor"],
)
def test_wire_rejects_invalid_nullable_recording_states(
    target: str, state: object
) -> None:
    wire = _wire_payload()
    if target == "top-level":
        wire["recording_state"] = state
    else:
        active = wire["active_run"]
        assert isinstance(active, dict)
        active["recording_state"] = state

    with pytest.raises(ValueError, match="recording state"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    "state",
    [None, "recorded", "unknown", 1, _EqualToPending(), ...],
    ids=[
        "null",
        "recorded",
        "unknown",
        "non-string",
        "non-string-equality-impostor",
        "missing",
    ],
)
def test_wire_rejects_invalid_unrecorded_terminal_states(state: object) -> None:
    wire = _wire_payload(projection_state=state)

    error = (
        "unrecorded_terminal_projection"
        if state is ...
        else "unrecorded terminal recording state"
    )
    with pytest.raises(ValueError, match=error):
        snapshot_from_payload(wire)


def test_parsed_pending_wire_patch_cannot_overwrite_recorded_state() -> None:
    current_wire = _wire_payload()
    current_active = current_wire["active_run"]
    assert isinstance(current_active, dict)
    current_active["recording_state"] = "recorded"
    current = snapshot_from_payload(current_wire)

    patch_wire = _wire_payload()
    patch_wire["state_revision"] = 2
    patch_active = patch_wire["active_run"]
    assert isinstance(patch_active, dict)
    patch_active["recording_state"] = "pending"
    patch = snapshot_from_payload(patch_wire)

    with pytest.raises(StatePatchRejected, match="pending_over_terminal"):
        apply_state_patch(current, patch)


def _projection(recording_state: object) -> UnrecordedTerminalProjection:
    return UnrecordedTerminalProjection(
        run_id="run-1",
        purpose="chat",
        outcome="completed",
        reply_text="done",
        error=None,
        recording_state=recording_state,  # type: ignore[arg-type]
        session_id="session-1",
        prompt_preview="hello",
    )


def _wire_payload(*, projection_state: object = _NO_PROJECTION) -> dict[str, object]:
    active = {
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
    }
    projection = None
    if projection_state is not _NO_PROJECTION:
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
    return {
        "process_instance_id": "process-1",
        "state_revision": 1,
        "coordinator_state": "running",
        "active_run": active,
        "step": None,
        "recording_state": None,
        "session_valid": True,
        "unrecorded_terminal_projection": projection,
    }
