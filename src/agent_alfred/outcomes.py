"""Closed Run outcome set. Single source for the loop, events, and schema."""

from __future__ import annotations

from typing import Literal, cast, get_args, overload

RunOutcome = Literal["completed", "max_steps", "failed", "interrupted"]
RUN_OUTCOMES: tuple[RunOutcome, ...] = get_args(RunOutcome)


@overload
def parse_run_outcome(
    value: object, *, allow_none: Literal[False] = False
) -> RunOutcome: ...


@overload
def parse_run_outcome(
    value: object, *, allow_none: Literal[True]
) -> RunOutcome | None: ...


@overload
def parse_run_outcome(value: object, *, allow_none: bool) -> RunOutcome | None: ...


def parse_run_outcome(
    value: object, *, allow_none: bool = False
) -> RunOutcome | None:
    """Validate a value crossing a wire boundary as a Run outcome."""
    if value is None:
        if allow_none:
            return None
        raise ValueError("run outcome is required")
    if not isinstance(value, str) or value not in RUN_OUTCOMES:
        raise ValueError(f"invalid run outcome: {value!r}")
    return cast(RunOutcome, value)
