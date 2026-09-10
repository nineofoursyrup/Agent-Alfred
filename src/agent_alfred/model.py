"""ModelClient protocol, results, and ScriptedModel."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Protocol, cast, get_args

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
STOP_REASONS: tuple[StopReason, ...] = get_args(StopReason)


def parse_stop_reason(value: object) -> StopReason:
    """Validate and narrow one model stop reason at a typed boundary."""
    if type(value) is not str or value not in STOP_REASONS:
        raise ValueError(f"invalid stop_reason: {value!r}")
    return cast(StopReason, value)

Retryable = Literal[True, False, "unknown"]
AttemptOutcome = Literal["committed", "aborted"]

ATTEMPT_COMMITTED: AttemptOutcome = "committed"
ATTEMPT_ABORTED: AttemptOutcome = "aborted"
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    ATTEMPT_COMMITTED,
    ATTEMPT_ABORTED,
)


def parse_attempt_outcome(value: object) -> AttemptOutcome:
    """Validate and narrow one Attempt outcome crossing a wire boundary."""
    if type(value) is not str:
        raise ValueError(f"invalid attempt outcome: {value!r}")
    if value == ATTEMPT_COMMITTED:
        return ATTEMPT_COMMITTED
    if value == ATTEMPT_ABORTED:
        return ATTEMPT_ABORTED
    raise ValueError(f"invalid attempt outcome: {value!r}")


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
    outcome: AttemptOutcome
    usage: Usage
    error: ModelError | None = None
    model: ModelRef | None = None


@dataclass(frozen=True)
class ModelResponse:
    blocks: tuple[Block, ...]
    stop_reason: StopReason
    model: ModelRef

    def __post_init__(self) -> None:
        parse_stop_reason(self.stop_reason)


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


class ModelCallInterrupted(BaseException):
    """A local/control failure after real Attempts, with their authoritative ledger.

    Strategies preserve this carrier; the Run ledger records its result before
    re-raising the original cause. It is not a new network Attempt or ModelError.
    """

    def __init__(self, cause: BaseException, result: ModelResult):
        super().__init__("model_call_interrupted")
        self.cause = cause
        self.result = result


def preserve_model_attempts(
    failure: BaseException, previous: ModelResult,
) -> ModelCallInterrupted:
    """Prepend completed strategy history without reconstructing from events."""
    if isinstance(failure, ModelCallInterrupted):
        return ModelCallInterrupted(failure.cause, ModelResult(
            attempts=previous.attempts + failure.result.attempts,
            response=failure.result.response,
            final_error=failure.result.final_error,
        ))
    return ModelCallInterrupted(failure, previous)


def mark_final_error_non_retryable(result: ModelResult) -> ModelResult:
    """Close only the final failed Attempt while preserving the full ledger."""
    error = result.final_error
    if error is None:
        return result
    closed = replace(error, retryable=False)
    attempts = tuple(
        replace(record, error=closed)
        if record.error is not None and record.attempt_id == closed.attempt_id
        else record
        for record in result.attempts
    )
    return ModelResult(attempts=attempts, response=None, final_error=closed)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


def tool_schema_jsonable(value):
    """Copy immutable declaration schemas into provider JSON containers."""
    if isinstance(value, Mapping):
        return {key: tool_schema_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [tool_schema_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class NamedToolChoice:
    name: str


ToolChoice = Literal["auto", "none", "required"] | NamedToolChoice


@dataclass(frozen=True)
class ModelRequest:
    model: ModelRef
    system: tuple[TextBlock, ...] | None
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None
    tool_choice: ToolChoice = "auto"
    # Called by the Adapter after local encoding, immediately at request start.
    # This business evidence is independent of optional event delivery.
    on_attempt_started: Callable[[str], None] | None = field(
        default=None, compare=False, repr=False
    )
    # Trusted execution provenance, not message text or a model-supplied ID.
    conversation_id: str | None = field(default=None, repr=False)
    # Transactional input registration receives the effective absolute deadline
    # selected by the strategy, including the shorter per-Attempt limit.
    on_attempt_preflight: Callable[[str, float | None], None] | None = field(
        default=None, compare=False, repr=False
    )

    def notify_attempt_started(self, attempt_id, deadline=None):
        if self.on_attempt_started is not None:
            self.on_attempt_started(attempt_id)
        if self.on_attempt_preflight is not None:
            self.on_attempt_preflight(attempt_id, deadline)


class EndpointUnconfigured(Exception):
    """No credential is available; no network round-trip may be sent."""


class ModelUnsupported(Exception):
    """The pinned shape is unsupported; preserve the assignment without sending."""


class ModelClient(Protocol):
    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
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
    retrieval_gate_api_key: str | None = field(default=None, repr=False)

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
        self._gate = gate
        self.entered = threading.Event()

    def respond(
        self,
        request: ModelRequest,
        *,
        events: Any | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        del events
        self.entered.set()
        if self._gate is not None:
            self._gate.wait()
        self.requests.append(request)
        self.deadlines.append(deadline)
        if self._index >= len(self._script):
            raise AssertionError("ScriptedModel has no remaining responses")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelResult):
            for attempt in item.attempts:
                request.notify_attempt_started(attempt.attempt_id, deadline)
            return item
        attempt_id = uuid.uuid4().hex
        request.notify_attempt_started(attempt_id, deadline)
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
        self.snapshots: list[ClientSnapshot] = []

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        self.snapshots.append(snapshot)
        return self._model
