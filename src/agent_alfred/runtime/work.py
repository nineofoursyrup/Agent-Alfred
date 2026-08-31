"""Shared admission/execution work types. Not a public API."""

from __future__ import annotations

from dataclasses import dataclass
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

SubmitKind = Literal["accepted"] | AdmissionRefusalKind

# What admission_reserve answers before the handoff: the SubmitKind
# vocabulary plus the internal "reserved" outcome that never escapes into a
# SubmitResult.
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
