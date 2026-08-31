"""Run outcome is one closed set, imported everywhere."""

from __future__ import annotations

from typing import get_type_hints

import pytest

from agent_alfred.events import RunOutcome as EventOutcome
from agent_alfred.gateway.web.state import (
    ActiveRunView,
    UnrecordedTerminalView,
    snapshot_from_payload,
    snapshot_payload,
)
from agent_alfred.loop.assistant import RunOutcome as LoopOutcome
from agent_alfred.outcomes import RUN_OUTCOMES, RunOutcome
from agent_alfred.runtime.recording import RunRecorder
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    UnrecordedTerminalProjection,
)
from agent_alfred.schema import OUTCOMES

_NO_PROJECTION = object()


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


@pytest.mark.parametrize("outcome", RUN_OUTCOMES)
def test_every_run_outcome_round_trips_through_the_wire(outcome: RunOutcome) -> None:
    wire = _wire_payload(
        active_outcome=outcome,
        projection_outcome=outcome,
        active_phase="finished",
    )

    assert snapshot_payload(snapshot_from_payload(wire)) == wire


def test_an_active_nonterminal_run_may_have_no_outcome() -> None:
    wire = _wire_payload(active_outcome=None)

    rebuilt = snapshot_from_payload(wire)

    assert rebuilt.active_run is not None
    assert rebuilt.active_run.outcome is None


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
    active_phase: str = "running",
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
        "coordinator_state": "running",
        "active_run": active,
        "step": None,
        "recording_state": None,
        "session_valid": True,
        "unrecorded_terminal_projection": projection,
    }
