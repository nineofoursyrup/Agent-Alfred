"""Closed Run phase set shared by storage and runtime projections."""

from __future__ import annotations

from typing import Literal, get_args

from agent_alfred.outcomes import RunOutcome, parse_run_outcome

RunPhase = Literal["accepted", "running", "finished"]
RUN_PHASES: tuple[RunPhase, ...] = get_args(RunPhase)
IN_FLIGHT_RUN_PHASES: tuple[RunPhase, RunPhase] = ("accepted", "running")
TERMINAL_RUN_PHASE: RunPhase = "finished"


def parse_run_phase(value: object) -> RunPhase:
    """Validate and narrow a value crossing a Run lifecycle boundary."""
    if not isinstance(value, str):
        raise ValueError(f"invalid run phase: {value!r}")
    if value == "accepted" or value == "running" or value == "finished":
        return value
    raise ValueError(f"invalid run phase: {value!r}")


def parse_run_lifecycle_pair(
    phase: object, outcome: object
) -> tuple[RunPhase, RunOutcome | None]:
    """Validate and narrow the two axes of a Run lifecycle together."""
    parsed_phase = parse_run_phase(phase)
    parsed_outcome = parse_run_outcome(outcome, allow_none=True)
    if parsed_phase == TERMINAL_RUN_PHASE:
        if parsed_outcome is None:
            raise ValueError(
                "invalid run lifecycle: finished phase requires a run outcome"
            )
    elif parsed_outcome is not None:
        raise ValueError(
            "invalid run lifecycle: accepted/running phase requires a null "
            "run outcome"
        )
    return parsed_phase, parsed_outcome
