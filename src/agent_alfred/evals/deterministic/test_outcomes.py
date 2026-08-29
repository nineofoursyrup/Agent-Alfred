"""Run outcome is one closed set, imported everywhere."""

from __future__ import annotations

from agent_alfred.events import RunOutcome as EventOutcome
from agent_alfred.loop.assistant import RunOutcome as LoopOutcome
from agent_alfred.outcomes import RUN_OUTCOMES, RunOutcome
from agent_alfred.schema import OUTCOMES


def test_run_outcome_is_a_single_closed_set() -> None:
    assert LoopOutcome is RunOutcome
    assert EventOutcome is RunOutcome
    assert OUTCOMES == RUN_OUTCOMES
    assert RUN_OUTCOMES == ("completed", "max_steps", "failed", "interrupted")
