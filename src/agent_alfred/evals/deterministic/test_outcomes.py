"""Run outcome is one closed set, imported everywhere."""

from __future__ import annotations

from typing import get_type_hints

import pytest

from agent_alfred.evals.deterministic._state_wire_test_helpers import (
    NO_PROJECTION,
    outcome_snapshot_wire,
)
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
from agent_alfred.run_phases import (
    RUN_PHASES,
    RunPhase,
    parse_run_phase,
)
from agent_alfred.runtime.recording import RunRecorder
from agent_alfred.runtime.runs import RunSummary, SessionChatRun
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.snapshot import (
    parse_run_phase as snapshot_parse_run_phase,
)
from agent_alfred.schema import OUTCOMES, PHASES


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


def test_run_phase_is_a_single_closed_set() -> None:
    assert PHASES is RUN_PHASES
    assert snapshot_parse_run_phase is parse_run_phase
    assert RUN_PHASES == ("accepted", "running", "finished")


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


def test_persisted_run_views_reuse_lifecycle_types() -> None:
    assert get_type_hints(RunSummary)["phase"] is RunPhase
    assert get_type_hints(RunSummary)["outcome"] == RunOutcome | None
    assert get_type_hints(SessionChatRun)["phase"] is RunPhase
    assert get_type_hints(SessionChatRun)["outcome"] == RunOutcome | None


@pytest.mark.parametrize("outcome", RUN_OUTCOMES)
def test_every_run_outcome_round_trips_through_the_wire(outcome: RunOutcome) -> None:
    wire = outcome_snapshot_wire(
        active_outcome=outcome,
        projection_outcome=outcome,
        active_phase="finished",
        coordinator_state="recording_pending",
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


@pytest.mark.parametrize("phase", ["accepted", "running"])
def test_an_active_nonterminal_run_may_have_no_outcome(phase: RunPhase) -> None:
    wire = outcome_snapshot_wire(
        active_outcome=None, active_phase=phase, coordinator_state=phase
    )

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.active_run is not None
    assert rebuilt.active_run.outcome is None


@pytest.mark.parametrize("phase", ["accepted", "running"])
@pytest.mark.parametrize("outcome", RUN_OUTCOMES)
def test_wire_rejects_an_outcome_before_the_run_is_finished(
    phase: RunPhase, outcome: RunOutcome
) -> None:
    wire = outcome_snapshot_wire(active_outcome=outcome, active_phase=phase)

    with pytest.raises(ValueError, match="run lifecycle"):
        snapshot_from_payload(wire)


@pytest.mark.parametrize("phase", ["accepted", "running"])
@pytest.mark.parametrize("outcome", RUN_OUTCOMES)
def test_authoritative_summary_rejects_an_outcome_before_the_run_is_finished(
    phase: RunPhase, outcome: RunOutcome
) -> None:
    with pytest.raises(ValueError, match="run lifecycle"):
        _active_run_summary(phase=phase, outcome=outcome)


def test_authoritative_summary_rejects_a_finished_run_without_an_outcome() -> None:
    with pytest.raises(ValueError, match="run lifecycle"):
        _active_run_summary(phase="finished", outcome=None)


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        pytest.param("accepted", None, id="accepted"),
        pytest.param("running", None, id="running"),
        *(
            pytest.param("finished", outcome, id=f"finished-{outcome}")
            for outcome in RUN_OUTCOMES
        ),
    ],
)
def test_authoritative_summary_accepts_every_valid_lifecycle_pair(
    phase: RunPhase, outcome: RunOutcome | None
) -> None:
    summary = _active_run_summary(phase=phase, outcome=outcome)

    assert (summary.phase, summary.outcome) == (phase, outcome)


@pytest.mark.parametrize("model", [RunSummary, SessionChatRun])
@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        pytest.param("running", "completed", id="nonterminal-with-outcome"),
        pytest.param("finished", None, id="terminal-without-outcome"),
    ],
)
def test_public_persisted_run_models_reject_impossible_lifecycle_pairs(
    model: type[RunSummary] | type[SessionChatRun],
    phase: RunPhase,
    outcome: RunOutcome | None,
) -> None:
    kwargs: dict[str, object] = {
        "run_id": "run-1",
        "phase": phase,
        "outcome": outcome,
        "accepted_at": "2026-01-01T00:00:00Z",
        "started_at": None,
        "finished_at": None,
        "activity_revision": 1,
    }
    if model is RunSummary:
        kwargs.update(
            purpose="chat",
            filter="chat",
            purpose_known=True,
            session_id="session-1",
            gateway="web",
            entry_surface_id=None,
            prompt_preview="hello",
        )
    else:
        kwargs.update(gateway="web", reply_preview=None, reply_source=None)

    with pytest.raises(ValueError, match="run lifecycle"):
        model(**kwargs)


@pytest.mark.parametrize(
    ("phase", "coordinator_state"),
    [
        pytest.param(None, "idle", id="idle"),
        pytest.param("accepted", "accepted", id="accepted"),
        pytest.param("running", "running", id="running"),
        pytest.param("finished", "recording_pending", id="recording-pending"),
        pytest.param("finished", "recording_failed", id="recording-failed"),
    ],
)
def test_every_coordinator_lifecycle_state_has_a_valid_wire_form(
    phase: RunPhase | None, coordinator_state: CoordinatorState
) -> None:
    if phase is None:
        wire = outcome_snapshot_wire(active_outcome=None)
        wire["coordinator_state"] = "idle"
        wire["active_run"] = None
        wire["step"] = None
        wire["recording_state"] = None
        wire["unrecorded_terminal_projection"] = None
        assert snapshot_payload(snapshot_from_payload(wire)) == wire
        return
    outcome = "completed" if phase == "finished" else None
    wire = outcome_snapshot_wire(
        active_outcome=outcome,
        active_phase=phase,
        coordinator_state=coordinator_state,
        projection_outcome=(
            outcome
            if coordinator_state in ("recording_pending", "recording_failed")
            else NO_PROJECTION
        ),
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
    wire = outcome_snapshot_wire(active_outcome=None)
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
    wire = outcome_snapshot_wire(active_outcome=None)
    if field == "phase":
        active = wire["active_run"]
        assert isinstance(active, dict)
        del active["phase"]
    else:
        del wire["coordinator_state"]

    expected_object = "active_run" if field == "phase" else "top"
    with pytest.raises(ValueError, match=expected_object):
        snapshot_from_payload(wire)


@pytest.mark.parametrize(
    ("target", "outcome", "error"),
    [
        pytest.param("active", 1, "run outcome", id="active-non-string"),
        pytest.param("active", "unknown", "run outcome", id="active-unknown"),
        pytest.param("active", None, "run outcome", id="active-terminal-null"),
        pytest.param("active", ..., "active_run", id="active-terminal-missing"),
        pytest.param("projection", 1, "run outcome", id="projection-non-string"),
        pytest.param(
            "projection", "unknown", "run outcome", id="projection-unknown"
        ),
        pytest.param("projection", None, "run outcome", id="projection-null"),
        pytest.param(
            "projection",
            ...,
            "unrecorded_terminal_projection",
            id="projection-missing",
        ),
    ],
)
def test_wire_rejects_invalid_run_outcomes(
    target: str, outcome: object, error: str
) -> None:
    kwargs: dict[str, object] = {"active_outcome": None}
    if target == "active":
        kwargs.update(active_outcome=outcome, active_phase="finished")
    else:
        kwargs["projection_outcome"] = outcome
    wire = outcome_snapshot_wire(**kwargs)

    with pytest.raises(ValueError, match=error):
        snapshot_from_payload(wire)




def _active_run_summary(
    *, phase: RunPhase, outcome: RunOutcome | None
) -> ActiveRunSummary:
    return ActiveRunSummary(
        run_id="run-1",
        purpose="chat",
        gateway="web",
        phase=phase,
        outcome=outcome,
        session_id="session-1",
        prompt_preview="hello",
        started_at=None,
        recording_state=None,
    )
