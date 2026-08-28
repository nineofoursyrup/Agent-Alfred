"""Shared admission/execution work types. Not a public API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_alfred.model import ClientSnapshot, ModelClient
from agent_alfred.runtime.snapshot import RuntimeSnapshot

SubmitKind = Literal[
    "accepted",
    "run_in_progress",
    "recording_unavailable",
    "admission_failed",
]


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
