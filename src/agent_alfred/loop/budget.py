"""RunBudget: one shared Step allotment for the whole Run (ADR-0012)."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class StepBudgetExceeded(Exception):
    """Raised when reserve_step is called with no remaining Steps.

    No network request has been sent. The caller must not pretend the
    model ran.
    """


@dataclass(frozen=True)
class StepLease:
    """One-shot credential for a model response. Never returned."""

    step_index: int
    node_id: str


class RunBudget:
    """A single remaining-Step counter shared by every consumer path."""

    def __init__(self, max_steps: int):
        if max_steps < 0:
            raise ValueError("max_steps must be >= 0")
        self._remaining = max_steps
        self._used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def used(self) -> int:
        return self._used

    def reserve_step(self, node_id: str) -> StepLease:
        with self._lock:
            if self._remaining <= 0:
                raise StepBudgetExceeded(
                    f"RunBudget exhausted; {self._used} step(s) already reserved"
                )
            lease = StepLease(step_index=self._used, node_id=node_id)
            self._remaining -= 1
            self._used += 1
            return lease
