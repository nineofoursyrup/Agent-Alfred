"""Lock RunBudget: shared, never reset, lease never returned."""

from __future__ import annotations

import pytest

from agent_alfred.loop.budget import RunBudget, StepBudgetExceeded


def test_reserve_step_consumes_one_step_whether_or_not_the_caller_succeeds() -> None:
    budget = RunBudget(2)
    first = budget.reserve_step("loop")
    second = budget.reserve_step("graph_node")
    assert (first.step_index, second.step_index) == (0, 1)
    assert budget.remaining == 0
    with pytest.raises(StepBudgetExceeded):
        budget.reserve_step("loop")
    assert budget.used == 2


def test_exhausted_budget_does_not_reset() -> None:
    budget = RunBudget(0)
    with pytest.raises(StepBudgetExceeded):
        budget.reserve_step("loop")
    with pytest.raises(StepBudgetExceeded):
        budget.reserve_step("loop")
    assert budget.used == 0
