"""In-process run-state snapshot. Transport-agnostic (ADR-0025)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from typing import Literal

from agent_alfred.outcomes import RunOutcome
from agent_alfred.runtime.recording_state import (
    RecordingState,
    UnrecordedTerminalState,
    parse_recording_state,
)

CoordinatorState = Literal[
    "idle", "accepted", "running", "recording_pending", "recording_failed"
]

_UNSET = object()


@dataclass(frozen=True)
class ActiveRunSummary:
    run_id: str
    purpose: str
    gateway: str
    phase: str
    session_id: str | None
    prompt_preview: str | None
    started_at: str | None
    recording_state: RecordingState | None
    current_step: int | None = None
    # The active Run's business conclusion, once it has one. Null while the
    # Run is not terminal, exactly like the phase/outcome axes require.
    outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        parse_recording_state(self.recording_state, allow_none=True)


@dataclass(frozen=True)
class UnrecordedTerminalProjection:
    run_id: str
    purpose: str
    outcome: RunOutcome
    reply_text: str | None
    error: str | None
    recording_state: UnrecordedTerminalState
    session_id: str | None
    prompt_preview: str | None

    def __post_init__(self) -> None:
        parse_recording_state(self.recording_state, unrecorded_terminal=True)


@dataclass(frozen=True)
class RuntimeSnapshot:
    process_instance_id: str
    state_revision: int
    coordinator_state: CoordinatorState
    active_run: ActiveRunSummary | None
    unrecorded_terminal_projection: UnrecordedTerminalProjection | None


class RunStateStore:
    """Authoritative in-process snapshot. Seq and activity_revision never live here.

    The optional ``listener`` is how a transport learns that the state moved.
    It is stored here rather than called by each of the Host's transitions
    because every transition goes through :meth:`replace`: one call site is
    one place to forget, and forgetting one would leave a client showing a
    lifetime state nothing will ever correct.
    """

    def __init__(
        self,
        process_instance_id: str,
        *,
        listener: Callable[[RuntimeSnapshot], None] | None = None,
    ):
        self._lock = threading.Lock()
        self._listener = listener
        self._snapshot = RuntimeSnapshot(
            process_instance_id=process_instance_id,
            state_revision=0,
            coordinator_state="idle",
            active_run=None,
            unrecorded_terminal_projection=None,
        )

    def get(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def replace(
        self,
        *,
        coordinator_state: CoordinatorState | None = None,
        active_run: object = _UNSET,
        unrecorded_terminal_projection: object = _UNSET,
    ) -> RuntimeSnapshot:
        with self._lock:
            current = self._snapshot
            kwargs: dict = {"state_revision": current.state_revision + 1}
            if coordinator_state is not None:
                kwargs["coordinator_state"] = coordinator_state
            if active_run is not _UNSET:
                kwargs["active_run"] = active_run
            if unrecorded_terminal_projection is not _UNSET:
                kwargs["unrecorded_terminal_projection"] = (
                    unrecorded_terminal_projection
                )
            self._snapshot = _dc_replace(current, **kwargs)
            snapshot = self._snapshot
            if self._listener is not None:
                # Called after the authoritative snapshot has moved and while
                # still holding the lock. The order is the contract (#30):
                # a patch may never describe a state that is not yet the
                # state. Holding the lock is what makes two transitions
                # deliver their patches in the order they happened.
                #
                # The listener must therefore be bounded and do no IO -- it
                # enqueues; it does not write to a socket.
                self._listener(snapshot)
            return self._snapshot
