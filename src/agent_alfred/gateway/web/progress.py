"""The terminal Attempt summary for the Host-owned current Step.

The atomic snapshot has to stay bounded (#30), so this is a *summary*: which
Step we are on and what became of each Attempt inside it. It carries no
streaming deltas -- those only ever travel as domain events, never copied
into a patch.

The Step index itself comes from ``RuntimeSnapshot.active_run.current_step``.
This module only retains bounded Attempt terminals and combines them with
that authoritative index when the broker renders a snapshot.

It is deliberately not thread-safe. The broker drives it inside the same
critical section that decides the connection's snapshot, so the progress a
snapshot reports is the progress at the moment that snapshot was taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# A Step can hold a retry and a streaming fallback, both of which are real
# billed round-trips; eight leaves room for a pathological Step without
# letting one unbounded retry chain inflate every snapshot.
DEFAULT_MAX_ATTEMPTS = 8

_COMMITTED = "committed"
_ABORTED = "aborted"


@dataclass(frozen=True)
class AttemptTerminal:
    """What became of one Attempt. A terminal fact, never a partial."""

    attempt_id: str
    outcome: str
    stop_reason: str | None
    error_code: str | None
    duration_ms: int


@dataclass(frozen=True)
class StepProjection:
    step_index: int
    attempts: tuple[AttemptTerminal, ...]
    # True once the oldest Attempts were dropped to stay bounded. Stated,
    # not hidden: a client that shows "3 attempts" when there were 5 is
    # showing something that never happened.
    attempts_truncated: bool = False


class RunProgress:
    """Tracks bounded Attempt terminals for the authoritative current Step."""

    def __init__(self, max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = max_attempts
        self._run_id: str | None = None
        self._attempts: list[AttemptTerminal] = []
        self._truncated = False

    def note_run_started(self, run_id: str) -> None:
        self._run_id = run_id
        self._attempts = []
        self._truncated = False

    def note_run_finished(self, run_id: str) -> None:
        """Freeze the projection instead of erasing it.

        ``run.finished`` is published before the recording transaction
        settles: the admission lease is still held (ADR-0026), the Run is
        still the active one, and this view is the only in-process record of
        what the Run produced. A same-process reconnect in that window reads
        exactly it, so the last Step/Attempt summary must survive here. The
        summary stops being shown -- and is cleared -- by
        :meth:`note_active_run` when the authoritative snapshot moves past
        the Run, or wholesale by the next ``run.started``; never by the
        event that merely ends the model loop.
        """
        del run_id

    def note_active_run(self, run_id: str | None) -> None:
        """Bind the view to the authoritative active Run, or unbind it.

        The frozen terminal summary belongs to exactly one Run. It may be
        shown only while that Run is still the authoritative active one --
        through ``recording_pending`` and the recorded snapshot -- and must
        stop the moment the snapshot moves past it: the lease released
        (recorded→idle) or a new Run admitted. Otherwise the old Run's
        progress would masquerade as the new state's progress.
        """
        if self._run_id is None or run_id == self._run_id:
            return
        self._run_id = None
        self._attempts = []
        self._truncated = False

    def note_step_started(self, run_id: str) -> None:
        if not self._tracking(run_id):
            return
        self._attempts = []
        self._truncated = False

    def note_attempt_terminal(
        self,
        run_id: str,
        *,
        attempt_id: str,
        outcome: str,
        stop_reason: str | None,
        error_code: str | None,
        duration_ms: int,
    ) -> None:
        if not self._tracking(run_id):
            return
        self._attempts.append(
            AttemptTerminal(
                attempt_id=attempt_id,
                outcome=outcome,
                stop_reason=stop_reason,
                error_code=error_code,
                duration_ms=duration_ms,
            )
        )
        while len(self._attempts) > self._max_attempts:
            self._attempts.pop(0)
            self._truncated = True

    def _tracking(self, run_id: str) -> bool:
        return self._run_id is not None and self._run_id == run_id

    def projection(self, step_index: int | None) -> StepProjection | None:
        """Combine Attempt terminals with the Host-owned Step fact."""
        if self._run_id is None or step_index is None:
            return None
        return StepProjection(
            step_index=step_index,
            attempts=tuple(self._attempts),
            attempts_truncated=self._truncated,
        )


def observe(payload: Any, run_id: str, progress: RunProgress) -> None:
    """Feed one domain payload to the progress view, by its published name.

    Kept next to the view so the event vocabulary it depends on lives in one
    place: adding an event does not require hunting for a second switch.
    """
    name = getattr(payload, "name", None)
    if name == "run.started":
        progress.note_run_started(run_id)
    elif name == "step.started":
        progress.note_step_started(run_id)
    elif name == "attempt.committed":
        progress.note_attempt_terminal(
            run_id,
            attempt_id=getattr(payload, "attempt_id", ""),
            outcome=_COMMITTED,
            stop_reason=getattr(payload, "stop_reason", None),
            error_code=None,
            duration_ms=int(getattr(payload, "duration_ms", 0)),
        )
    elif name == "attempt.aborted":
        progress.note_attempt_terminal(
            run_id,
            attempt_id=getattr(payload, "attempt_id", ""),
            outcome=_ABORTED,
            stop_reason=None,
            error_code=_error_code(getattr(payload, "error", None)),
            duration_ms=int(getattr(payload, "duration_ms", 0)),
        )
    elif name == "run.finished":
        progress.note_run_finished(run_id)


def _error_code(error: Any) -> str | None:
    if error is None:
        return None
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__
