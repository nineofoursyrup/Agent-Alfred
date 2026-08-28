"""Closed Run outcome set. Single source for the loop, events, and schema."""

from __future__ import annotations

from typing import Literal, get_args

RunOutcome = Literal["completed", "max_steps", "failed", "interrupted"]
RUN_OUTCOMES: tuple[RunOutcome, ...] = get_args(RunOutcome)
