"""ModelClient protocol, results, and ScriptedModel."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from agent_alfred.messages import Block, Message, TextBlock

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "content_filter",
    "paused",
    "context_exceeded",
    "error",
    "unknown",
]

Retryable = Literal[True, False, "unknown"]


@dataclass(frozen=True)
class ModelRef:
    endpoint_id: str
    model_id: str


@dataclass(frozen=True)
class Usage:
    total_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    endpoint_reported_cost_usd: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelError:
    retryable: Retryable
    status_code: int | None
    body_excerpt: str | None
    attempt_id: str
    code: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    streamed: bool
    outcome: Literal["committed", "aborted"]
    usage: Usage
    error: ModelError | None = None


@dataclass(frozen=True)
class ModelResponse:
    blocks: tuple[Block, ...]
    stop_reason: StopReason
    model: ModelRef


@dataclass(frozen=True)
class ModelResult:
    attempts: tuple[AttemptRecord, ...]
    response: ModelResponse | None
    final_error: ModelError | None

    def __post_init__(self) -> None:
        if not self.attempts:
            raise ValueError("ModelResult.attempts must be non-empty")
        if (self.response is None) == (self.final_error is None):
            raise ValueError("ModelResult needs exactly one of response or final_error")


@dataclass(frozen=True)
class ModelRequest:
    model: ModelRef
    system: tuple[TextBlock, ...] | None
    messages: tuple[Message, ...]
    tools: tuple[Any, ...] = ()
    max_tokens: int | None = None


class ModelClient(Protocol):
    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
        stream: bool = False,
    ) -> ModelResult: ...


@dataclass(frozen=True)
class ModelAssignment:
    """One assignable slot: a pinned (endpoint, model, wire style)."""

    endpoint_id: str
    model_id: str
    wire_style: str


@dataclass(frozen=True)
class ClientSnapshot:
    """Captured at admission. The api_key stays in memory; it is not an event."""

    config_version: str
    primary: ModelAssignment
    retrieval_gate: ModelAssignment | None
    api_key: str | None
    stream: bool
    stream_fallback: bool
    overall_deadline_s: float | None
    per_attempt_timeout_s: float

    @property
    def endpoint_id(self) -> str:
        return self.primary.endpoint_id

    @property
    def model_id(self) -> str:
        return self.primary.model_id

    @property
    def wire_style(self) -> str:
        return self.primary.wire_style


class ModelClientFactory(Protocol):
    def create(self, snapshot: ClientSnapshot) -> ModelClient: ...


class ScriptedModel:
    """A ModelClient that returns pre-canned responses. No network.

    Default use is as the outer client (drives the loop). The same class
    can sit at the inner Adapter seam to script retries.
    """

    def __init__(
        self,
        script: Sequence[str | ModelResult | ModelError | BaseException],
        *,
        gate: threading.Event | None = None,
    ):
        self._script = list(script)
        self._index = 0
        self.requests: list[ModelRequest] = []
        self.deadlines: list[float | None] = []
        self.stream_flags: list[bool] = []
        self._gate = gate
        self.entered = threading.Event()

    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
        stream: bool = False,
    ) -> ModelResult:
        del events
        self.entered.set()
        if self._gate is not None:
            self._gate.wait()
        self.requests.append(request)
        self.deadlines.append(deadline)
        self.stream_flags.append(stream)
        if self._index >= len(self._script):
            raise AssertionError("ScriptedModel has no remaining responses")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelResult):
            return item
        attempt_id = uuid.uuid4().hex
        if isinstance(item, ModelError):
            return ModelResult(
                attempts=(
                    AttemptRecord(
                        attempt_id=attempt_id,
                        streamed=stream,
                        outcome="aborted",
                        usage=Usage(),
                        error=item,
                    ),
                ),
                response=None,
                final_error=item,
            )
        response = ModelResponse(
            blocks=(TextBlock(item),),
            stop_reason="end_turn",
            model=request.model,
        )
        return ModelResult(
            attempts=(
                AttemptRecord(
                    attempt_id=attempt_id,
                    streamed=stream,
                    outcome="committed",
                    usage=Usage(),
                ),
            ),
            response=response,
            final_error=None,
        )


class ScriptedModelFactory:
    def __init__(self, model: ScriptedModel):
        self._model = model
        self.snapshots: list[ClientSnapshot] = []

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        self.snapshots.append(snapshot)
        return self._model
