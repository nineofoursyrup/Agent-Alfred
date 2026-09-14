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
from agent_alfred.model import parse_attempt_outcome, parse_stop_reason
from agent_alfred.outcomes import RunOutcome, parse_run_outcome
from agent_alfred.run_phases import parse_run_lifecycle_pair
from agent_alfred.runtime.recording_state import (
    RecordingState,
    UnrecordedTerminalState,
    parse_recording_state,
)
from agent_alfred.runtime.snapshot import (
    CoordinatorState,
    RunPhase,
    RuntimeSnapshot,
    is_unaddressable_unstarted_handoff_failure,
    parse_coordinator_state,
)

# The unrecorded reply is the one thing in the snapshot that could be long.
# Bounding it keeps the snapshot bounded, and the cut is marked so nobody
# reads a truncated sentence as the whole sentence.
SNAPSHOT_TEXT_LIMIT = 2000
_ATTEMPT_FIELDS = frozenset(
    {"attempt_id", "outcome", "stop_reason", "error_code", "duration_ms"}
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "process_instance_id",
        "state_revision",
        "coordinator_state",
        "active_run",
        "step",
        "recording_state",
        "session_valid",
        "unrecorded_terminal_projection",
    }
)
_ACTIVE_RUN_FIELDS = frozenset(
    {
        "run_id",
        "purpose",
        "gateway",
        "phase",
        "outcome",
        "session_id",
        "prompt_preview",
        "started_at",
        "current_step",
        "recording_state",
    }
)
_STEP_FIELDS = frozenset({"step_index", "attempts", "attempts_truncated"})
_TERMINAL_PROJECTION_FIELDS = frozenset(
    {
        "run_id",
        "purpose",
        "outcome",
        "reply_preview",
        "error",
        "recording_state",
        "session_id",
        "prompt_preview",
    }
)

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
    phase: RunPhase
    outcome: RunOutcome | None
    session_id: str | None
    prompt_preview: str | None
    started_at: str | None
    current_step: int | None
    recording_state: RecordingState | None


@dataclass(frozen=True)
class UnrecordedTerminalView:
    run_id: str
    purpose: str
    outcome: RunOutcome
    reply_preview: str | None
    error: str | None
    recording_state: UnrecordedTerminalState
    session_id: str | None
    prompt_preview: str | None

    reply_disposition: str | None = None
    aggregation: dict | None = None


@dataclass(frozen=True)
class RunStateSnapshot:
    process_instance_id: str
    state_revision: int
    coordinator_state: CoordinatorState
    active_run: ActiveRunView | None
    step: StepProjection | None
    recording_state: RecordingState | None
    session_valid: bool
    unrecorded_terminal_projection: UnrecordedTerminalView | None


def _limit(text: str | None, limit: int = SNAPSHOT_TEXT_LIMIT) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_required_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _parse_non_negative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _parse_required_nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _parse_optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{field} must be a string or null")
    return value


def _parse_exact_object(
    value: object, field: str, expected_fields: frozenset[str]
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected_fields:
        names = ", ".join(sorted(expected_fields))
        raise ValueError(f"{field} must be an object containing exactly {names}")
    return value


def _parse_attempt_terminal(value: object) -> AttemptTerminal:
    value = _parse_exact_object(value, "attempt", _ATTEMPT_FIELDS)
    stop_reason = value["stop_reason"]
    return AttemptTerminal(
        attempt_id=_parse_required_nonempty_string(value["attempt_id"], "attempt_id"),
        outcome=parse_attempt_outcome(value["outcome"]),
        stop_reason=(
            None if stop_reason is None else parse_stop_reason(stop_reason)
        ),
        error_code=_parse_optional_string(value["error_code"], "error_code"),
        duration_ms=_parse_non_negative_int(value["duration_ms"], "duration_ms"),
    )


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
                **(
                    {
                        "reply_disposition": (
                            snapshot.unrecorded_terminal_projection.reply_disposition
                        )
                    }
                    if snapshot.unrecorded_terminal_projection.reply_disposition
                    else {}
                ),
                **({"aggregation": snapshot.unrecorded_terminal_projection.aggregation}
                   if snapshot.unrecorded_terminal_projection.aggregation else {}),
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


def snapshot_from_payload(payload: object) -> RunStateSnapshot:
    """Rebuild the typed snapshot from a wire document.

    The client is our own code, so it reads the typed form; the round trip
    exists so the merge rules are exercised against what actually crossed the
    wire rather than against a convenient in-memory object.
    """
    payload = _parse_exact_object(payload, "top", _SNAPSHOT_FIELDS)
    step = payload["step"]
    projection = payload["unrecorded_terminal_projection"]
    active = payload["active_run"]
    active_run = None
    if active is not None:
        active = _parse_exact_object(active, "active_run", _ACTIVE_RUN_FIELDS)
        phase, outcome = parse_run_lifecycle_pair(
            active["phase"], active["outcome"]
        )
        active_run = ActiveRunView(
            run_id=_parse_required_nonempty_string(active["run_id"], "run_id"),
            purpose=_parse_required_nonempty_string(active["purpose"], "purpose"),
            gateway=_parse_required_nonempty_string(active["gateway"], "gateway"),
            phase=phase,
            outcome=outcome,
            session_id=_parse_optional_string(active["session_id"], "session_id"),
            prompt_preview=_parse_optional_string(
                active["prompt_preview"], "prompt_preview"
            ),
            started_at=_parse_optional_string(active["started_at"], "started_at"),
            current_step=(
                None
                if active["current_step"] is None
                else _parse_non_negative_int(active["current_step"], "current_step")
            ),
            recording_state=parse_recording_state(
                active["recording_state"], allow_none=True
            ),
        )
    if step is not None:
        step = _parse_exact_object(step, "step", _STEP_FIELDS)
        attempts = step["attempts"]
        if type(attempts) is not list:
            raise ValueError("attempts must be a list")
    if projection is not None:
        projection = _parse_exact_object(
            projection,
            "unrecorded_terminal_projection",
            _TERMINAL_PROJECTION_FIELDS
            | ({"reply_disposition"} if "reply_disposition" in projection else set())
            | ({"aggregation"} if "aggregation" in projection else set()),
        )
    if projection is not None and projection.get("reply_disposition") not in (
        None,
        "reply",
        "no_reply",
        "reply_withheld",
    ):
        raise ValueError("invalid reply_disposition")
    return _validate_snapshot_relations(
        RunStateSnapshot(
            process_instance_id=_parse_required_nonempty_string(
                payload["process_instance_id"], "process_instance_id"
            ),
            state_revision=_parse_non_negative_int(
                payload["state_revision"], "state_revision"
            ),
            coordinator_state=parse_coordinator_state(payload["coordinator_state"]),
            active_run=active_run,
            step=(
                None
                if step is None
                else StepProjection(
                    step_index=_parse_non_negative_int(
                        step["step_index"], "step_index"
                    ),
                    attempts=tuple(
                        _parse_attempt_terminal(attempt) for attempt in attempts
                    ),
                    attempts_truncated=_parse_required_bool(
                        step, "attempts_truncated"
                    ),
                )
            ),
            recording_state=parse_recording_state(
                payload["recording_state"], allow_none=True
            ),
            session_valid=_parse_required_bool(payload, "session_valid"),
            unrecorded_terminal_projection=(
                None
                if projection is None
                else UnrecordedTerminalView(
                    reply_disposition=projection.get("reply_disposition"),
                    aggregation=projection.get("aggregation"),
                    run_id=_parse_required_nonempty_string(
                        projection["run_id"], "run_id"
                    ),
                    purpose=_parse_required_nonempty_string(
                        projection["purpose"], "purpose"
                    ),
                    outcome=parse_run_outcome(projection["outcome"]),
                    reply_preview=_parse_optional_string(
                        projection["reply_preview"], "reply_preview"
                    ),
                    error=_parse_optional_string(projection["error"], "error"),
                    recording_state=parse_recording_state(
                        projection["recording_state"], unrecorded_terminal=True
                    ),
                    session_id=_parse_optional_string(
                        projection["session_id"], "session_id"
                    ),
                    prompt_preview=_parse_optional_string(
                        projection["prompt_preview"], "prompt_preview"
                    ),
                )
            ),
        )
    )


def _validate_snapshot_relations(snapshot: RunStateSnapshot) -> RunStateSnapshot:
    """Prove that all independently parsed fields describe one lifecycle."""
    state = snapshot.coordinator_state
    active = snapshot.active_run
    projection = snapshot.unrecorded_terminal_projection

    if state == "idle":
        if (
            active is not None
            or snapshot.step is not None
            or snapshot.recording_state is not None
            or projection is not None
        ):
            raise ValueError("idle snapshot must not contain run lifecycle state")
        return snapshot

    if active is None:
        if (
            state == "recording_failed"
            and snapshot.recording_state == "failed"
            and snapshot.step is None
            and projection is None
        ):
            return snapshot
        raise ValueError(f"{state} snapshot requires active_run")

    expected_phase = "accepted" if state == "accepted" else "running"
    if state == "recording_pending" or state == "recording_failed":
        expected_phase = "finished"
    if active.phase != expected_phase:
        raise ValueError("active_run phase contradicts coordinator_state")

    if state == "accepted" or state == "running":
        if state == "accepted" and active.started_at is not None:
            raise ValueError(
                "accepted coordinator_state requires started_at to be null"
            )
        if state == "running" and not active.started_at:
            raise ValueError(
                "running coordinator_state requires a non-empty started_at"
            )
        if active.recording_state is not None:
            raise ValueError("active_run recording_state contradicts coordinator_state")
        if projection is not None:
            raise ValueError(
                "unrecorded_terminal_projection contradicts coordinator_state"
            )
        if state == "accepted" and active.current_step is not None:
            raise ValueError("accepted coordinator_state cannot have a current step")
    elif state == "recording_pending":
        if active.started_at == "":
            raise ValueError(
                "recording_pending started_at must be non-empty or null"
            )
        if active.recording_state == "pending":
            if projection is None:
                raise ValueError(
                    "pending recording requires unrecorded_terminal_projection"
                )
        elif active.recording_state == "recorded":
            if projection is not None:
                raise ValueError(
                    "recorded transition cannot retain terminal projection"
                )
        else:
            raise ValueError("active_run recording_state contradicts coordinator_state")
    else:
        if active.recording_state != "failed":
            raise ValueError("active_run recording_state contradicts coordinator_state")
        if projection is None:
            raise ValueError(
                "recording_failed requires unrecorded_terminal_projection"
            )
        if active.started_at == "":
            raise ValueError(
                "recording_failed started_at must be non-empty or null"
            )

    if state in ("recording_pending", "recording_failed") and active.started_at is None:
        if active.outcome not in ("failed", "interrupted"):
            raise ValueError(
                "terminal active_run with null started_at requires a failed or "
                "interrupted outcome"
            )
        if active.current_step is not None or snapshot.step is not None:
            raise ValueError(
                "terminal active_run with null started_at cannot contain "
                "current_step or step"
            )

    if snapshot.recording_state != active.recording_state:
        raise ValueError("top-level recording_state disagrees with active_run")

    step = snapshot.step
    if (active.current_step is None) != (step is None) or (
        step is not None and step.step_index != active.current_step
    ):
        raise ValueError("step must match active_run.current_step")

    if projection is not None:
        for field in (
            "run_id",
            "purpose",
            "session_id",
            "prompt_preview",
            "outcome",
            "recording_state",
        ):
            if getattr(projection, field) != getattr(active, field):
                raise ValueError(
                    f"unrecorded_terminal_projection {field} disagrees with active_run"
                )
    return snapshot


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
    suppress_run = is_unaddressable_unstarted_handoff_failure(snapshot)
    active = None if suppress_run else snapshot.active_run
    projection = (
        None if suppress_run else snapshot.unrecorded_terminal_projection
    )
    recording_state = None
    if suppress_run:
        recording_state = "failed"
    elif active is not None and active.recording_state is not None:
        recording_state = active.recording_state
    elif projection is not None:
        recording_state = projection.recording_state
    return _validate_snapshot_relations(
        RunStateSnapshot(
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
                    reply_disposition=projection.reply_disposition,
                    aggregation=projection.aggregation,
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
    if (
        _is_pending(patch)
        and _is_settled(current)
        and current.active_run is not None
        and patch.active_run is not None
        and current.active_run.run_id == patch.active_run.run_id
    ):
        raise StatePatchRejected("pending_over_terminal")
    return patch
