"""Closed recording-state sets owned by the runtime domain."""

from __future__ import annotations

from typing import Literal, overload

RecordingState = Literal["pending", "recorded", "failed"]
UnrecordedTerminalState = Literal["pending", "failed"]


@overload
def parse_recording_state(
    value: object,
    *,
    allow_none: Literal[False] = False,
    unrecorded_terminal: Literal[False] = False,
) -> RecordingState: ...


@overload
def parse_recording_state(
    value: object,
    *,
    allow_none: Literal[True],
    unrecorded_terminal: Literal[False] = False,
) -> RecordingState | None: ...


@overload
def parse_recording_state(
    value: object,
    *,
    allow_none: Literal[False] = False,
    unrecorded_terminal: Literal[True],
) -> UnrecordedTerminalState: ...


@overload
def parse_recording_state(
    value: object,
    *,
    allow_none: Literal[True],
    unrecorded_terminal: Literal[True],
) -> UnrecordedTerminalState | None: ...


def parse_recording_state(
    value: object,
    *,
    allow_none: bool = False,
    unrecorded_terminal: bool = False,
) -> RecordingState | None:
    """Validate and narrow a recording state crossing a runtime boundary."""
    if value is None:
        if allow_none:
            return None
        qualifier = "unrecorded terminal " if unrecorded_terminal else ""
        raise ValueError(f"{qualifier}recording state is required")
    if not isinstance(value, str):
        qualifier = "unrecorded terminal " if unrecorded_terminal else ""
        raise ValueError(f"invalid {qualifier}recording state: {value!r}")
    if value == "pending" or value == "failed":
        return value
    if value == "recorded" and not unrecorded_terminal:
        return value
    qualifier = "unrecorded terminal " if unrecorded_terminal else ""
    raise ValueError(f"invalid {qualifier}recording state: {value!r}")
