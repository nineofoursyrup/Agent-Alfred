"""Closed Run phase set shared by storage and runtime projections."""

from __future__ import annotations

from typing import Literal, get_args

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
