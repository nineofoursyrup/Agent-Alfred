"""The atomic run-state snapshot and the rules for merging a patch into it.

Two halves live here because they are one contract seen from two ends:

- the server builds a **bounded** snapshot: process identity, state revision,
  the active Run's lifecycle projection, the current Step/Attempt terminals,
  the recording state, whether *this* connection's Session is still valid,
  and at most one unrecorded terminal projection. Historic Sessions,
  messages and finished Runs are not in it -- those are the paged read APIs.
- the client merges patches by a closed set of rules: refuse another
  process's patch, refuse a rewinding revision, refuse a revision it has
  already applied, and refuse a ``pending`` that would overwrite a settled
  ``recorded``/``failed``.

The merge rules are code here, not documentation on a wiki, precisely so
they can be tested without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_alfred.gateway.web.progress import AttemptTerminal, StepProjection
from agent_alfred.runtime.snapshot import RuntimeSnapshot

# The unrecorded reply is the one thing in the snapshot that could be long.
# Bounding it keeps the snapshot bounded, and the cut is marked so nobody
# reads a truncated sentence as the whole sentence.
SNAPSHOT_TEXT_LIMIT = 2000

RecordingState = Literal["pending", "recorded", "failed"] | None
PatchRejection = Literal[
    "instance_mismatch",
    "revision_duplicate",
    "revision_regression",
    "pending_over_terminal",
]


class StatePatchRejected(ValueError):
    """The patch must not be applied. The client keeps what it has."""

    def __init__(self, reason: PatchRejection):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ActiveRunView:
    run_id: str
    purpose: str
    gateway: str
    phase: str
    outcome: str | None
    session_id: str | None
    prompt_preview: str | None
    started_at: str | None
    current_step: int | None
    recording_state: RecordingState


@dataclass(frozen=True)
class UnrecordedTerminalView:
    run_id: str
    purpose: str
    outcome: str
    reply_preview: str | None
    error: str | None
    recording_state: Literal["pending", "failed"]
    session_id: str | None
    prompt_preview: str | None


@dataclass(frozen=True)
class RunStateSnapshot:
    process_instance_id: str
    state_revision: int
    coordinator_state: str
    active_run: ActiveRunView | None
    step: StepProjection | None
    recording_state: RecordingState
    session_valid: bool
    unrecorded_terminal_projection: UnrecordedTerminalView | None


def _limit(text: str | None, limit: int = SNAPSHOT_TEXT_LIMIT) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def snapshot_payload(
    snapshot: RunStateSnapshot,
) -> dict[str, Any]:
    """The wire document. No histories, no messages, no finished Runs."""
    return {
        "process_instance_id": snapshot.process_instance_id,
        "state_revision": snapshot.state_revision,
        "coordinator_state": snapshot.coordinator_state,
        "active_run": (
            None
            if snapshot.active_run is None
            else {
                "run_id": snapshot.active_run.run_id,
                "purpose": snapshot.active_run.purpose,
                "gateway": snapshot.active_run.gateway,
                "phase": snapshot.active_run.phase,
                "outcome": snapshot.active_run.outcome,
                "session_id": snapshot.active_run.session_id,
                "prompt_preview": snapshot.active_run.prompt_preview,
                "started_at": snapshot.active_run.started_at,
                "current_step": snapshot.active_run.current_step,
                "recording_state": snapshot.active_run.recording_state,
            }
        ),
        "step": (
            None
            if snapshot.step is None
            else {
                "step_index": snapshot.step.step_index,
                "attempts": [
                    {
                        "attempt_id": attempt.attempt_id,
                        "outcome": attempt.outcome,
                        "stop_reason": attempt.stop_reason,
                        "error_code": attempt.error_code,
                        "duration_ms": attempt.duration_ms,
                    }
                    for attempt in snapshot.step.attempts
                ],
                "attempts_truncated": snapshot.step.attempts_truncated,
            }
        ),
        "recording_state": snapshot.recording_state,
        "session_valid": snapshot.session_valid,
        "unrecorded_terminal_projection": (
            None
            if snapshot.unrecorded_terminal_projection is None
            else {
                "run_id": snapshot.unrecorded_terminal_projection.run_id,
                "purpose": snapshot.unrecorded_terminal_projection.purpose,
                "outcome": snapshot.unrecorded_terminal_projection.outcome,
                "reply_preview": snapshot.unrecorded_terminal_projection.reply_preview,
                "error": snapshot.unrecorded_terminal_projection.error,
                "recording_state": (
                    snapshot.unrecorded_terminal_projection.recording_state
                ),
                "session_id": snapshot.unrecorded_terminal_projection.session_id,
                "prompt_preview": (
                    snapshot.unrecorded_terminal_projection.prompt_preview
                ),
            }
        ),
    }


def snapshot_from_payload(payload: dict[str, Any]) -> RunStateSnapshot:
    """Rebuild the typed snapshot from a wire document.

    The client is our own code, so it reads the typed form; the round trip
    exists so the merge rules are exercised against what actually crossed the
    wire rather than against a convenient in-memory object.
    """
    step = payload.get("step")
    projection = payload.get("unrecorded_terminal_projection")
    active = payload.get("active_run")
    return RunStateSnapshot(
        process_instance_id=payload["process_instance_id"],
        state_revision=payload["state_revision"],
        coordinator_state=payload["coordinator_state"],
        active_run=(
            None
            if active is None
            else ActiveRunView(
                run_id=active["run_id"],
                purpose=active["purpose"],
                gateway=active["gateway"],
                phase=active["phase"],
                outcome=active.get("outcome"),
                session_id=active.get("session_id"),
                prompt_preview=active.get("prompt_preview"),
                started_at=active.get("started_at"),
                current_step=active.get("current_step"),
                recording_state=active.get("recording_state"),
            )
        ),
        step=(
            None
            if step is None
            else StepProjection(
                step_index=step["step_index"],
                attempts=tuple(
                    AttemptTerminal(**attempt) for attempt in step["attempts"]
                ),
                attempts_truncated=bool(step.get("attempts_truncated", False)),
            )
        ),
        recording_state=payload.get("recording_state"),
        session_valid=bool(payload.get("session_valid", False)),
        unrecorded_terminal_projection=(
            None
            if projection is None
            else UnrecordedTerminalView(
                run_id=projection["run_id"],
                purpose=projection["purpose"],
                outcome=projection["outcome"],
                reply_preview=projection.get("reply_preview"),
                error=projection.get("error"),
                recording_state=projection["recording_state"],
                session_id=projection.get("session_id"),
                prompt_preview=projection.get("prompt_preview"),
            )
        ),
    )


def build_snapshot(
    snapshot: RuntimeSnapshot,
    *,
    step: StepProjection | None,
    session_valid: bool,
) -> RunStateSnapshot:
    """Project the authoritative in-process snapshot onto the wire shape.

    The unrecorded reply is bounded here and here only: the projection the
    Host holds is the full text, and the snapshot is the one place that text
    would otherwise escape into something sent to a browser.
    """
    active = snapshot.active_run
    projection = snapshot.unrecorded_terminal_projection
    recording_state = None
    if active is not None and active.recording_state is not None:
        recording_state = active.recording_state
    elif projection is not None:
        recording_state = projection.recording_state
    return RunStateSnapshot(
        process_instance_id=snapshot.process_instance_id,
        state_revision=snapshot.state_revision,
        coordinator_state=snapshot.coordinator_state,
        active_run=(
            None
            if active is None
            else ActiveRunView(
                run_id=active.run_id,
                purpose=active.purpose,
                gateway=active.gateway,
                phase=active.phase,
                outcome=active.outcome,
                session_id=active.session_id,
                prompt_preview=active.prompt_preview,
                started_at=active.started_at,
                current_step=active.current_step,
                recording_state=active.recording_state,
            )
        ),
        step=step,
        recording_state=recording_state,
        session_valid=session_valid,
        unrecorded_terminal_projection=(
            None
            if projection is None
            else UnrecordedTerminalView(
                run_id=projection.run_id,
                purpose=projection.purpose,
                outcome=projection.outcome,
                reply_preview=_limit(projection.reply_text),
                error=projection.error,
                recording_state=projection.recording_state,
                session_id=projection.session_id,
                prompt_preview=projection.prompt_preview,
            )
        ),
    )


def _is_settled(state: RunStateSnapshot) -> bool:
    projection = state.unrecorded_terminal_projection
    if projection is not None and projection.recording_state == "failed":
        return True
    active = state.active_run
    return active is not None and active.recording_state in ("recorded", "failed")


def _is_pending(patch: RunStateSnapshot) -> bool:
    projection = patch.unrecorded_terminal_projection
    if projection is not None and projection.recording_state == "pending":
        return True
    active = patch.active_run
    return active is not None and active.recording_state == "pending"


def apply_state_patch(
    current: RunStateSnapshot | None, patch: RunStateSnapshot
) -> RunStateSnapshot:
    """Merge one patch, or say why it cannot be merged.

    A rejected patch is not applied at all: the client keeps the state it
    has and waits for the snapshot a reconnect will bring. Half-applying a
    patch would produce a state nobody ever published.
    """
    if current is None:
        return patch
    if patch.process_instance_id != current.process_instance_id:
        raise StatePatchRejected("instance_mismatch")
    if patch.state_revision < current.state_revision:
        raise StatePatchRejected("revision_regression")
    if patch.state_revision == current.state_revision:
        # The same revision is the state the client already holds. The
        # server publishes each revision exactly once, so a repeat is
        # either a replay of something already folded in or something that
        # never came from this process's snapshot sequence -- and the
        # payload cannot tell the two apart. Refusing both shapes is what
        # keeps a revision naming exactly one published state; the client
        # keeps what it has.
        raise StatePatchRejected("revision_duplicate")
    # "Reply finished but unsaved" is a fact the client must keep showing
    # until the database says otherwise. A pending patch that would replace
    # a settled state is therefore refused, not applied and then corrected.
    if _is_pending(patch) and _is_settled(current):
        raise StatePatchRejected("pending_over_terminal")
    return patch
