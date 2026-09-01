"""Run outcome is one closed set, imported everywhere."""

from __future__ import annotations

from typing import get_type_hints

import pytest

from agent_alfred.events import RunOutcome as EventOutcome
from agent_alfred.gateway.web.state import (
    ActiveRunView,
    RunStateSnapshot,
    UnrecordedTerminalView,
    snapshot_from_payload,
    snapshot_payload,
)
from agent_alfred.loop.assistant import RunOutcome as LoopOutcome
from agent_alfred.outcomes import RUN_OUTCOMES, RunOutcome
from agent_alfred.runtime.recording import RunRecorder
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RunPhase,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.schema import OUTCOMES

_NO_PROJECTION = object()


class LiteralImpostor:
    """A non-string that claims equality with one chosen wire literal."""

    def __init__(self, literal: str) -> None:
        self.literal = literal

    def __eq__(self, other: object) -> bool:
        return other == self.literal


def test_run_outcome_is_a_single_closed_set() -> None:
    assert LoopOutcome is RunOutcome
    assert EventOutcome is RunOutcome
    assert OUTCOMES == RUN_OUTCOMES
    assert RUN_OUTCOMES == ("completed", "max_steps", "failed", "interrupted")


def test_authoritative_snapshot_and_wire_views_reuse_run_outcome() -> None:
    assert get_type_hints(ActiveRunSummary)["outcome"] == RunOutcome | None
    assert get_type_hints(UnrecordedTerminalProjection)["outcome"] is RunOutcome
    assert get_type_hints(ActiveRunView)["outcome"] == RunOutcome | None
    assert get_type_hints(UnrecordedTerminalView)["outcome"] is RunOutcome
    assert get_type_hints(RunRecorder.settle)["outcome"] is RunOutcome


def test_authoritative_snapshot_and_wire_views_reuse_lifecycle_types() -> None:
    assert get_type_hints(ActiveRunSummary)["phase"] is RunPhase
    assert get_type_hints(ActiveRunView)["phase"] is RunPhase
    assert get_type_hints(RuntimeSnapshot)["coordinator_state"] is CoordinatorState
    assert get_type_hints(RunStateSnapshot)["coordinator_state"] is CoordinatorState


@pytest.mark.parametrize("outcome", RUN_OUTCOMES)
def test_every_run_outcome_round_trips_through_the_wire(outcome: RunOutcome) -> None:
    wire = _wire_payload(
        active_outcome=outcome,
        projection_outcome=outcome,
        active_phase="finished",
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


@pytest.mark.parametrize("phase", ["accepted", "running"])
def test_an_active_nonterminal_run_may_have_no_outcome(phase: RunPhase) -> None:
    wire = _wire_payload(active_outcome=None, active_phase=phase)

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.active_run is not None
    assert rebuilt.active_run.outcome is None


@pytest.mark.parametrize("phase", ["accepted", "running", "finished"])
@pytest.mark.parametrize(
    "coordinator_state",
    ["idle", "accepted", "running", "recording_pending", "recording_failed"],
)
def test_every_lifecycle_literal_round_trips_through_the_wire(
    phase: RunPhase, coordinator_state: CoordinatorState
) -> None:
    outcome = "completed" if phase == "finished" else None
    wire = _wire_payload(
        active_outcome=outcome,
        active_phase=phase,
        coordinator_state=coordinator_state,
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        pytest.param("phase", "unknown", "run phase", id="phase-unknown"),
        pytest.param("phase", 1, "run phase", id="phase-non-string"),
        pytest.param(
            "phase",
            LiteralImpostor("finished"),
            "run phase",
            id="phase-literal-impostor",
        ),
        pytest.param(
            "coordinator_state",
            "unknown",
            "coordinator state",
            id="coordinator-unknown",
        ),
        pytest.param(
            "coordinator_state",
            1,
            "coordinator state",
            id="coordinator-non-string",
        ),
        pytest.param(
            "coordinator_state",
            LiteralImpostor("running"),
            "coordinator state",
            id="coordinator-literal-impostor",
        ),
    ],
)
def test_wire_rejects_invalid_lifecycle_literals(
    field: str, value: object, error: str
) -> None:
    wire = _wire_payload(active_outcome=None)
    if field == "phase":
        active = wire["active_run"]
        assert isinstance(active, dict)
        active["phase"] = value
    else:
        wire["coordinator_state"] = value

    with pytest.raises(ValueError, match=error):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("field", ["phase", "coordinator_state"])
def test_wire_requires_lifecycle_keys(field: str) -> None:
    wire = _wire_payload(active_outcome=None)
    if field == "phase":
        active = wire["active_run"]
        assert isinstance(active, dict)
        del active["phase"]
    else:
        del wire["coordinator_state"]

    with pytest.raises(KeyError):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("target", "outcome"),
    [
        pytest.param("active", 1, id="active-non-string"),
        pytest.param("active", "unknown", id="active-unknown"),
        pytest.param("active", None, id="active-terminal-null"),
        pytest.param("active", ..., id="active-terminal-missing"),
        pytest.param("projection", 1, id="projection-non-string"),
        pytest.param("projection", "unknown", id="projection-unknown"),
        pytest.param("projection", None, id="projection-null"),
        pytest.param("projection", ..., id="projection-missing"),
    ],
)
def test_wire_rejects_invalid_run_outcomes(target: str, outcome: object) -> None:
    kwargs: dict[str, object] = {"active_outcome": None}
    if target == "active":
        kwargs.update(active_outcome=outcome, active_phase="finished")
    else:
        kwargs["projection_outcome"] = outcome
    wire = _wire_payload(**kwargs)

    with pytest.raises(ValueError, match="run outcome"):
        snapshot_from_payload(wire)


def _wire_payload(
    *,
    active_outcome: object,
    projection_outcome: object = _NO_PROJECTION,
    active_phase: object = "running",
    coordinator_state: object = "running",
) -> dict[str, object]:
    active = {
        "run_id": "run-1",
        "purpose": "chat",
        "gateway": "web",
        "phase": active_phase,
        "outcome": active_outcome,
        "session_id": "session-1",
        "prompt_preview": "hello",
        "started_at": "2026-01-01T00:00:00Z",
        "current_step": 1,
        "recording_state": None,
    }
    if active_outcome is ...:
        del active["outcome"]
    projection = None
    if projection_outcome is not _NO_PROJECTION:
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
    return {
        "process_instance_id": "process-1",
        "state_revision": 1,
        "coordinator_state": coordinator_state,
        "active_run": active,
        "step": None,
        "recording_state": None,
        "session_valid": True,
        "unrecorded_terminal_projection": projection,
    }
