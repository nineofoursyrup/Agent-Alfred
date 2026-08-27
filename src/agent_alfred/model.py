"""ModelClient protocol, results, and ScriptedModel."""

from __future__ import annotations

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
    ) -> ModelResult: ...


@dataclass(frozen=True)
class ClientSnapshot:
    """Captured at admission. The api_key stays in memory; it is not an event."""

    endpoint_id: str
    model_id: str
    wire_style: str
    api_key: str | None
    stream: bool


class ModelClientFactory(Protocol):
    def create(self, snapshot: ClientSnapshot) -> ModelClient: ...


class ScriptedModel:
    """A ModelClient that returns pre-canned responses. No network.

    Default use is as the outer client (drives the loop). The same class
    can sit at the inner Adapter seam to script retries.
    """

    def __init__(self, script: Sequence[str | ModelResult | ModelError | Exception]):
        self._script = list(script)
        self._index = 0
        self.requests: list[ModelRequest] = []

    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        del events, deadline
        self.requests.append(request)
        if self._index >= len(self._script):
            raise AssertionError("ScriptedModel has no remaining responses")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ModelResult):
            return item
        attempt_id = uuid.uuid4().hex
        if isinstance(item, ModelError):
            return ModelResult(
                attempts=(
                    AttemptRecord(
                        attempt_id=attempt_id,
                        streamed=False,
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
                    streamed=False,
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

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        del snapshot
        return self._model


