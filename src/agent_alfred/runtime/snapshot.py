"""In-process run-state snapshot. Transport-agnostic (ADR-0025)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from typing import Literal

from agent_alfred.outcomes import RunOutcome
from agent_alfred.run_phases import (
    RunPhase,
    parse_run_lifecycle_pair,
)
from agent_alfred.run_phases import (
    parse_run_phase as parse_run_phase,
)
from agent_alfred.runtime.recording_state import (
    RecordingState,
    UnrecordedTerminalState,
    parse_recording_state,
)

CoordinatorState = Literal[
    "idle", "accepted", "running", "recording_pending", "recording_failed"
]


def parse_coordinator_state(value: object) -> CoordinatorState:
    """Validate and narrow a coordinator state crossing a runtime boundary."""
    if not isinstance(value, str):
        raise ValueError(f"invalid coordinator state: {value!r}")
    if value == "idle" or value == "accepted" or value == "running":
        return value
    if value == "recording_pending" or value == "recording_failed":
        return value
    raise ValueError(f"invalid coordinator state: {value!r}")

_UNSET = object()


@dataclass(frozen=True)
class ActiveRunSummary:
    run_id: str
    purpose: str
    gateway: str
    phase: RunPhase
    session_id: str | None
    prompt_preview: str | None
    started_at: str | None
    recording_state: RecordingState | None
    current_step: int | None = None
    # The active Run's business conclusion, once it has one. Null while the
    # Run is not terminal, exactly like the phase/outcome axes require.
    outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        phase, outcome = parse_run_lifecycle_pair(self.phase, self.outcome)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "outcome", outcome)
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
    # A safe display marker is not complete reply content (ADR-0029).
    reply_withheld: bool = False

    def __post_init__(self) -> None:
        parse_recording_state(self.recording_state, unrecorded_terminal=True)


@dataclass(frozen=True)
class RuntimeSnapshot:
    process_instance_id: str
    state_revision: int
    coordinator_state: CoordinatorState
    active_run: ActiveRunSummary | None
    unrecorded_terminal_projection: UnrecordedTerminalProjection | None

    def __post_init__(self) -> None:
        parse_coordinator_state(self.coordinator_state)


@dataclass(frozen=True)
class _PendingDelivery:
    """One committed snapshot whose listener return is still uncertain."""

    owner_run_id: str
    snapshot: RuntimeSnapshot


@dataclass(frozen=True)
class _StoreState:
    """Atomically published snapshot plus its listener-delivery debt."""

    snapshot: RuntimeSnapshot
    pending_delivery: _PendingDelivery | None


def is_unaddressable_unstarted_handoff_failure(
    snapshot: RuntimeSnapshot,
) -> bool:
    """Whether recovery authority names a Run that never reached execution.

    The accepted database row and the in-process projection must survive so
    restart recovery and admission remain fail-closed.  This exact shape is
    nevertheless not a Run a browser can navigate to: handoff failed before
    execution and even the interrupted finalize could not commit.
    """
    active = snapshot.active_run
    projection = snapshot.unrecorded_terminal_projection
    return bool(
        snapshot.coordinator_state == "recording_failed"
        and active is not None
        and active.phase == "finished"
        and active.outcome == "interrupted"
        and active.started_at is None
        and active.recording_state == "failed"
        and projection is not None
        and projection.run_id == active.run_id
        and projection.session_id == active.session_id
        and projection.outcome == "interrupted"
        and projection.recording_state == "failed"
        and projection.reply_text is None
        and projection.error == "handoff_failed"
    )


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
        self._state = _StoreState(
            snapshot=RuntimeSnapshot(
                process_instance_id=process_instance_id,
                state_revision=0,
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            ),
            pending_delivery=None,
        )

    def get(self) -> RuntimeSnapshot:
        with self._lock:
            return self._state.snapshot

    def replace(
        self,
        *,
        owner_run_id: str,
        coordinator_state: CoordinatorState | None = None,
        active_run: object = _UNSET,
        unrecorded_terminal_projection: object = _UNSET,
    ) -> RuntimeSnapshot:
        with self._lock:
            current = self._state.snapshot
            kwargs: dict = {"state_revision": current.state_revision + 1}
            if coordinator_state is not None:
                kwargs["coordinator_state"] = coordinator_state
            if active_run is not _UNSET:
                kwargs["active_run"] = active_run
            if unrecorded_terminal_projection is not _UNSET:
                kwargs["unrecorded_terminal_projection"] = (
                    unrecorded_terminal_projection
                )
            snapshot = _dc_replace(current, **kwargs)
            if self._listener is not None:
                # Called after the authoritative snapshot has moved and while
                # still holding the lock. The order is the contract (#30):
                # a patch may never describe a state that is not yet the
                # state. Holding the lock is what makes two transitions
                # deliver their patches in the order they happened.
                #
                # The listener must therefore be bounded and do no IO -- it
                # enqueues; it does not write to a socket.
                delivery = _PendingDelivery(owner_run_id, snapshot)
                # A newer absolute snapshot supersedes an older undelivered
                # one.  Keeping only this token prevents a later recovery
                # from publishing an obsolete state after its replacement.
                published = _StoreState(snapshot, delivery)
                self._state = published
                self._listener(snapshot)
                if self._state is published:
                    self._state = _StoreState(snapshot, None)
            else:
                self._state = _StoreState(snapshot, None)
            return self._state.snapshot

    def resume_pending(self, owner_run_id: str) -> bool:
        """Retry this Run's current listener delivery without a new revision.

        ``replace`` commits the immutable snapshot before invoking its
        listener.  If a ``BaseException`` loses the listener's return edge,
        the token remains here.  Recovery may replay only the same owner's
        still-current absolute snapshot; a successor's replacement
        supersedes the token and makes an old retry a no-op.
        """
        with self._lock:
            current = self._state
            delivery = current.pending_delivery
            if (
                delivery is None
                or delivery.owner_run_id != owner_run_id
                or delivery.snapshot is not current.snapshot
            ):
                return False
            if self._listener is not None:
                self._listener(delivery.snapshot)
            if self._state is current:
                self._state = _StoreState(current.snapshot, None)
            return True
