"""In-process run-state snapshot. Transport-agnostic (ADR-0025)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from typing import Literal

RecordingState = Literal["pending", "recorded", "failed"]
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


@dataclass(frozen=True)
class UnrecordedTerminalProjection:
    run_id: str
    purpose: str
    outcome: str
    reply_text: str | None
    error: str | None
    recording_state: Literal["pending", "failed"]
    session_id: str | None
    prompt_preview: str | None


@dataclass(frozen=True)
class RuntimeSnapshot:
    process_instance_id: str
    state_revision: int
    coordinator_state: CoordinatorState
    active_run: ActiveRunSummary | None
    unrecorded_terminal_projection: UnrecordedTerminalProjection | None


class RunStateStore:
    """Authoritative in-process snapshot. Seq and activity_revision never live here."""

    def __init__(self, process_instance_id: str):
        self._lock = threading.Lock()
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
            return self._snapshot
