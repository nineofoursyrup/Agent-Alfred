"""Shared admission/execution work types. Not a public API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agent_alfred.model import ClientSnapshot, ModelClient
from agent_alfred.runtime.snapshot import RuntimeSnapshot

AdmissionRefusalKind = Literal[
    "run_in_progress",
    "recording_unavailable",
    # A mutation from another door is already in flight. It is a conflict,
    # not an unavailability: nothing here is broken, something else is busy
    # (ADR-0016). Keeping it apart from ``recording_unavailable`` is what
    # lets the HTTP layer answer 409 for one and 503 for the other.
    "mutation_in_flight",
    "admission_failed",
]

# The accepted row committed but the unique executor handoff did not. This is
# a submit result, never a preflight/reserve refusal: keeping it out of
# ``AdmissionRefusalKind`` prevents coordinators from claiming it before a
# committed Run actually exists.
SubmitKind = (
    Literal[
        "accepted",
        "handoff_failed",
        "model_unsupported",
        "endpoint_unconfigured",
        "invalid_probe_target",
    ]
    | AdmissionRefusalKind
)

# What admission_reserve answers before the handoff: the refusal vocabulary
# plus the internal "reserved" outcome that never escapes into a SubmitResult.
ReserveKind = AdmissionRefusalKind | Literal["reserved"]

# What the read-only admission preflight answers. ``admissible`` is only an
# observation: callers must still use admission_reserve() after any work done
# outside the coordinator lock.
AdmissionObservationKind = AdmissionRefusalKind | Literal["admissible"]


@dataclass(frozen=True)
class SubmitRequest:
    message: str
    purpose: str = "chat"
    session_id: str | None = None
    gateway: str = "cli"
    entry_surface_id: str | None = None
    stream: bool = False
    # Synchronous callers consume the complete LoopResult through Host.wait().
    # Asynchronous callers observe the durable Run and session read models and
    # must opt out so private reply/attempt/usage payloads are never retained.
    wait_for_result: bool = True
    endpoint_id: str | None = None
    model_id: str | None = None
    retry_batch_id: str | None = None
    expected_revision: int | None = None
    operation_id: str | None = None
    consolidation_trigger_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.consolidation_trigger_run_id is not None and (
            self.purpose != "consolidation" or self.retry_batch_id is not None
        ):
            raise ValueError("invalid_consolidation_trigger")
        if self.purpose != "chat" and self.session_id is not None:
            raise ValueError("a system Run cannot name a Session")


@dataclass(frozen=True)
class SubmitResult:
    kind: SubmitKind
    run_id: str | None = None
    session_id: str | None = None
    snapshot: RuntimeSnapshot | None = None


@dataclass
class WorkItem:
    run_id: str
    request: SubmitRequest
    snapshot: ClientSnapshot
    client: ModelClient
    session_id: str | None
    prompt_preview: str | None
    accepted_at: str
    routing: object = None
    record_reply: bool = True
    memory_permission: object = field(default_factory=object, repr=False)
    memory_telemetry: dict = field(
        default_factory=lambda: {
            "gate_state": "not_evaluated",
            "gate": None,
            "input_attempts": [],
        }
    )
